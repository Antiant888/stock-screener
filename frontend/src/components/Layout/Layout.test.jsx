import { MemoryRouter } from 'react-router-dom';
import { fireEvent, screen } from '@testing-library/react';
import { act } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Layout from './Layout';
import { MarketProvider } from '../../contexts/MarketContext';
import { renderWithProviders } from '../../test/renderWithProviders';

const runtimeState = {
  auth: { required: false, authenticated: true },
  features: { chatbot: false, themes: false, tasks: false },
  marketCatalog: { markets: [] },
  primaryMarket: 'US',
  isLoggingOut: false,
  logout: vi.fn(),
};

const strategyState = {
  activeProfile: 'default',
  setActiveProfile: vi.fn(),
};
const useRuntimeActivityMock = vi.hoisted(() => vi.fn());

vi.mock('../../contexts/RuntimeContext', () => ({
  useRuntime: () => runtimeState,
}));

vi.mock('../../contexts/StrategyProfileContext', () => ({
  useStrategyProfile: () => strategyState,
  useStrategyProfileData: () => ({
    ...strategyState,
    activeProfileDetail: { label: 'Default' },
    profiles: [{ profile: 'default', label: 'Default' }],
    isLoadingProfiles: false,
  }),
}));

vi.mock('../../hooks/useRuntimeActivity', () => ({
  useRuntimeActivity: (...args) => useRuntimeActivityMock(...args),
}));

const requestedPages = vi.hoisted(() => new Set());
const api = vi.hoisted(() => ({
  getBreadthBootstrap: vi.fn(),
  getDailySnapshot: vi.fn(),
  getGroupsBootstrap: vi.fn(),
}));

vi.mock('../../pages/BreadthPage', () => {
  requestedPages.add('BreadthPage');
  return { default: () => null };
});
vi.mock('../../pages/MarketScanPage', () => ({ default: () => null }));
vi.mock('../../pages/GroupRankingsPage', () => ({ default: () => null }));
vi.mock('../../api/breadth', async (importOriginal) => ({
  ...(await importOriginal()),
  getBreadthBootstrap: api.getBreadthBootstrap,
}));
vi.mock('../../api/marketScan', async (importOriginal) => ({
  ...(await importOriginal()),
  getDailySnapshot: api.getDailySnapshot,
}));
vi.mock('../../api/groups', async (importOriginal) => ({
  ...(await importOriginal()),
  getGroupsBootstrap: api.getGroupsBootstrap,
}));

function renderInApp(path = '/scan') {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <MarketProvider>
        <Layout><div>content</div></Layout>
      </MarketProvider>
    </MemoryRouter>,
  );
}

