"""Unit rules for percentage fields in Yahoo's quote summary.

``dividendYield`` is already in percentage points (measured with the pinned
``yfinance==0.2.66`` against ``dividendRate / price``: AAPL 0.31 vs 0.3157 %,
KO 2.27 vs 2.2739 %), while ``trailingAnnualDividendYield`` is a decimal
fraction. Every provider path that reads the key must use this helper, so the
unit cannot drift between the single-symbol and the bulk path.
"""

from __future__ import annotations

import math
from typing import Optional

#: A yield above this is a unit error or garbage, not a measurement. Kept
#: generous: one-off special dividends have produced three-digit yields.
MAX_PLAUSIBLE_YIELD_PERCENT = 1_000.0


def as_percentage_points(value: Optional[float]) -> Optional[float]:
    """Return a percentage-point value unchanged, or ``None`` if it is not one.

    ``None`` stays ``None``, a real ``0`` stays ``0`` (a non-payer is an answer,
    not a gap), and a non-numeric, non-finite, negative or implausibly large
    value becomes ``None`` instead of a plausible-looking number.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or parsed > MAX_PLAUSIBLE_YIELD_PERCENT:
        return None
    return parsed
