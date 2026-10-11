import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider, createTheme } from '@mui/material/styles';

import StaticGroupsPage from './StaticGroupsPage';

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
        <StaticGroupsPage />
      </ThemeProvider>
    </QueryClientProvider>
  );
};

describe('StaticGroupsPage', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_STATIC_SITE', 'true');
    globalThis.fetch = vi.fn(async (url) => {
      const path = String(url).split('/static-data/')[1];

      if (path === 'manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            pages: {
              groups: {
                path: 'groups.json',
              },
            },
          }),
        };
      }

      if (path === 'groups.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            available: true,
            payload: {
              movers_period: '1w',
              rankings: {
                date: '2026-03-31',
                rankings: [
                  {
                    industry_group: 'Semiconductors',
                    rank: 1,
                    avg_rs_rating: 92.5,
                    avg_rs_rating_1d: 82.25,
                    avg_rs_rating_1w: 78.5,
                    avg_rs_rating_1m: 38.25,
                    avg_rs_rating_3m: 61.75,
                    avg_rs_rating_6m: 88.5,
                    num_stocks: 14,
                    rank_change_1w: 2,
                    rank_change_1m: 4,
                    rank_change_3m: 7,
                  },
                  {
                    industry_group: 'Retail',
                    rank: 2,
                    avg_rs_rating: 0,
                    avg_rs_rating_1d: 0,
                    avg_rs_rating_1w: null,
                    avg_rs_rating_1m: 0,
                    avg_rs_rating_3m: null,
                    avg_rs_rating_6m: 10,
                    num_stocks: 4,
                    rank_change_1w: 0,
                    rank_change_1m: 0,
                    rank_change_3m: 0,
                    rank_change_6m: 0,
                  },
                ],
              },
              movers: {
                gainers: [{ industry_group: 'Semiconductors', rank: 1, rank_change_1w: 3 }],
                losers: [{ industry_group: 'Retail', rank: 197, rank_change_1w: -5 }],
              },
            },
          }),
        };
      }

      return {
        ok: false,
        status: 404,
        json: async () => ({}),
      };
    });
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('renders 1W movers and the 1W rank-change column', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'US Group Rankings' })).toBeInTheDocument();
    expect(screen.getByText('Top Gainers (1W)')).toBeInTheDocument();
    expect(screen.getByText('Top Losers (1W)')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '1W' })).toBeInTheDocument();
    expect(screen.getAllByText('Semiconductors').length).toBeGreaterThan(0);
    expect(screen.getByText('+3')).toBeInTheDocument();
  });

  it('explains that the overall RS blends horizons up to 12 months (#305)', async () => {
    renderPage();

    const header = await screen.findByRole('columnheader', { name: 'Avg RS' });
    expect(within(header).getByRole('button')).toHaveAttribute(
      'title',
      expect.stringMatching(/up to 12 months.*sort by 1M or 3M RS/i),
    );
  });

  it('renders short-horizon RS columns with finite-value formatting', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'US Group Rankings' })).toBeInTheDocument();
    const table = (await screen.findByRole('columnheader', { name: 'Avg RS' })).closest('table');
    const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent.trim());
    expect(headers).toEqual([
      'Rank',
      'Group',
      'Avg RS',
      '1D RS',
      '1W RS',
      '1M RS',
      '3M RS',
      '6M RS',
      'Stocks',
      '1W',
      '1M',
      '3M',
      '6M',
      'Top Stock',
    ]);
    const rows = within(table).getAllByRole('row');
    expect(within(rows[1]).getAllByRole('cell')[3]).toHaveTextContent('82.3');
    expect(within(rows[1]).getAllByRole('cell')[4]).toHaveTextContent('78.5');
    expect(within(rows[1]).getAllByRole('cell')[5]).toHaveTextContent('38.3');
    expect(within(rows[1]).getAllByRole('cell')[6]).toHaveTextContent('61.8');
    expect(within(rows[1]).getAllByRole('cell')[7]).toHaveTextContent('88.5');
    expect(within(rows[2]).getAllByRole('cell')[2]).toHaveTextContent('0.0');
    expect(within(rows[2]).getAllByRole('cell')[3]).toHaveTextContent('0.0');
    expect(within(rows[2]).getAllByRole('cell')[4]).toHaveTextContent('-');
    expect(within(rows[2]).getAllByRole('cell')[7]).toHaveTextContent('10.0');
  });

  it('uses the same RS score tints in the static rankings table', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'US Group Rankings' })).toBeInTheDocument();
    const table = (await screen.findByRole('columnheader', { name: 'Avg RS' })).closest('table');
    const rows = within(table).getAllByRole('row');
    const leaderCells = within(rows[1]).getAllByRole('cell');
    const laggardCells = within(rows[2]).getAllByRole('cell');

    expect(leaderCells.slice(2, 8).map((cell) => cell.dataset.rsTone)).toEqual([
      'up-strong',
      'up-strong',
      'up-soft',
      'neutral',
      'neutral',
      'up-strong',
    ]);
    expect(laggardCells.slice(2, 8).map((cell) => cell.dataset.rsTone)).toEqual([
      'down-strong',
      'down-strong',
      'neutral',
      'down-strong',
      'neutral',
      'down-strong',
    ]);
    // Translucent success/error tints from MUI's default theme (success #2e7d32, error #d32f2f).
    const tint = (cell) => getComputedStyle(cell).backgroundColor;
    expect(tint(leaderCells[2])).toMatch(/^rgba\(46, 125, 50, /);
    expect(tint(leaderCells[4])).toMatch(/^rgba\(46, 125, 50, /);
    expect(tint(laggardCells[2])).toMatch(/^rgba\(211, 47, 47, /);
  });

  it('sorts static group rankings by RS columns', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'US Group Rankings' })).toBeInTheDocument();
    const table = screen.getByRole('columnheader', { name: 'Avg RS' }).closest('table');
    let rows = within(table).getAllByRole('row');
    expect(within(rows[1]).getAllByRole('cell')[1]).toHaveTextContent('Semiconductors');
    expect(within(rows[2]).getAllByRole('cell')[1]).toHaveTextContent('Retail');

    fireEvent.click(within(screen.getByRole('columnheader', { name: 'Avg RS' })).getByRole('button'));
    rows = within(table).getAllByRole('row');
    expect(within(rows[1]).getAllByRole('cell')[1]).toHaveTextContent('Retail');
    expect(within(rows[2]).getAllByRole('cell')[1]).toHaveTextContent('Semiconductors');

    fireEvent.click(within(screen.getByRole('columnheader', { name: 'Avg RS' })).getByRole('button'));
    rows = within(table).getAllByRole('row');
    expect(within(rows[1]).getAllByRole('cell')[1]).toHaveTextContent('Semiconductors');
    expect(within(rows[2]).getAllByRole('cell')[1]).toHaveTextContent('Retail');
  });

  it('keeps missing values below populated rank changes when sorting descending', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'US Group Rankings' })).toBeInTheDocument();
    const table = screen.getByRole('columnheader', { name: '6M' }).closest('table');

    fireEvent.click(within(screen.getByRole('columnheader', { name: '6M' })).getByRole('button'));
    fireEvent.click(within(screen.getByRole('columnheader', { name: '6M' })).getByRole('button'));

    const rows = within(table).getAllByRole('row');
    expect(within(rows[1]).getAllByRole('cell')[1]).toHaveTextContent('Retail');
    expect(within(rows[2]).getAllByRole('cell')[1]).toHaveTextContent('Semiconductors');
  });

  it('renders the RRG chart from the baked bundle when the toggle is selected', async () => {
    globalThis.fetch = vi.fn(async (url) => {
      const path = String(url).split('/static-data/')[1];
      if (path === 'manifest.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            pages: { groups: { path: 'groups.json' } },
            assets: { groups_rrg: { path: 'groups_rrg.json' } },
          }),
        };
      }
      if (path === 'groups.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            available: true,
            payload: {
              movers_period: '1w',
              rankings: { date: '2026-03-31', rankings: [] },
              movers: { gainers: [], losers: [] },
            },
          }),
        };
      }
      if (path === 'groups_rrg.json') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            available: true,
            available_scopes: ['groups'],
            payload: {
              groups: {
                date: '2026-03-31',
                scope: 'groups',
                groups: [
                  {
                    industry_group: 'Semiconductors',
                    rank: 1,
                    num_stocks: 14,
                    avg_rs_rating: 92.5,
                    quadrant: 'Leading',
                    is_provisional: false,
                    current: { date: '2026-03-31', x: 108.3, y: 106.1 },
                    tail: [
                      { date: '2026-02-01', x: 104.0, y: 98.0 },
                      { date: '2026-03-31', x: 108.3, y: 106.1 },
                    ],
                  },
                ],
              },
              sectors: { date: '2026-03-31', scope: 'sectors', groups: [] },
            },
          }),
        };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });

    renderPage();

    expect(await screen.findByRole('heading', { name: 'US Group Rankings' })).toBeInTheDocument();
    // Switch from the table view to the Relative Rotation Graph.
    fireEvent.click(screen.getByRole('tab', { name: 'RRG' }));
    expect(await screen.findByText(/Relative Rotation Graph/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sectors' })).not.toBeInTheDocument();
  });
});

 it('loads the Matrix asset independently of a failed rankings asset', async () => {
  vi.stubEnv('VITE_STATIC_SITE', 'true');
  const requested = [];
  globalThis.fetch = vi.fn(async url => {
    const path = String(url).split('/static-data/')[1]; requested.push(path);
    if (path === 'manifest.json') return {ok:true, json:async()=>({pages:{groups:{path:'groups.json'}}, assets:{groups_matrix:{path:'matrix.json'}}})};
    if (path === 'matrix.json') return {ok:true, json:async()=>({available:false, reason:'missing_ibd_mappings'})};
    return {ok:false, status:500};
  });
  renderPage();
  expect(await screen.findByRole('tab', {name:'Matrix'})).toBeInTheDocument();
  expect(requested).not.toContain('matrix.json');
  fireEvent.click(screen.getByRole('tab', {name:'Matrix'}));
  expect(await screen.findByText(/IBD classifications are not available/)).toBeInTheDocument();
  expect(requested.every(path=>Boolean(path))).toBe(true);
  vi.unstubAllEnvs();
 });
