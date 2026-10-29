import { QueryClient, dehydrate } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from './App';
import {
  PERSISTED_QUERY_CACHE_BUSTER,
  shouldDehydratePersistedQuery,
} from './appQueryPersistence';
import { DEFAULT_CAPABILITIES } from './contexts/RuntimeContext';

const { getAppCapabilities } = vi.hoisted(() => ({ getAppCapabilities: vi.fn() }));

vi.mock('./config/runtimeMode', () => ({ STATIC_SITE_MODE: false }));
vi.mock('./api/appRuntime', () => ({
  getAppCapabilities: (...args) => getAppCapabilities(...args),
  startRuntimeBootstrap: vi.fn(),
  updateRuntimeMarkets: vi.fn(),
}));
vi.mock('./components/App/ServerLoginScreen', () => ({ default: () => <div>Server Login</div> }));
vi.mock('./components/Layout/Layout', () => ({ default: ({ children }) => <div>{children}</div> }));
vi.mock('./contexts/PipelineContext', () => ({
  PipelineProvider: ({ children }) => <>{children}</>,
}));
vi.mock('./contexts/StrategyProfileContext', () => ({
  StrategyProfileProvider: ({ children }) => <>{children}</>,
}));
vi.mock('./pageLoaders', async (importOriginal) => ({
  ...(await importOriginal()),
  loadMarketScanPage: async () => ({ default: () => <div>Market Scan Page</div> }),
}));

const successfulQuery = (queryKey) => ({
  queryKey,
  state: { status: 'success' },
});

const failedQuery = (queryKey) => ({
  queryKey,
  state: { status: 'error' },
});

describe('app query persistence', () => {
  it('persists app capabilities but not volatile runtime activity', () => {
    expect(shouldDehydratePersistedQuery(successfulQuery(['appCapabilities']))).toBe(true);
    expect(shouldDehydratePersistedQuery(successfulQuery(['runtimeActivity']))).toBe(false);
  });

  it('never restores a scannable watchlist symbol list from storage', () => {
    expect(shouldDehydratePersistedQuery(successfulQuery(['userWatchlistSymbols', 7]))).toBe(false);
    expect(shouldDehydratePersistedQuery(successfulQuery(['userWatchlists']))).toBe(true);
  });

  it('persists ordinary successful data queries only', () => {
    expect(shouldDehydratePersistedQuery(successfulQuery(['scanHistory', 'US']))).toBe(true);
    expect(shouldDehydratePersistedQuery(failedQuery(['scanHistory', 'US']))).toBe(false);
  });

  it('persists only the most recently loaded scan results page', () => {
    const queryClient = new QueryClient();
    const queryCache = queryClient.getQueryCache();
    queryClient.setQueryData(['scanResultsQuery', 'scan-1', 'page-1'], { rows: 1 }, { updatedAt: 1000 });
    queryClient.setQueryData(['scanResultsQuery', 'scan-1', 'page-2'], { rows: 2 }, { updatedAt: 2000 });
    queryClient.setQueryData(['scanHistory', 'US'], { scans: [] }, { updatedAt: 500 });

    const find = (queryKey) => queryCache.find({ queryKey, exact: true });

    expect(shouldDehydratePersistedQuery(find(['scanResultsQuery', 'scan-1', 'page-1']), queryCache)).toBe(false);
    expect(shouldDehydratePersistedQuery(find(['scanResultsQuery', 'scan-1', 'page-2']), queryCache)).toBe(true);
    expect(shouldDehydratePersistedQuery(find(['scanHistory', 'US']), queryCache)).toBe(true);
  });
});

describe('App refresh with a persisted cache', () => {
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  it('paints the app from an authenticated cache, then shows login once the live check says the session expired', async () => {
    const previousPageClient = new QueryClient();
    previousPageClient.setQueryData(['appCapabilities'], {
      ...DEFAULT_CAPABILITIES,
      auth: { ...DEFAULT_CAPABILITIES.auth, required: true, authenticated: true },
    }, { updatedAt: Date.now() - 60_000 });
    window.localStorage.setItem('stockscanner-query-cache', JSON.stringify({
      timestamp: Date.now(),
      buster: PERSISTED_QUERY_CACHE_BUSTER,
      clientState: dehydrate(previousPageClient, {
        shouldDehydrateQuery: shouldDehydratePersistedQuery,
      }),
    }));
    let resolveLive;
    getAppCapabilities.mockReturnValue(new Promise((resolve) => { resolveLive = resolve; }));

    render(<App />);

    expect(await screen.findByText('Market Scan Page')).toBeInTheDocument();
    expect(getAppCapabilities).toHaveBeenCalledTimes(1);

    resolveLive({
      ...DEFAULT_CAPABILITIES,
      auth: { ...DEFAULT_CAPABILITIES.auth, required: true, authenticated: false },
    });

    expect(await screen.findByText('Server Login')).toBeInTheDocument();
    expect(screen.queryByText('Market Scan Page')).not.toBeInTheDocument();
  });
});
