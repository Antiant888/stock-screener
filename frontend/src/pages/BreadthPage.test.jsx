import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import BreadthPage from './BreadthPage';
import MarketSelector from '../components/Layout/MarketSelector';
import { MarketProvider } from '../contexts/MarketContext';
import * as breadthApi from '../api/breadth';
import * as stocksApi from '../api/stocks';
import { renderWithProviders } from '../test/renderWithProviders';

// Render the page alongside the global header market selector, mirroring the
// app layout (per-page market selectors were replaced by MarketSelector).
function renderBreadthPage() {
  // Fresh elements per render: reusing one element object would let React
  // bail out of re-rendering the subtree on rerender.
  const buildUi = () => (
    <MemoryRouter>
      <MarketProvider>
        <MarketSelector />
        <BreadthPage />
      </MarketProvider>
    </MemoryRouter>
  );
  const view = renderWithProviders(buildUi());
  return { ...view, rerenderPage: () => view.rerender(buildUi()) };
}

const runtimeState = {
  runtimeReady: true,
  primaryMarket: 'HK',
  enabledMarkets: ['US', 'HK'],
  supportedMarkets: ['US', 'HK', 'IN', 'JP', 'KR', 'TW', 'CN', 'CA', 'DE', 'SG', 'AU', 'MY'],
  marketCatalog: {
    markets: [
      { code: 'US', label: 'United States', capabilities: { breadth: true } },
      { code: 'HK', label: 'Hong Kong', capabilities: { breadth: true } },
      { code: 'IN', label: 'India', capabilities: { breadth: true } },
      { code: 'JP', label: 'Japan', capabilities: { breadth: true } },
      { code: 'KR', label: 'South Korea', capabilities: { breadth: true } },
      { code: 'TW', label: 'Taiwan', capabilities: { breadth: true } },
      { code: 'CN', label: 'China A-shares', capabilities: { breadth: true } },
      { code: 'CA', label: 'Canada', capabilities: { breadth: true } },
      { code: 'DE', label: 'Germany', capabilities: { breadth: true } },
      { code: 'SG', label: 'Singapore', capabilities: { breadth: false } },
      { code: 'AU', label: 'Australia', capabilities: { breadth: false } },
      { code: 'MY', label: 'Malaysia', capabilities: { breadth: false } },
    ],
  },
};

vi.mock('../contexts/RuntimeContext', () => ({
  useRuntime: () => runtimeState,
}));

vi.mock('../components/Charts/BreadthChart', () => ({
  default: ({ breadthData, benchmarkLabel, spyData, error }) => (
    <div data-testid="breadth-chart" data-error={error?.message || ''}>
      {benchmarkLabel}:{breadthData?.length ?? 0}:{spyData?.length ?? 0}
    </div>
  ),
}));

vi.mock('../api/breadth', () => ({
  getBreadthBootstrap: vi.fn(),
  getBreadthContributorIndex: vi.fn(),
  getBreadthContributors: vi.fn(),
  getCurrentBreadth: vi.fn(),
  getHistoricalBreadth: vi.fn(),
  getBreadthSummary: vi.fn(),
}));

vi.mock('../api/stocks', () => ({
  getPriceHistory: vi.fn(),
}));

function breadthRow(market = 'HK') {
  return {
    market,
    date: '2026-04-24',
    stocks_up_4pct: market === 'HK' ? 22 : 10,
    stocks_down_4pct: market === 'HK' ? 8 : 4,
    ratio_5day: 2.75,
    ratio_10day: 2.5,
    stocks_up_25pct_quarter: 30,
    stocks_down_25pct_quarter: 12,
    stocks_up_25pct_month: 24,
    stocks_down_25pct_month: 10,
    stocks_up_50pct_month: 6,
    stocks_down_50pct_month: 2,
    stocks_up_13pct_34days: 18,
    stocks_down_13pct_34days: 7,
    total_stocks_scanned: 30,
    advancing_count: 20,
    declining_count: 8,
    unchanged_count: 2,
    advance_decline_eligible_count: 30,
    new_high_52week_count: 5,
    new_low_52week_count: 1,
    high_low_52week_eligible_count: 25,
    t2108_count: 55,
    t2108_pct: 57.89,
    t2108_eligible_count: 95,
    atr_10x_extension_count: 3,
    atr_extension_eligible_count: 28,
    broad_universe_count: 32,
    calculation_revision: 3,
    calculation_duration_seconds: 1.25,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  runtimeState.runtimeReady = true;
  runtimeState.primaryMarket = 'HK';
  runtimeState.enabledMarkets = ['US', 'HK'];
  runtimeState.supportedMarkets = ['US', 'HK', 'IN', 'JP', 'KR', 'TW', 'CN', 'CA', 'DE', 'SG', 'AU', 'MY'];

  breadthApi.getCurrentBreadth.mockImplementation((market = 'US') => Promise.resolve(breadthRow(market)));
  breadthApi.getHistoricalBreadth.mockImplementation((startDate, endDate, limit, market = 'US') => (
    Promise.resolve([breadthRow(market)])
  ));
  breadthApi.getBreadthSummary.mockImplementation((market = 'US') => Promise.resolve({
    market,
    latest_date: '2026-04-24',
    total_records: 1,
    date_range_start: '2026-04-24',
    date_range_end: '2026-04-24',
  }));
  breadthApi.getBreadthBootstrap.mockRejectedValue(new Error('no snapshot'));
  breadthApi.getBreadthContributorIndex.mockImplementation((market = 'US') => Promise.resolve({
    schema: 'breadth-contributors-v1',
    market,
    calculation_revision: 3,
    dates: [],
  }));
  breadthApi.getBreadthContributors.mockResolvedValue(null);
  stocksApi.getPriceHistory.mockResolvedValue([]);
});

