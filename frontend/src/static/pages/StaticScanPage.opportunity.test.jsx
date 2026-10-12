import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider, createTheme } from '@mui/material/styles';

import StaticScanPage from './StaticScanPage';

const filterPanelSpy = vi.fn();
const resultsTableSpy = vi.fn();
const staticChartModalSpy = vi.fn();
const resultsTableTestState = vi.hoisted(() => ({ renderReal: false }));

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: ({ count }) => ({
    getVirtualItems: () => Array.from({ length: count }, (_, index) => ({
      index,
      start: index * 48,
      end: (index + 1) * 48,
      size: 48,
      key: index,
    })),
    getTotalSize: () => count * 48,
    measure: () => {},
  }),
}));

vi.mock('../../components/Scan/FilterPanel', () => ({
  default: (props) => {
    filterPanelSpy(props);
    return <div data-testid="filter-panel" />;
  },
}));

vi.mock('../../components/Scan/ResultsTable', async () => {
  const actual = await vi.importActual('../../components/Scan/ResultsTable');
  return {
    default: (props) => {
    resultsTableSpy(props);
    if (resultsTableTestState.renderReal) {
      const ActualResultsTable = actual.default;
      return <ActualResultsTable {...props} />;
    }
    return (
      <div>
        <div data-testid="results-table-page">{props.page}</div>
        <div data-testid="results-table-total">{props.total}</div>
        <div data-testid="results-table-rows">{props.results.map((row) => row.symbol).join(',')}</div>
        <div data-testid="results-table-actions">{props.showActions ? 'actions-visible' : 'actions-hidden'}</div>
        <button type="button" onClick={() => props.onPageChange(3)}>
          go-to-page-3
        </button>
        <button type="button" onClick={() => props.onSortChange('rating', 'asc')}>
          resort
        </button>
        <button type="button" onClick={() => props.onOpenChart?.('NVDA')}>
          open-chart
        </button>
      </div>
    );
  },
  };
});

vi.mock('../StaticChartViewerModal', () => ({
  default: (props) => {
    staticChartModalSpy(props);
    return props.open ? <div data-testid="static-chart-modal">{props.initialSymbol}</div> : null;
  },
}));

const renderPage = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider theme={createTheme()}>
        <StaticScanPage />
      </ThemeProvider>
    </QueryClientProvider>
  );
};