describe('Layout', () => {
  beforeEach(() => {
    useRuntimeActivityMock.mockReset();
    Object.values(api).forEach((mock) => mock.mockReset().mockResolvedValue(null));
    window.localStorage.clear();
    runtimeState.features = { chatbot: false, themes: false, tasks: false };
    runtimeState.marketCatalog = { markets: [] };
    runtimeState.primaryMarket = 'US';
    runtimeState.enabledMarkets = undefined;
    runtimeState.uiSnapshots = undefined;
  });

  it("loads a page's code and snapshot when its nav link is hovered", async () => {
    useRuntimeActivityMock.mockReturnValue({ data: null });
    runtimeState.enabledMarkets = ['US', 'HK'];
    runtimeState.primaryMarket = 'HK';
    runtimeState.uiSnapshots = { breadth: true };
    renderInApp();

    fireEvent.mouseEnter(screen.getByRole('link', { name: 'Breadth' }));

    await vi.waitFor(() => expect(requestedPages).toContain('BreadthPage'));
    expect(api.getBreadthBootstrap).toHaveBeenCalledWith('HK');
  });

  it('skips the snapshot a page would not request', async () => {
    useRuntimeActivityMock.mockReturnValue({ data: null });
    runtimeState.uiSnapshots = { breadth: false };
    renderInApp();

    fireEvent.mouseEnter(screen.getByRole('link', { name: 'Breadth' }));
    await act(async () => {});

    expect(api.getBreadthBootstrap).not.toHaveBeenCalled();
  });

  it('prefetches the daily snapshot for the selected market on focus', async () => {
    useRuntimeActivityMock.mockReturnValue({ data: null });
    runtimeState.enabledMarkets = ['US', 'HK'];
    runtimeState.primaryMarket = 'HK';
    renderInApp();

    fireEvent.focus(screen.getByRole('link', { name: 'Daily' }));

    await vi.waitFor(() => expect(api.getDailySnapshot).toHaveBeenCalledWith('HK'));
  });

  it('seeds the same caches as the page when it prefetches a groups snapshot', async () => {
    useRuntimeActivityMock.mockReturnValue({ data: null });
    runtimeState.uiSnapshots = { groups: true };
    const rankings = { date: '2026-10-09', rankings: [] };
    api.getGroupsBootstrap.mockResolvedValue({
      is_stale: false,
      payload: { rankings, movers: { gainers: [] } },
    });
    const { queryClient } = renderInApp();

    fireEvent.mouseEnter(screen.getByRole('link', { name: 'Groups' }));

    await vi.waitFor(() => (
      expect(queryClient.getQueryData(['groupRankings', 'US', '2026-10-09'])).toEqual(rankings)
    ));
    expect(queryClient.getQueryData(['groupMovers', '1w', 'US', '2026-10-09'])).toEqual({ gainers: [] });
  });

  it('defers the header runtime activity request until after initial paint', () => {
    vi.useFakeTimers();
    useRuntimeActivityMock.mockReturnValue({ data: null });

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={['/']}>
          <Layout>
            <div>content</div>
          </Layout>
        </MemoryRouter>
      );

      expect(useRuntimeActivityMock).toHaveBeenLastCalledWith({ enabled: false });
      expect(screen.getByText('Checking activity')).toBeInTheDocument();
      expect(screen.getByText('View operations')).toBeInTheDocument();
      expect(screen.queryByText('Markets ready')).not.toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(1500);
      });

      expect(useRuntimeActivityMock).toHaveBeenLastCalledWith({ enabled: true });
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the runtime activity header summary and removes Digest navigation', () => {
    useRuntimeActivityMock.mockReturnValue({
      dataUpdatedAt: 1,
      data: {
        bootstrap: { state: 'ready' },
        summary: { active_market_count: 1, status: 'active' },
        markets: [
          {
            market: 'HK',
            status: 'running',
            stage_label: 'Fundamentals Refresh',
          },
        ],
      },
    });

    renderWithProviders(
      <MemoryRouter initialEntries={['/scan']}>
        <Layout>
          <div>content</div>
        </Layout>
      </MemoryRouter>
    );

    expect(screen.getByText('1 market active')).toBeInTheDocument();
    expect(screen.getByText('HK · Fundamentals Refresh')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /digest/i })).not.toBeInTheDocument();
  });

  it('shows stale runtime activity as a header warning', () => {
    useRuntimeActivityMock.mockReturnValue({
      dataUpdatedAt: 1,
      data: {
        bootstrap: { state: 'ready' },
        summary: { active_market_count: 0, status: 'warning' },
        markets: [
          {
            market: 'US',
            status: 'stale',
            stage_label: 'Price Refresh',
            message: 'Refreshing market prices - stale: No live data-fetch lock owns task old-task.',
          },
        ],
      },
    });

    renderWithProviders(
      <MemoryRouter initialEntries={['/scan']}>
        <Layout>
          <div>content</div>
        </Layout>
      </MemoryRouter>
    );

    expect(screen.getByText('Refresh warning')).toBeInTheDocument();
    expect(screen.getByText('US · Price Refresh')).toBeInTheDocument();
  });

  it('does not show a fake 0 percent for indeterminate bootstrap progress', () => {
    useRuntimeActivityMock.mockReturnValue({
      dataUpdatedAt: 1,
      data: {
        bootstrap: {
          state: 'running',
          primary_market: 'US',
          current_stage: 'Universe Refresh',
          progress_mode: 'indeterminate',
          percent: null,
          message: 'Refreshing official market universe',
        },
        summary: { active_market_count: 1, status: 'active' },
        markets: [
          {
            market: 'US',
            status: 'running',
            stage_label: 'Universe Refresh',
            progress_mode: 'indeterminate',
            percent: null,
          },
        ],
      },
    });

    renderWithProviders(
      <MemoryRouter initialEntries={['/scan']}>
        <Layout>
          <div>content</div>
        </Layout>
      </MemoryRouter>
    );

    expect(screen.getByText('Bootstrapping US')).toBeInTheDocument();
    expect(screen.getByText('Universe Refresh')).toBeInTheDocument();
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });

  it('shows Options only for an enabled US market capability and keeps detail active', () => {
    useRuntimeActivityMock.mockReturnValue({ data: null });
    runtimeState.features = { ...runtimeState.features, options_analytics: true };
    runtimeState.marketCatalog = {
      markets: [{ code: 'US', capabilities: { options_analytics: true } }],
    };

    const { rerender } = renderWithProviders(
      <MemoryRouter initialEntries={['/options/AAPL']}>
        <Layout><div>content</div></Layout>
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Options' })).toHaveStyle({ fontWeight: '600' });

    runtimeState.primaryMarket = 'HK';
    rerender(
      <MemoryRouter initialEntries={['/options/AAPL']}>
        <Layout><div>content</div></Layout>
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link', { name: 'Options' })).not.toBeInTheDocument();
  });
});
