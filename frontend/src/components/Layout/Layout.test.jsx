import { MemoryRouter } from 'react-router-dom';
import { fireEvent, screen, within } from '@testing-library/react';
import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Layout from './Layout';
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

describe('Layout', () => {
  beforeEach(() => {
    useRuntimeActivityMock.mockReset();
    runtimeState.features = { chatbot: false, themes: false, tasks: false };
    runtimeState.marketCatalog = { markets: [] };
    runtimeState.primaryMarket = 'US';
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
    expect(screen.getByRole('link', { name: 'Options' })).toHaveAttribute('aria-current', 'page');

    runtimeState.primaryMarket = 'HK';
    rerender(
      <MemoryRouter initialEntries={['/options/AAPL']}>
        <Layout><div>content</div></Layout>
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link', { name: 'Options' })).not.toBeInTheDocument();
  });

  describe('on a phone-width screen', () => {
    beforeEach(() => {
      // useMediaQuery(breakpoints.down('md')) matches.
      window.matchMedia = vi.fn().mockReturnValue({
        matches: true,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
      useRuntimeActivityMock.mockReturnValue({
        dataUpdatedAt: 1,
        data: { bootstrap: { state: 'ready' }, summary: { active_market_count: 0 }, markets: [] },
      });
    });

    afterEach(() => {
      delete window.matchMedia;
    });

    const renderAt = (path) => renderWithProviders(
      <MemoryRouter initialEntries={[path]}>
        <Layout><div>content</div></Layout>
      </MemoryRouter>,
    );

    it('moves navigation into a bottom bar with a More sheet', () => {
      renderAt('/scan');

      const nav = screen.getByRole('navigation', { name: 'Main' });
      expect(within(nav).getAllByRole('link').map((link) => link.textContent))
        .toEqual(['Daily', 'Scan', 'Breadth', 'Groups']);
      expect(within(nav).getByRole('link', { name: 'Scan' })).toHaveAttribute('aria-current', 'page');
      expect(screen.queryByRole('link', { name: 'Backtest' })).not.toBeInTheDocument();

      fireEvent.click(within(nav).getByRole('button', { name: 'More' }));
      const sheet = screen.getByRole('list', { name: 'More' });
      expect(within(sheet).getByRole('link', { name: 'Backtest' })).toBeInTheDocument();
      expect(within(sheet).getByRole('button', { name: 'Default' })).toBeInTheDocument();
      expect(within(sheet).getByRole('button', { name: /mode$/ })).toBeInTheDocument();
    });

    it('highlights More when the current page lives in the sheet', () => {
      renderAt('/validation');

      const nav = screen.getByRole('navigation', { name: 'Main' });
      expect(within(nav).getByRole('button', { name: 'More' })).toHaveClass('Mui-selected');
    });

    it('collapses the runtime status to its badge but keeps the full label', () => {
      renderAt('/');

      expect(screen.queryByText('Markets ready')).not.toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Markets ready: View operations' })).toBeInTheDocument();
    });
  });
});
