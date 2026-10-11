// First-paint queries of the main pages, shared with the nav prefetcher so a
// prefetched result lands under the page's own key and runs the page's own
// cache seeding.
import { getBreadthBootstrap } from './api/breadth';
import { getGroupsBootstrap } from './api/groups';
import { getDailySnapshot } from './api/marketScan';
import { getScanBootstrap } from './api/scans';

export const GROUP_RANKING_MARKET_FALLBACKS = ['US', 'HK', 'IN', 'JP', 'KR', 'TW', 'CN', 'CA'];
export const BREADTH_MARKET_FALLBACKS = ['US', 'HK', 'IN', 'JP', 'KR', 'TW', 'CN', 'CA', 'DE'];

export const MARKET_LIVE_BENCHMARK_SYMBOLS = {
  US: 'SPY',
  HK: '2800.HK',
  IN: 'NIFTYBEES.NS',
  JP: '1306.T',
  KR: '069500.KS',
  TW: '0050.TW',
  CN: '000300.SS',
  CA: 'XIU.TO',
  DE: '^GDAXI',
  SG: 'ES3.SI',
  AU: 'IOZ.AX',
  MY: '^KLSE',
};

export const getBreadthDateRange = (range) => {
  const endDate = new Date();
  const startDate = new Date();
  if (range === '1M') startDate.setMonth(startDate.getMonth() - 1);
  else if (range === '3M') startDate.setMonth(startDate.getMonth() - 3);
  else if (range === '6M') startDate.setMonth(startDate.getMonth() - 6);
  else startDate.setFullYear(startDate.getFullYear() - 1);
  return {
    startDate: startDate.toISOString().split('T')[0],
    endDate: endDate.toISOString().split('T')[0],
  };
};

// The breadth history table's fixed 90-day window.
export const breadthHistoryWindow = () => ({
  startDate: new Date(Date.now() - 90 * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
  endDate: new Date().toISOString().split('T')[0],
});

export const dailySnapshotQuery = (market) => ({
  queryKey: ['dailySnapshot', market],
  queryFn: () => getDailySnapshot(market),
  staleTime: 60_000,
});

// Market-scoped so the first load selects and seeds the selected market's
// scans, never another market's newest scan.
export const scanBootstrapQuery = (market) => ({
  queryKey: ['scanBootstrap', 'latest', market ?? null],
  queryFn: () => getScanBootstrap(null, market ?? null),
  retry: false,
  staleTime: 60_000,
});

// The snapshot fast-path: the bootstrap queryFn seeds the live query caches
// synchronously for this exact market — key and payload come from the same
// fetch closure, so cross-market mixups are structurally impossible (no
// seeding effect, no settled flag).
export const groupsBootstrapQuery = (queryClient, market) => ({
  queryKey: ['groupsBootstrap', market],
  queryFn: async () => {
    const snapshot = await getGroupsBootstrap(market);
    if (snapshot && !snapshot.is_stale) {
      const payload = snapshot.payload ?? {};
      const rankingDate = payload.rankings?.date ?? null;
      queryClient.setQueryData(['groupRankings', market, rankingDate], payload.rankings ?? null);
      queryClient.setQueryData(['groupMovers', '1w', market, rankingDate], payload.movers ?? null);
    }
    return snapshot;
  },
  retry: false,
  staleTime: 60_000,
});

export const breadthBootstrapQuery = (queryClient, market) => ({
  queryKey: ['breadthBootstrap', market],
  queryFn: async () => {
    const snapshot = await getBreadthBootstrap(market);
    if (snapshot && !snapshot.is_stale) {
      const payload = snapshot.payload ?? {};
      const { startDate, endDate } = breadthHistoryWindow();
      queryClient.setQueryData(['breadth', 'current', market], payload.current ?? null);
      queryClient.setQueryData(
        ['breadth', 'historical', market, startDate, endDate],
        payload.history_90d ?? [],
      );
      queryClient.setQueryData(['breadth', 'summary', market], payload.summary ?? {});
      if (payload.chart_range === '1M') {
        const chartRange = getBreadthDateRange('1M');
        queryClient.setQueryData(
          ['breadth', 'chart', market, chartRange.startDate, chartRange.endDate],
          payload.chart_data ?? [],
        );
        const benchmarkSymbol = MARKET_LIVE_BENCHMARK_SYMBOLS[market] || null;
        if (benchmarkSymbol) {
          queryClient.setQueryData(
            ['benchmark', 'history', market, benchmarkSymbol, '1mo'],
            payload.benchmark_overlay ?? payload.spy_overlay ?? [],
          );
        }
      }
    }
    return snapshot;
  },
  retry: false,
  staleTime: 60_000,
});
