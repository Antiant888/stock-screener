"""Profile where a bulk scan's wall-clock time goes, per chunk phase (issue #416).

Runs the real RunBulkScanUseCase against this deployment's database and cache,
with every port the chunk loop calls wrapped in a timer:

    prefetch        data_provider.prepare_data_bulk        (parent, I/O + frame building)
    market_rs       market_rs_reader.get + apply           (parent, I/O)
    compute_submit  batch runner submit_batch              (parent, pickling calls to the pool)
    compute_wait    pending batch result()                 (parent blocked on the compute pool)
    persist         scan_results.persist_orchestrator_results (parent, I/O)
    commit          uow.commit                             (parent, I/O)
    cancel_check    cancel.is_cancelled                    (parent, I/O)

The chunk loop is pipelined: the parent prefetches and submits chunk N+1
before it collects and persists chunk N. A chunk record closes on the
progress event for chunk N, so it holds the prefetch, market_rs and
compute_submit of N+1 and the compute_wait, persist and commit of N. Totals
are unaffected. compute_wait is the time the parent had nothing left to do
but wait for the pool.

The scan row is created with status "failed" and its status is never changed.
That status is not active, so the run does not take the single-active-scan
slot or block user scans, and it is not "completed" or "cancelled", so nothing
that reads the latest finished scan can pick up its partial results. The row
and its results are deleted afterwards, including after Ctrl-C or SIGTERM.
Two cases leave the row behind, and it then shows as a failed scan in scan
history: --keep (on purpose, to inspect the results) and SIGKILL or an
out-of-memory kill. Remove it from the UI, or with DELETE /api/v1/scans/<id>;
its universe_key starts with "profile-scan-phases:".

Provider access is blocked. The scan runs with ``cache_only``, which keeps the
bulk per-stock price read off the provider (#451) but not the benchmark lookup
(#455); the script stubs the bulk, benchmark and single-symbol price fetches
with counters and points HTTP(S) at
a dead proxy, with any proxy bypass cleared, so anything that still reaches a
provider fails fast and shows up in the "provider fetches blocked" line. A
non-zero count there means some path is not cache-only.

Usage (inside a backend or worker container):

    python scripts/profile_scan_phases.py --market US --limit 1000
    python scripts/profile_scan_phases.py --market US --limit 0      # whole universe
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import statistics
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.domain.scanning.models import ScanStatus  # noqa: E402
from app.domain.scanning.ports import ProgressSink  # noqa: E402
from app.infra.db.uow import SqlUnitOfWork  # noqa: E402
from app.infra.tasks.cancellation import DbCancellationToken  # noqa: E402
from app.infra.tasks.scan_compute_pool import process_stock_scan_batch_runner  # noqa: E402
from app.models.stock_universe import StockUniverse  # noqa: E402
from app.use_cases.scanning.run_bulk_scan import (  # noqa: E402
    RunBulkScanCommand,
    RunBulkScanUseCase,
)
from app.utils.parallelism import resolve_scan_compute_processes  # noqa: E402

PARENT_IO_PHASES = ("prefetch", "market_rs", "persist", "commit", "cancel_check")
DEFAULT_SCREENERS = "minervini,canslim,ipo,custom,volume_breakthrough,setup_engine"


class PhaseClock:
    """Seconds per phase for the chunk in progress, and one record per finished chunk."""

    def __init__(self) -> None:
        self.current: dict[str, float] = {}
        self.chunks: list[dict[str, float]] = []
        self._chunk_started = time.perf_counter()

    @contextmanager
    def phase(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.current[name] = self.current.get(name, 0.0) + time.perf_counter() - started

    def end_chunk(self) -> None:
        now = time.perf_counter()
        self.chunks.append({**self.current, "wall": now - self._chunk_started})
        self.current = {}
        self._chunk_started = now

    def restart(self) -> None:
        """Start timing the next chunk from now, so time spent in between
        (setup before chunk 1, the profiler's own output) is not charged to it."""
        self.current = {}
        self._chunk_started = time.perf_counter()


class _Timed:
    """Delegates to ``target``, timing the named methods into a phase."""

    def __init__(self, target, clock: PhaseClock, phases: dict[str, str]) -> None:
        self._target = target
        self._clock = clock
        self._phases = phases

    def __getattr__(self, name: str):
        attribute = getattr(self._target, name)
        phase = self._phases.get(name)
        if phase is None:
            return attribute

        def timed(*args, **kwargs):
            with self._clock.phase(phase):
                return attribute(*args, **kwargs)

        return timed


def _timed_or_none(target, clock: PhaseClock, phases: dict[str, str]):
    return None if target is None else _Timed(target, clock, phases)


class _TimedCancel:
    """The chunk loop starts with a cancellation check; the first one starts the clock."""

    def __init__(self, cancel, clock: PhaseClock) -> None:
        self._cancel = cancel
        self._clock = clock
        self._started = False

    def is_cancelled(self) -> bool:
        if not self._started:
            self._started = True
            self._clock.restart()
        with self._clock.phase("cancel_check"):
            return self._cancel.is_cancelled()


class _InertScans:
    """Scan repository whose status never changes (see module docstring).

    The use case would otherwise mark the row running, then completed.
    """

    def __init__(self, target) -> None:
        self._target = target

    def __getattr__(self, name: str):
        return getattr(self._target, name)

    def update_status(self, *_args, **_kwargs) -> None:
        return None


class _ProfilingUow(SqlUnitOfWork):
    def __init__(self, session_factory, clock: PhaseClock) -> None:
        super().__init__(session_factory)
        self._clock = clock

    def __enter__(self):
        super().__enter__()
        self.scans = _InertScans(self.scans)
        self.scan_results = _Timed(
            self.scan_results, self._clock, {"persist_orchestrator_results": "persist"}
        )
        return self

    def commit(self) -> None:
        with self._clock.phase("commit"):
            super().commit()


class _ChunkBoundary(ProgressSink):
    """The use case emits progress once per persisted chunk."""

    def __init__(self, clock: PhaseClock) -> None:
        self._clock = clock

    def emit(self, event) -> None:
        self._clock.end_chunk()
        print(f"  {event.current}/{event.total} symbols", flush=True)
        self._clock.restart()


class _CacheTierCounter(logging.Handler):
    """Sums the price cache's own 'Bulk fetched ...' summary lines."""

    FIELDS = ("symbols", "redis_hits", "stale", "stale_intraday", "insufficient", "misses")

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.totals = dict.fromkeys(self.FIELDS, 0)

    def emit(self, record: logging.LogRecord) -> None:
        if not str(record.msg).startswith("Bulk fetched") or not isinstance(record.args, tuple):
            return
        for name, value in zip(self.FIELDS, record.args):
            self.totals[name] += int(value)


def block_provider_fetches() -> dict[str, int]:
    """Replace the price paths' provider fetches with counters.

    Covers the bulk price fetch (reached by cache-only scans before #451), the
    benchmark fetch and the single-symbol price fetch. The dead proxy is a
    backstop for anything else; it only works if nothing bypasses it.
    """
    from app.services.benchmark_cache_service import BenchmarkCacheService
    from app.services.bulk_data_fetcher import BulkDataFetcher
    from app.services.price_cache_service import PriceCacheService

    for variable in ("NO_PROXY", "no_proxy"):
        os.environ.pop(variable, None)
    # Nothing listens on this port, so any other HTTP(S) provider call fails fast.
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ[variable] = os.environ[variable.lower()] = "http://127.0.0.1:9"

    blocked = {"calls": 0, "symbols": 0}

    def count(symbols: int) -> None:
        blocked["calls"] += 1
        blocked["symbols"] += symbols

    def fetch_no_prices(self, symbols, *args, **kwargs):
        count(len(symbols))
        return {}

    def fetch_no_frame(self, *args, **kwargs):
        count(1)
        return None

    BulkDataFetcher.fetch_prices_in_batches = fetch_no_prices
    BenchmarkCacheService._fetch_normalized_benchmark = fetch_no_frame
    PriceCacheService._fetch_direct_historical_data = fetch_no_frame
    return blocked


def _session_state(market: str) -> dict:
    """Whether the market is in session, and the day prices are judged fresh against."""
    from app.wiring.bootstrap import get_market_calendar_service

    calendar = get_market_calendar_service()
    try:
        return {
            "market_open": calendar.is_market_open(market),
            "last_completed_trading_day": str(calendar.last_completed_trading_day(market)),
        }
    except Exception as exc:  # a missing calendar must not abort a profile
        return {"error": str(exc)}


class _TimedRunner:
    """Times a batch's submission and the wait for its result as two phases."""

    def __init__(self, runner, clock: PhaseClock) -> None:
        self._runner = runner
        self._clock = clock

    def submit_batch(self, calls):
        with self._clock.phase("compute_submit"):
            pending = self._runner.submit_batch(calls)
        return _Timed(pending, self._clock, {"result": "compute_wait"})


def _timed_runner_factory(clock: PhaseClock):
    @contextmanager
    def factory(scanner, processes):
        with process_stock_scan_batch_runner(scanner, processes) as runner:
            yield _TimedRunner(runner, clock)

    return factory


def _universe_symbols(market: str, limit: int) -> list[str]:
    db = SessionLocal()
    try:
        symbols = [
            row[0]
            for row in db.query(StockUniverse.symbol)
            .filter(StockUniverse.active_filter(), StockUniverse.market == market)
            .order_by(StockUniverse.symbol)
        ]
    finally:
        db.close()
    if limit and len(symbols) > limit:
        # Evenly spaced, so the sample is not just the start of the alphabet.
        step = len(symbols) / limit
        symbols = [symbols[int(index * step)] for index in range(limit)]
    return symbols


def _create_scan(scan_id: str, market: str, symbols: list[str], screeners: list[str]) -> None:
    with SqlUnitOfWork(SessionLocal) as uow:
        uow.scans.create(
            scan_id=scan_id,
            criteria={},
            universe="custom",
            universe_key=f"profile-scan-phases:{scan_id}",
            universe_type="custom",
            universe_market=market,
            screener_types=screeners,
            composite_method="weighted_average",
            # Neither active nor selectable as a finished scan; see the module docstring.
            status=ScanStatus.FAILED.value,
            total_stocks=len(symbols),
            passed_stocks=0,
            trigger_source="manual",
        )
        uow.commit()


def _delete_scan(scan_id: str) -> None:
    with SqlUnitOfWork(SessionLocal) as uow:
        uow.scan_results.delete_by_scan_id(scan_id)
        uow.scans.delete(scan_id)
        uow.commit()


def summarize(chunks: list[dict[str, float]]) -> dict:
    """Phase totals and shares across every finished chunk."""
    wall = sum(chunk["wall"] for chunk in chunks)
    phases = (
        "prefetch",
        "market_rs",
        "compute_submit",
        "compute_wait",
        "persist",
        "commit",
        "cancel_check",
    )
    totals = {name: sum(chunk.get(name, 0.0) for chunk in chunks) for name in phases}
    totals["other_parent"] = wall - sum(totals.values())
    return {
        "chunks": len(chunks),
        "wall_seconds": wall,
        "totals": totals,
        "shares": {name: (value / wall if wall else 0.0) for name, value in totals.items()},
        "median_per_chunk": {
            name: statistics.median(chunk.get(name, 0.0) for chunk in chunks) for name in phases
        },
        "parent_io_share": (
            sum(totals[name] for name in PARENT_IO_PHASES) / wall if wall else 0.0
        ),
    }


def _print_report(summary: dict, *, symbols: int, processes: int, chunk_size: int, cache: dict) -> None:
    wall = summary["wall_seconds"]
    print(
        f"\n{symbols} symbols, {summary['chunks']} chunks of {chunk_size}, "
        f"{processes} compute processes, {wall:.1f}s wall "
        f"({symbols / wall:.1f} symbols/s)"
    )
    print(f"{'phase':14s} {'total s':>9s} {'share':>7s} {'median ms/chunk':>16s}")
    for name, total in summary["totals"].items():
        median = summary["median_per_chunk"].get(name)
        median_text = f"{median * 1e3:16.0f}" if median is not None else f"{'':>16s}"
        print(f"{name:14s} {total:9.2f} {summary['shares'][name]:7.1%} {median_text}")
    print(f"\nparent I/O share of wall time: {summary['parent_io_share']:.1%}")
    print(f"price cache tiers: {cache}")
    print("JSON " + json.dumps({**summary, "symbols": symbols, "processes": processes, "cache": cache}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", default="US")
    parser.add_argument("--limit", type=int, default=1000, help="symbols to scan; 0 = whole universe")
    parser.add_argument("--screeners", default=DEFAULT_SCREENERS)
    parser.add_argument("--processes", type=int, default=0, help="0 = the deployment's setting")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the scan and its results (it then shows as a failed scan in scan history)",
    )
    args = parser.parse_args()

    # ERROR keeps per-symbol scanner warnings out of the report.
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s %(message)s")
    cache_tiers = _CacheTierCounter()
    price_cache_logger = logging.getLogger("app.services.price_cache_service")
    price_cache_logger.setLevel(logging.INFO)
    price_cache_logger.addHandler(cache_tiers)
    price_cache_logger.propagate = False

    from app.wiring.bootstrap import (
        get_run_bulk_scan_use_case,
        initialize_process_runtime_services,
    )

    blocked_fetches = block_provider_fetches()
    initialize_process_runtime_services()
    wired = get_run_bulk_scan_use_case()

    market = args.market.upper()
    symbols = _universe_symbols(market, args.limit)
    if not symbols:
        print(f"No active symbols for market {market}")
        return 1
    screeners = [name.strip() for name in args.screeners.split(",") if name.strip()]
    processes = args.processes or resolve_scan_compute_processes(settings.scan_compute_processes)
    chunk_size = settings.scan_usecase_chunk_size

    clock = PhaseClock()
    use_case = RunBulkScanUseCase(
        scanner=wired._scanner,
        data_provider=_timed_or_none(
            wired._data_provider,
            clock,
            {"prepare_data_bulk": "prefetch", "apply_market_rs_resolution": "market_rs"},
        ),
        market_rs_reader=_timed_or_none(wired._market_rs_reader, clock, {"get": "market_rs"}),
        scan_batch_runner_factory=_timed_runner_factory(clock),
    )

    # SIGTERM (docker stop, a timeout) would otherwise skip the cleanup below.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    scan_id = str(uuid.uuid4())
    cancel = None
    # From here on every exit path, including one during setup, reaches cleanup.
    # Deleting a scan that was never created is a no-op.
    try:
        _create_scan(scan_id, market, symbols, screeners)
        cancel = DbCancellationToken(SessionLocal, scan_id)
        session_at_start = _session_state(market)
        print(
            f"Profiling scan {scan_id}: {len(symbols)} {market} symbols, {processes} processes, "
            f"session {session_at_start}",
            flush=True,
        )
        result = use_case.execute(
            _ProfilingUow(SessionLocal, clock),
            RunBulkScanCommand(
                scan_id=scan_id,
                symbols=symbols,
                chunk_size=chunk_size,
                cache_only=True,
                parallel_workers=processes,
            ),
            _ChunkBoundary(clock),
            _TimedCancel(cancel, clock),
        )
    finally:
        if cancel is not None:
            cancel.close()
        if not args.keep:
            _delete_scan(scan_id)

    print(f"Scan finished: {result.status}, {result.total_scanned} scanned, {result.failed} without a result")
    print(
        f"provider fetches blocked: {blocked_fetches['calls']} calls "
        f"for {blocked_fetches['symbols']} symbols"
    )
    session_at_end = _session_state(market)
    if session_at_end != session_at_start:
        print(
            f"WARNING: the market session changed during the run ({session_at_start} -> "
            f"{session_at_end}). The freshness cut-off moved, so chunks before and after "
            "are not comparable."
        )
    if not clock.chunks:
        print("No chunk finished, so there is nothing to report.")
        return 1
    _print_report(
        summarize(clock.chunks),
        symbols=len(symbols),
        processes=processes,
        chunk_size=chunk_size,
        cache=cache_tiers.totals,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