describe('BreadthPage', () => {
  it('loads one contributor date lazily and opens the shared dialog', async () => {
    const user = userEvent.setup();
    breadthApi.getHistoricalBreadth.mockImplementation((startDate, endDate, limit, market = 'US') => (
      Promise.resolve([{ ...breadthRow(market), stocks_up_4pct: 1 }])
    ));
    breadthApi.getBreadthContributorIndex.mockResolvedValue({
      schema: 'breadth-contributors-v1',
      market: 'HK',
      calculation_revision: 3,
      dates: ['2026-04-24'],
    });
    breadthApi.getBreadthContributors.mockResolvedValue({
      schema: 'breadth-contributors-v1',
      market: 'HK',
      date: '2026-04-24',
      calculation_revision: 3,
      contributors: [{
        symbol: '0700',
        company_name: 'Tencent',
        ibd_industry_group: 'Internet Content',
        daily_change_pct: 5,
        signals: { up_4pct: 5 },
      }],
    });

    renderBreadthPage();
    await screen.findByText('04/24/26');
    const button = await screen.findByRole('button', {
      name: 'View 1 contributing stocks for Stocks Up 4%+',
    });
    expect(breadthApi.getBreadthContributors).not.toHaveBeenCalled();
    await user.click(button);

    expect(await screen.findByRole('dialog', { name: /Stocks Up 4%\+.*1 stocks/i })).toBeInTheDocument();
    expect(await screen.findByText('Tencent')).toBeInTheDocument();
    expect(breadthApi.getBreadthContributors).toHaveBeenCalledTimes(1);
    expect(breadthApi.getBreadthContributors).toHaveBeenCalledWith('HK', '2026-04-24');
  });

  it('renders contributor 409 responses as an inconsistent snapshot', async () => {
    const user = userEvent.setup();
    breadthApi.getHistoricalBreadth.mockImplementation((startDate, endDate, limit, market = 'US') => (
      Promise.resolve([{ ...breadthRow(market), stocks_up_4pct: 1 }])
    ));
    breadthApi.getBreadthContributorIndex.mockResolvedValue({
      schema: 'breadth-contributors-v1',
      market: 'HK',
      calculation_revision: 3,
      dates: ['2026-04-24'],
    });
    breadthApi.getBreadthContributors.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Contributor count mismatch for stocks_up_4pct' },
      },
    });

    renderBreadthPage();
    await screen.findByText('04/24/26');
    await user.click(await screen.findByRole('button', {
      name: 'View 1 contributing stocks for Stocks Up 4%+',
    }));

    expect(await screen.findByText(
      'Contributor count mismatch for stocks_up_4pct',
    )).toBeInTheDocument();
    expect(screen.queryByText(/Could not load contributors/i)).not.toBeInTheDocument();
  });

  it('explains the purpose of each recent-history indicator group', async () => {
    renderBreadthPage();

    expect(await screen.findByText(
      'Primary tracks daily movers and ratios; secondary tracks trend windows; context adds T2108, ATR extension, and universe size.',
    )).toBeInTheDocument();
  });

  it('renders the latest date without shifting a date-only value', async () => {
    vi.stubEnv('TZ', 'America/Los_Angeles');
    try {
      renderBreadthPage();

      expect(await screen.findByText('Apr 24, 2026')).toBeInTheDocument();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it('defaults breadth requests to the runtime primary market', async () => {
    renderBreadthPage();

    expect(await screen.findByText('Latest Breadth Data')).toBeInTheDocument();
    expect(await screen.findByText('57.89% (55 / 95)')).toBeInTheDocument();

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('HK');
      expect(breadthApi.getBreadthSummary).toHaveBeenCalledWith('HK');
      expect(stocksApi.getPriceHistory).toHaveBeenCalledWith('2800.HK', '1mo');
    });
  });

  it('supports Korea as a runtime primary market', async () => {
    runtimeState.primaryMarket = 'KR';
    runtimeState.enabledMarkets = ['KR', 'US'];

    renderBreadthPage();

    expect(await screen.findByText('Latest Breadth Data')).toBeInTheDocument();

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('KR');
      expect(breadthApi.getBreadthSummary).toHaveBeenCalledWith('KR');
      expect(stocksApi.getPriceHistory).toHaveBeenCalledWith('069500.KS', '1mo');
    });
    expect(screen.getByRole('combobox', { name: /market/i })).toHaveTextContent('KR');
  });

  it('does not request unsupported Australia breadth when AU is enabled', async () => {
    runtimeState.primaryMarket = 'AU';
    runtimeState.enabledMarkets = ['AU', 'US'];

    renderBreadthPage();

    expect(await screen.findByText('Latest Breadth Data')).toBeInTheDocument();

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('US');
      expect(breadthApi.getBreadthSummary).toHaveBeenCalledWith('US');
      expect(stocksApi.getPriceHistory).toHaveBeenCalledWith('SPY', '1mo');
    });
    expect(breadthApi.getCurrentBreadth).not.toHaveBeenCalledWith('AU');
    // Header selector shows the global selection (AU); the page falls back
    // to a breadth-capable market for its data requests.
    expect(screen.getByRole('combobox', { name: /market/i })).toHaveTextContent('AU');
  });

  it('resyncs the default market when runtime primary market data loads late', async () => {
    runtimeState.primaryMarket = 'US';
    runtimeState.enabledMarkets = ['US'];

    const { rerenderPage } = renderBreadthPage();

    expect(await screen.findByText('Latest Breadth Data')).toBeInTheDocument();
    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('US');
    });

    runtimeState.primaryMarket = 'HK';
    runtimeState.enabledMarkets = ['US', 'HK'];
    rerenderPage();

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('HK');
      expect(stocksApi.getPriceHistory).toHaveBeenCalledWith('2800.HK', '1mo');
    });
  });

  it('resyncs to the runtime primary market when market options load late', async () => {
    runtimeState.primaryMarket = 'HK';
    runtimeState.enabledMarkets = ['US'];

    const { rerenderPage } = renderBreadthPage();

    expect(await screen.findByText('Latest Breadth Data')).toBeInTheDocument();
    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('US');
    });

    runtimeState.enabledMarkets = ['US', 'HK'];
    rerenderPage();

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('HK');
      expect(stocksApi.getPriceHistory).toHaveBeenCalledWith('2800.HK', '1mo');
    });
  });

  it('seeds bootstrap benchmark overlay under the live benchmark query key', async () => {
    breadthApi.getBreadthBootstrap.mockResolvedValue({
      is_stale: false,
      payload: {
        current: breadthRow('HK'),
        history_90d: [breadthRow('HK')],
        summary: { market: 'HK', total_records: 1 },
        chart_range: '1M',
        chart_data: [breadthRow('HK')],
        benchmark_symbol: '^HSI',
        benchmark_overlay: [{ date: '2026-04-24', close: 18400 }],
      },
    });

    renderBreadthPage();

    const chart = await screen.findByTestId('breadth-chart');
    await waitFor(() => {
      expect(chart).toHaveTextContent('2800.HK:1:1');
    });
    expect(stocksApi.getPriceHistory).not.toHaveBeenCalledWith('^HSI', '1mo');
  });

  it('renders breadth data when the optional benchmark overlay request fails', async () => {
    stocksApi.getPriceHistory.mockRejectedValue(new Error('benchmark unavailable'));

    renderBreadthPage();

    await waitFor(() => expect(screen.getByTestId('breadth-chart')).toHaveTextContent('2800.HK:1'));
    expect(screen.getByTestId('breadth-chart')).toHaveAttribute('data-error', '');
  });

  it('refetches breadth data when the selected market changes', async () => {
    const user = userEvent.setup();

    renderBreadthPage();

    const marketSelect = await screen.findByRole('combobox', { name: /market/i });
    fireEvent.mouseDown(marketSelect);
    await user.click(await screen.findByRole('option', { name: /United States/i }));

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('US');
      expect(breadthApi.getHistoricalBreadth).toHaveBeenCalledWith(
        expect.any(String),
        expect.any(String),
        730,
        'US',
      );
      expect(stocksApi.getPriceHistory).toHaveBeenCalledWith('SPY', '1mo');
    });
  });

  it('keeps the market selector available when the selected market has no breadth rows', async () => {
    const user = userEvent.setup();
    breadthApi.getCurrentBreadth.mockImplementation((market = 'US') => (
      market === 'HK'
        ? Promise.reject(new Error('No breadth data available for market HK.'))
        : Promise.resolve(breadthRow(market))
    ));

    renderBreadthPage();

    expect(
      await screen.findByText('Error loading HK breadth data: No breadth data available for market HK.')
    ).toBeInTheDocument();

    const marketSelect = screen.getByRole('combobox', { name: /market/i });
    fireEvent.mouseDown(marketSelect);
    await user.click(await screen.findByRole('option', { name: /United States/i }));

    await waitFor(() => {
      expect(breadthApi.getCurrentBreadth).toHaveBeenCalledWith('US');
    });
    expect(await screen.findByText('Latest Breadth Data')).toBeInTheDocument();
  });
});