describe('StaticScanPage', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_STATIC_SITE', 'true');
    filterPanelSpy.mockClear();
    resultsTableSpy.mockClear();
    staticChartModalSpy.mockClear();
    resultsTableTestState.renderReal = false;
  });
  it('hides opportunity workflow for a legacy bundle without the capability', async () => {
    resultsTableTestState.renderReal = true;
    globalThis.fetch = vi.fn(async (url) => {
      const path = String(url).split('/static-data/')[1];
      if (path === 'manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ pages: { scan: { path: 'scan/manifest.json' } } }),
        };
      }
      if (path === 'scan/manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            run_id: 9,
            as_of_date: '2026-08-21',
            sort: { field: 'composite_score', order: 'desc' },
            default_page_size: 50,
            rows_total: 1,
            filter_options: {},
            initial_rows: [{ symbol: 'LEGACY', composite_score: 80, rating: 'Watch' }],
            preset_screens: [{
              id: 'correction_survivors',
              name: 'Correction Survivors',
              short_name: 'Survivors',
              filters: { correctionSurvivor: true },
              sort_by: 'resilience_score',
              sort_order: 'desc',
            }],
            chunks: [],
            charts: { available: false },
          }),
        };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });

    renderPage();

    expect(await screen.findByRole('heading', { name: 'Daily Scan' })).toBeInTheDocument();
    expect(screen.queryByText(/Survivors/)).not.toBeInTheDocument();
    expect(screen.queryByText('Res')).not.toBeInTheDocument();
    expect(screen.queryByText('Action')).not.toBeInTheDocument();
  });

  it('keeps opportunity fields in the Logic Builder for a capable static bundle', async () => {
    const user = userEvent.setup();
    globalThis.fetch = vi.fn(async (url) => {
      const path = String(url).split('/static-data/')[1];
      if (path === 'manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            features: { opportunity_state: true },
            pages: { scan: { path: 'scan/manifest.json' } },
          }),
        };
      }
      if (path === 'scan/manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            run_id: 10,
            as_of_date: '2026-08-21',
            sort: { field: 'composite_score', order: 'desc' },
            default_page_size: 50,
            rows_total: 1,
            filter_options: {},
            initial_rows: [{ symbol: 'READY', composite_score: 90 }],
            preset_screens: [],
            chunks: [],
            charts: { available: false },
          }),
        };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });

    renderPage();

    expect(await screen.findByRole('heading', { name: 'Daily Scan' })).toBeInTheDocument();
    // The heading can render before the filter panel mounts; wait for its props.
    await waitFor(() =>
      expect(filterPanelSpy.mock.lastCall?.[0]?.onOpenLogicBuilder).toBeTypeOf('function'),
    );
    act(() => filterPanelSpy.mock.lastCall[0].onOpenLogicBuilder());
    await user.click(screen.getByRole('button', { name: /add named setup/i }));
    await user.click(screen.getAllByRole('combobox')[1]);

    expect(screen.getByRole('option', { name: /Correction survivor/i })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Resilience score/i })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Action state/i })).toBeInTheDocument();
  });

  it('opens static opportunity evidence without a chart bundle and keeps mixed legacy rows non-interactive', async () => {
    resultsTableTestState.renderReal = true;
    globalThis.fetch = vi.fn(async (url) => {
      const path = String(url).split('/static-data/')[1];
      if (path === 'manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            features: { opportunity_state: true },
            pages: { scan: { path: 'scan/manifest.json' } },
          }),
        };
      }
      if (path === 'scan/manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            run_id: 10,
            as_of_date: '2026-08-21',
            sort: { field: 'resilience_score', order: 'desc' },
            default_page_size: 50,
            rows_total: 2,
            filter_options: {},
            initial_rows: [
              {
                symbol: 'READY',
                resilience_score: 84,
                action_state: 'setup_ready',
                rating: 'Leader',
                opportunity_state: {
                  market: 'US',
                  benchmark_symbol: 'SPY',
                  score_pillars: { benchmark_leadership: 20 },
                  passed_checks: ['leadership_gate'],
                },
              },
              { symbol: 'LEGACY', rating: 'Watch' },
            ],
            preset_screens: [{
              id: 'correction_survivors',
              name: 'Correction Survivors',
              short_name: 'Survivors',
              filters: { correctionSurvivor: true },
              sort_by: 'resilience_score',
              sort_order: 'desc',
            }],
            chunks: [],
            charts: { available: false },
          }),
        };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });

    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Setup Ready' }));
    expect(screen.getByRole('presentation')).toHaveTextContent('Resilience score');
    expect(screen.getByRole('presentation')).toHaveTextContent('SPY');
    expect(screen.getByText('Not computed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Not computed' })).not.toBeInTheDocument();
    expect(resultsTableSpy.mock.calls.at(-1)?.[0]).not.toHaveProperty('opportunityTelemetrySurface');
  });

  it('applies and resilience-sorts the Correction Survivors preset', async () => {
    globalThis.fetch = vi.fn(async (url) => {
      const path = String(url).split('/static-data/')[1];

      if (path === 'manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            features: { opportunity_state: true },
            pages: { scan: { path: 'scan/manifest.json' } },
          }),
        };
      }

      if (path === 'scan/manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            sort: { field: 'composite_score', order: 'desc' },
            default_page_size: 50,
            rows_total: 4,
            filter_options: {},
            initial_rows: [
              { symbol: 'BETA', correction_survivor: true, resilience_score: 84, composite_score: 70 },
              { symbol: 'ALPHA', correction_survivor: true, resilience_score: 84, composite_score: 60 },
              { symbol: 'HIGH', correction_survivor: true, resilience_score: 95, composite_score: 50 },
              { symbol: 'OUT', correction_survivor: false, resilience_score: 99, composite_score: 99 },
            ],
            preset_screens: [{
              id: 'correction_survivors',
              name: 'Correction Survivors',
              short_name: 'Survivors',
              filters: { correctionSurvivor: true },
              sort_by: 'resilience_score',
              sort_order: 'desc',
            }],
            chunks: [],
            charts: { path: 'charts/index.json', available: false },
          }),
        };
      }

      if (path === 'charts/index.json') {
        return { ok: true, status: 200, json: async () => ({ symbols: [] }) };
      }

      return { ok: false, status: 404, json: async () => ({}) };
    });

    renderPage();

    const user = userEvent.setup();
    await user.click(await screen.findByText('Survivors (3)'));

    await waitFor(() => {
      expect(screen.getByTestId('results-table-rows')).toHaveTextContent('HIGH,ALPHA,BETA');
      expect(screen.getByTestId('results-table-rows')).not.toHaveTextContent('OUT');
    });
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });
});
