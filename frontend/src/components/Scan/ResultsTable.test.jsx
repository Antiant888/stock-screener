import { cleanup, fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '../../test/renderWithProviders';
import {
  fullSeRow,
  legacyOpportunityRow,
  mixedSeRow,
  nullSeRow,
  opportunityRow,
} from '../../test/fixtures/setupEngineFixtures';
import ResultsTable from './ResultsTable';

/*
 * Module mocks — required because:
 * - @tanstack/react-virtual: useVirtualizer depends on DOM layout measurements
 *   (getBoundingClientRect, scrollHeight) that jsdom doesn't implement -> zero rows render
 * - RSSparkline/PriceSparkline: Recharts ResponsiveContainer requires real DOM dimensions
 * - AddToWatchlistMenu: uses React Query (QueryClientProvider) which is not needed here
 */
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: ({ count }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, i) => ({
        index: i,
        start: i * 32,
        end: (i + 1) * 32,
        size: 32,
        key: i,
      })),
    getTotalSize: () => count * 32,
    measure: () => {},
  }),
}));
vi.mock('./RSSparkline', () => ({
  default: ({ data, trend }) => (
    <span data-testid="rs-sparkline">RS:{data?.length ?? 'none'}:{trend}</span>
  ),
}));
vi.mock('./PriceSparkline', () => ({
  default: ({ data, trend, change1d }) => (
    <span data-testid="price-sparkline">Price:{data?.length ?? 'none'}:{trend}:{change1d}</span>
  ),
}));
vi.mock('../common/AddToWatchlistMenu', () => ({
  default: ({ symbols }) => <span data-testid={`watchlist-${symbols}`} />,
}));

/** Default props for a basic render — 1 row, page 1. */
const defaultProps = {
  results: [fullSeRow],
  total: 1,
  page: 1,
  perPage: 25,
  sortBy: 'composite_score',
  sortOrder: 'desc',
  onPageChange: vi.fn(),
  onPerPageChange: vi.fn(),
  onSortChange: vi.fn(),
  onOpenChart: vi.fn(),
  loading: false,
  showOpportunityState: true,
};

describe('ResultsTable', () => {
  it('rerenders a retained row when matched setups change', () => {
    const firstRow = {
      ...fullSeRow,
      matched_groups: [{ id: 'growth', name: 'Growth' }],
    };
    const { rerender } = renderWithProviders(
      <ResultsTable {...defaultProps} results={[firstRow]} />,
    );
    expect(screen.getByText('Growth')).toBeInTheDocument();

    rerender(
      <ResultsTable
        {...defaultProps}
        results={[{
          ...firstRow,
          matched_groups: [{ id: 'momentum', name: 'Momentum' }],
        }]}
      />,
    );

    expect(screen.queryByText('Growth')).not.toBeInTheDocument();
    expect(screen.getByText('Momentum')).toBeInTheDocument();
  });

  describe('opportunity state columns', () => {
    // Catches badge click bubbling into the existing row chart action or a drawer coupled to chart data.
    it('opens opportunity evidence without opening the chart', async () => {
      const onOpenChart = vi.fn();
      const user = userEvent.setup();
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[opportunityRow]}
          onOpenChart={onOpenChart}
        />,
      );

      await user.click(screen.getByRole('button', { name: 'Setup Ready' }));

      expect(screen.getByRole('presentation')).toHaveTextContent('Resilience score');
      expect(screen.getByRole('presentation')).toHaveTextContent('84.0');
      expect(onOpenChart).not.toHaveBeenCalled();
    });

    it('reconciles selected evidence to current rows and never reopens a cleared selection', async () => {
      const user = userEvent.setup();
      const { rerender } = renderWithProviders(
        <ResultsTable {...defaultProps} results={[opportunityRow]} />,
      );
      await user.click(screen.getByRole('button', { name: 'Setup Ready' }));
      expect(screen.getByRole('presentation')).toHaveTextContent('84.0');

      rerender(
        <ResultsTable
          {...defaultProps}
          results={[{ ...opportunityRow, resilience_score: 79.0 }]}
        />,
      );
      expect(screen.getByRole('presentation')).toHaveTextContent('79.0');
      expect(screen.getByRole('presentation')).not.toHaveTextContent('84.0');

      rerender(
        <ResultsTable
          {...defaultProps}
          results={[{ ...opportunityRow, symbol: 'NEW' }]}
        />,
      );
      expect(screen.queryByRole('heading', { name: 'Opportunity evidence' })).not.toBeInTheDocument();

      rerender(<ResultsTable {...defaultProps} results={[opportunityRow]} />);
      expect(screen.queryByRole('heading', { name: 'Opportunity evidence' })).not.toBeInTheDocument();
    });

    it('clears evidence selection when capability is disabled', async () => {
      const user = userEvent.setup();
      const { rerender } = renderWithProviders(
        <ResultsTable {...defaultProps} results={[opportunityRow]} />,
      );
      await user.click(screen.getByRole('button', { name: 'Setup Ready' }));

      rerender(
        <ResultsTable
          {...defaultProps}
          results={[opportunityRow]}
          showOpportunityState={false}
        />,
      );
      rerender(<ResultsTable {...defaultProps} results={[opportunityRow]} />);

      expect(screen.queryByRole('heading', { name: 'Opportunity evidence' })).not.toBeInTheDocument();
    });

    // Catches legacy rows becoming misleadingly actionable when compact evidence was never persisted.
    it('renders legacy rows as non-interactive Not computed', () => {
      renderWithProviders(
        <ResultsTable {...defaultProps} results={[legacyOpportunityRow]} />,
      );

      expect(screen.getByText('Not computed')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Not computed' })).not.toBeInTheDocument();
    });

    // Catches capability gating that leaves headers/cells behind and a width increase when disabled.
    it('hides opportunity columns and preserves the legacy table width when disabled', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[opportunityRow]}
          showOpportunityState={false}
        />,
      );

      expect(screen.queryByText('Res')).not.toBeInTheDocument();
      expect(screen.queryByText('Action')).not.toBeInTheDocument();
      expect(screen.queryByText('Setup Ready')).not.toBeInTheDocument();
      expect(screen.getByRole('table')).toHaveStyle({ minWidth: '2673px' });
    });

    // Catches wrong column placement, widths, or missing server-sort integration.
    it('renders adjacent sortable 48px resilience and 105px action columns', async () => {
      const onSortChange = vi.fn();
      const user = userEvent.setup();
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[opportunityRow]}
          onSortChange={onSortChange}
        />,
      );

      const headers = screen.getAllByRole('columnheader');
      const resilienceHeader = screen.getByText('Res').closest('th');
      const actionHeader = screen.getByText('Action').closest('th');
      const pivotHeader = screen.getByText('Pvt$').closest('th');
      expect(headers.indexOf(resilienceHeader)).toBe(headers.indexOf(pivotHeader) + 1);
      expect(headers.indexOf(actionHeader)).toBe(headers.indexOf(resilienceHeader) + 1);
      expect(resilienceHeader).toHaveStyle({ width: '48px', minWidth: '48px', maxWidth: '48px' });
      expect(actionHeader).toHaveStyle({ width: '105px', minWidth: '105px', maxWidth: '105px' });
      expect(screen.getByRole('table')).toHaveStyle({ minWidth: '2826px' });

      await user.click(screen.getByText('Res'));
      expect(onSortChange).toHaveBeenCalledWith('resilience_score', 'asc');
      await user.click(screen.getByText('Action'));
      expect(onSortChange).toHaveBeenCalledWith('action_state', 'asc');
    });

    // Catches the virtual-row comparator retaining stale top-level and nested opportunity evidence.
    it('rerenders retained rows when action state and nested evidence change', async () => {
      const user = userEvent.setup();
      const { rerender } = renderWithProviders(
        <ResultsTable {...defaultProps} results={[opportunityRow]} />,
      );

      rerender(
        <ResultsTable
          {...defaultProps}
          results={[{
            ...opportunityRow,
            resilience_score: 91,
            action_state: 'watch',
            opportunity_state: {
              ...opportunityRow.opportunity_state,
              benchmark_symbol: 'QQQ',
            },
          }]}
        />,
      );

      expect(screen.queryByText('Setup Ready')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Watch' })).toBeInTheDocument();
      expect(screen.getByText('91.0')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Watch' }));
      expect(screen.getByRole('presentation')).toHaveTextContent('QQQ');
    });
  });

  // ── SE column rendering — full data ──────────────────────────────────
  describe('SE column rendering — full data', () => {
    beforeEach(() => {
      renderWithProviders(<ResultsTable {...defaultProps} results={[fullSeRow]} />);
    });

    it('renders se_setup_score as 78.3', () => {
      expect(screen.getByText('78.3')).toBeInTheDocument();
    });

    it('renders se_pattern_primary as cup_with_handle', () => {
      expect(screen.getByText('cup_with_handle')).toBeInTheDocument();
    });

    it('renders se_distance_to_pivot_pct as -3.2%', () => {
      expect(screen.getByText('-3.2%')).toBeInTheDocument();
    });

    it('renders se_bb_width_pctile_252 as 15', () => {
      expect(screen.getByText('15')).toBeInTheDocument();
    });

    it('renders se_volume_vs_50d as 1.8x', () => {
      expect(screen.getByText('1.8x')).toBeInTheDocument();
    });

    it('renders CheckIcon for se_rs_line_new_high=true', () => {
      // Multiple boolean columns render CheckIcon (ma_alignment, passes_template, se_rs_line_new_high).
      // fullSeRow has ma_alignment=true, passes_template=true, se_rs_line_new_high=true → 3 CheckIcons.
      const checkIcons = screen.getAllByTestId('CheckIcon');
      expect(checkIcons.length).toBe(3);
    });

    it('renders se_pivot_price as $198.50', () => {
      expect(screen.getByText('$198.50')).toBeInTheDocument();
    });

    it('renders first-class RS blue dot with the new-high date tooltip', () => {
      expect(screen.getByTitle('RS blue dot: latest RS high 2026-01-06')).toBeInTheDocument();
    });
  });

  // ── SE column rendering — null data ──────────────────────────────────
  describe('SE column rendering — null data', () => {
    it('renders dash for all 7 SE columns when null', () => {
      renderWithProviders(<ResultsTable {...defaultProps} results={[nullSeRow]} />);
      // The table has many '-' dashes (other null columns too). We verify
      // by checking that none of the SE-specific formatted values appear.
      expect(screen.queryByText('78.3')).not.toBeInTheDocument();
      expect(screen.queryByText('cup_with_handle')).not.toBeInTheDocument();
      expect(screen.queryByText('-3.2%')).not.toBeInTheDocument();
      expect(screen.queryByText('1.8x')).not.toBeInTheDocument();
      expect(screen.queryByText('$198.50')).not.toBeInTheDocument();
      // No CheckIcon should appear for se_rs_line_new_high=null
      // (other booleans like ma_alignment still render icons)
    });
  });

  // ── SE column rendering — mixed data ─────────────────────────────────
  describe('SE column rendering — mixed data', () => {
    beforeEach(() => {
      renderWithProviders(<ResultsTable {...defaultProps} results={[mixedSeRow]} />);
    });

    it('renders CloseIcon for se_rs_line_new_high=false', () => {
      // mixedSeRow has se_rs_line_new_high=false, vcp_detected=false, vcp_ready_for_breakout=false → 3 CloseIcons.
      const closeIcons = screen.getAllByTestId('CloseIcon');
      expect(closeIcons.length).toBe(3);
    });

    it('renders populated SE values alongside dashes for null ones', () => {
      expect(screen.getByText('62.1')).toBeInTheDocument();
      expect(screen.getByText('4.7%')).toBeInTheDocument();
      expect(screen.getByText('2.3x')).toBeInTheDocument();
    });
  });

  describe('young IPO partial metrics', () => {
    it('renders calculable short-history values while long-history values stay blank', () => {
      const youngIpoRow = {
        ...nullSeRow,
        symbol: 'NEWIPO',
        company_name: 'New IPO Inc.',
        composite_score: null,
        rating: 'Insufficient Data',
        data_status: 'insufficient_history',
        scan_mode: 'listing_only',
        is_scannable: false,
        history_bars: 45,
        rs_sparkline_data: Array.from({ length: 30 }, (_, index) => 1 + index / 100),
        rs_trend: 1,
        price_sparkline_data: Array.from({ length: 30 }, (_, index) => 1 + index / 200),
        price_trend: 1,
        price_change_1d: 2.5,
        adr_percent: 10.0,
        rs_rating_1m: 50.0,
        rs_rating: null,
        rs_rating_3m: null,
        rs_rating_12m: null,
        stage: null,
        ma_alignment: null,
      };

      renderWithProviders(<ResultsTable {...defaultProps} results={[youngIpoRow]} />);

      expect(screen.getByText('New IPO')).toBeInTheDocument();
      expect(screen.getByText('RS:30:1')).toBeInTheDocument();
      expect(screen.getByText('Price:30:1:2.5')).toBeInTheDocument();
      expect(screen.getByText('50')).toBeInTheDocument();
      expect(screen.getByText('10.0%')).toBeInTheDocument();
      expect(screen.queryByText('S2')).not.toBeInTheDocument();
    });
  });

  // ── SE column headers ────────────────────────────────────────────────
  describe('SE column headers', () => {
    it('renders all 7 SE header labels', () => {
      renderWithProviders(<ResultsTable {...defaultProps} />);
      const headers = ['SE', 'Pat', 'Pvt%', 'Sqz', 'V50', 'RSH', 'SEBD', 'RSBD', 'Pvt$'];
      headers.forEach((label) => {
        expect(screen.getByText(label)).toBeInTheDocument();
      });
    });

    it('renders a readable IBD industry group column', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[{ ...fullSeRow, ibd_industry_group: 'Semiconductors' }]}
        />
      );

      expect(screen.getByText('IBD Industry')).toBeInTheDocument();
      expect(screen.getByText('Semiconductors')).toBeInTheDocument();
    });

    it('renders a compact market themes column', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[{
            ...fullSeRow,
            ibd_industry_group: 'Semiconductors',
            market_themes: ['AI Infrastructure', 'Foundry'],
          }]}
        />
      );

      expect(screen.getByText('Themes')).toBeInTheDocument();
      // Compact variant: first theme renders as a chip, with a +N counter for overflow.
      expect(screen.getByText('AI Infrastructure')).toBeInTheDocument();
      expect(screen.getByText('+1')).toBeInTheDocument();
    });

    it('keeps group rank compact and exposes the rank date in a tooltip', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[{
            ...fullSeRow,
            ibd_group_rank: 153,
            ibd_group_rank_date: '2026-06-14',
          }]}
        />
      );

      const rankCell = screen.getByText('153').closest('td');
      expect(rankCell).toHaveAttribute('title', 'Group rank 153 as of Jun 14, 2026');
      expect(screen.queryByText(/Jun 14, 2026/)).not.toBeInTheDocument();
    });
  });

  // ── structural ───────────────────────────────────────────────────────
  describe('structural', () => {
    it('shows "No results found" when results is empty', () => {
      renderWithProviders(
        <ResultsTable {...defaultProps} results={[]} total={0} />
      );
      expect(screen.getByText('No results found')).toBeInTheDocument();
    });

    it('shows a table-shaped loading placeholder when loading=true', () => {
      renderWithProviders(
        <ResultsTable {...defaultProps} loading={true} />
      );
      expect(screen.getByRole('status')).toHaveTextContent('Loading scan results');
    });

    it('renders pagination controls', () => {
      renderWithProviders(
        <ResultsTable {...defaultProps} results={[fullSeRow]} total={50} />
      );
      // MUI TablePagination renders "Rows per page:" text
      expect(screen.getByText(/rows per page/i)).toBeInTheDocument();
    });

    it('rerenders when showActions changes so the action column is removed', () => {
      const results = [fullSeRow];
      const { rerender } = renderWithProviders(
        <ResultsTable {...defaultProps} results={results} showActions={true} />
      );

      expect(screen.getByTestId('ShowChartIcon')).toBeInTheDocument();

      rerender(<ResultsTable {...defaultProps} results={results} showActions={false} />);

      expect(screen.queryByTestId('ShowChartIcon')).not.toBeInTheDocument();
    });

    it('keeps the action column but hides the chart button when a row is not chart-enabled', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[fullSeRow]}
          showActions={true}
          showWatchlistMenu={false}
          isChartEnabled={() => false}
        />
      );

      expect(screen.queryByTestId('ShowChartIcon')).not.toBeInTheDocument();
      expect(screen.getByText('FULL')).toBeInTheDocument();
    });

    it('renders a young-IPO status chip and suppresses chart actions for non-scannable rows', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[{
            ...fullSeRow,
            symbol: '0100.HK',
            company_name: 'MINIMAX-W',
            scan_mode: 'listing_only',
            data_status: 'insufficient_history',
            is_scannable: false,
          }]}
          showActions={true}
          showWatchlistMenu={false}
          isChartEnabled={() => true}
        />
      );

      expect(screen.getByText('New IPO')).toBeInTheDocument();
      expect(screen.queryByTestId('ShowChartIcon')).not.toBeInTheDocument();
    });

    it('does not relabel generic error rows as young IPOs', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          results={[{
            ...fullSeRow,
            symbol: 'BROKEN',
            rating: 'Error',
            scan_mode: 'listing_only',
            data_status: 'error',
            is_scannable: false,
          }]}
          showActions={true}
          showWatchlistMenu={false}
          isChartEnabled={() => true}
        />
      );

      expect(screen.queryByText('New IPO')).not.toBeInTheDocument();
      expect(screen.getByText('Error')).toBeInTheDocument();
      expect(screen.queryByTestId('ShowChartIcon')).not.toBeInTheDocument();
    });
  });

  // ── interactions ─────────────────────────────────────────────────────
  describe('interactions', () => {
    it('calls onOpenChart when row is clicked', async () => {
      const onOpenChart = vi.fn();
      renderWithProviders(
        <ResultsTable {...defaultProps} onOpenChart={onOpenChart} results={[fullSeRow]} />
      );

      fireEvent.click(screen.getByText('FULL'));
      expect(onOpenChart).toHaveBeenCalledWith('FULL');
    });

    it('calls onSortChange when a sortable header is clicked', async () => {
      const onSortChange = vi.fn();
      renderWithProviders(
        <ResultsTable {...defaultProps} onSortChange={onSortChange} />
      );

      const user = userEvent.setup();
      // Click the "SE" header (sortable)
      await user.click(screen.getByText('SE'));
      expect(onSortChange).toHaveBeenCalledWith('se_setup_score', 'asc');
    });

    it('toggles sort direction when same header is clicked twice', async () => {
      const onSortChange = vi.fn();
      // Start sorted by se_setup_score asc
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          sortBy="se_setup_score"
          sortOrder="asc"
          onSortChange={onSortChange}
        />
      );

      const user = userEvent.setup();
      await user.click(screen.getByText('SE'));
      // Since current is asc, clicking again should flip to desc
      expect(onSortChange).toHaveBeenCalledWith('se_setup_score', 'desc');
    });
  });

  it('fills the stage badge from the theme, overriding the neutral default', () => {
    renderWithProviders(<ResultsTable {...defaultProps} />);

    // Stage 2 = success.main in the dark test theme (#2ebd85).
    expect(screen.getByText('S2')).toHaveStyle({ backgroundColor: 'rgb(46, 189, 133)' });
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
    });

    afterEach(() => {
      delete window.matchMedia;
    });

    it('lists results as cards with a sort picker instead of a wide table', async () => {
      const onSortChange = vi.fn();
      const onOpenChart = vi.fn();
      renderWithProviders(
        <ResultsTable {...defaultProps} onSortChange={onSortChange} onOpenChart={onOpenChart} />,
      );

      expect(screen.queryByRole('table')).not.toBeInTheDocument();
      const list = screen.getByRole('list', { name: 'Scan results' });
      expect(within(list).getAllByRole('listitem')).toHaveLength(1);

      fireEvent.click(screen.getByRole('button', { name: `Open ${fullSeRow.symbol} chart` }));
      expect(onOpenChart).toHaveBeenCalledWith(fullSeRow.symbol);

      fireEvent.click(screen.getByRole('button', { name: /Sorted descending/ }));
      expect(onSortChange).toHaveBeenLastCalledWith('composite_score', 'asc');

      await userEvent.click(screen.getByRole('combobox', { name: 'Sort by' }));
      await userEvent.click(screen.getByRole('option', { name: 'RS rating' }));
      expect(onSortChange).toHaveBeenLastCalledWith('rs_rating', 'desc');
    });

    it('hides the sort picker when sorting is disabled and skips unscannable charts', () => {
      renderWithProviders(
        <ResultsTable
          {...defaultProps}
          sortingEnabled={false}
          results={[{ ...fullSeRow, is_scannable: false }]}
          onOpenChart={vi.fn()}
        />,
      );

      expect(screen.queryByRole('combobox', { name: 'Sort by' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^Open .* chart$/ })).not.toBeInTheDocument();
      expect(screen.getByRole('listitem')).toHaveAttribute('aria-setsize', '1');
    });

    it('keeps the scan status or rating as the card badge without opportunity state', () => {
      const ipoRow = {
        ...fullSeRow,
        symbol: 'NEWCO',
        scan_mode: 'listing_only',
        data_status: 'insufficient_history',
        is_scannable: false,
      };
      const ratedRow = { ...fullSeRow, symbol: 'RATED', rating: 'Strong Buy' };
      renderWithProviders(
        <ResultsTable {...defaultProps} showOpportunityState={false} results={[ipoRow, ratedRow]} />,
      );

      const [ipoCard, ratedCard] = screen.getAllByRole('listitem');
      expect(within(ipoCard).getByText('New IPO')).toBeInTheDocument();
      expect(within(ratedCard).getByText('Strong Buy')).toBeInTheDocument();
    });

    it('shows the metric the list is sorted by, formatted like its column, instead of the composite', () => {
      const row = {
        ...fullSeRow, composite_score: 66, minervini_score: 56.2, vcp_score: 81.5, eps_growth_qq: 45.24,
        gap_percent: 12, volume_surge: 2.43, adr_percent: 4.1, se_pattern_primary: 'cup_with_handle', mystery_field: 9,
        sales_growth_qq: 31.04, volume: 2.5e9, market_cap: 1.0e12, market_cap_usd: 132.3e9, adv_usd: 45.2e6,
        vcp_pivot: 101.25, stage: 2, currency: 'USD', perf_3m: 18.5,
      };
      const expectPill = (sortBy, text, name) => {
        renderWithProviders(<ResultsTable {...defaultProps} results={[row]} sortBy={sortBy} />);
        // Screen readers get the full name as real text; the abbreviation is aria-hidden.
        const spoken = screen.getAllByText(name).find((el) => el.getAttribute('aria-hidden') !== 'true');
        expect(spoken.parentElement.querySelector('[aria-hidden="true"]')).toHaveTextContent(text);
        cleanup();
      };

      expectPill('minervini_score', 'Min 56.2', 'Minervini 56.2');
      expectPill('vcp_score', 'VScr 81.5', 'VCP score 81.5'); // VCP and Tight presets
      expectPill('eps_growth_qq', 'EPS 45.2%', 'EPS growth Q/Q 45.2%'); // Growth Rockets
      expectPill('gap_percent', 'Gap 12.0%', 'Gap 12.0%'); // Episodic Pivot (not a table column)
      expectPill('volume_surge', 'Vol 2.4×', 'Volume surge 2.4×'); // Pocket Pivot
      expectPill('adr_percent', 'ADR 4.1%', 'ADR 4.1%');
      // Static-site rows also carry these (static_scan_bundle_exporter extended fields).
      expectPill('perf_3m', '3M 18.5%', 'Performance 3 months 18.5%');
      // Any sortable column a custom preset can save, formatted like the desktop cell.
      expectPill('sales_growth_qq', 'Sales 31.0%', 'Sales growth Q/Q 31.0%');
      expectPill('volume', '$Vol $2.5B', 'Dollar volume $2.5B');
      // market_cap sorts by the native value, so it shows in local currency;
      // market_cap_usd is its own sort.
      expectPill('market_cap', 'MCap $1.0T', 'Market cap $1.0T');
      expectPill('market_cap_usd', 'MCap$ $132.3B', 'Market cap USD $132.3B');
      expectPill('adv_usd', 'ADV $45.2M', 'Average dollar volume $45.2M');
      expectPill('vcp_pivot', 'Pvt $101.25', 'VCP pivot $101.25');
      expectPill('stage', 'Stg S2', 'Stage S2');
      // Non-numeric, unknown and already-visible fields keep the composite.
      expectPill('se_pattern_primary', '66.0', 'Score 66.0');
      expectPill('mystery_field', '66.0', 'Score 66.0');
      expectPill('symbol', '66.0', 'Score 66.0');
    });

    it('leads the group line with a text sort value', () => {
      const row = {
        ...fullSeRow, gics_sector: 'Technology', se_pattern_primary: 'cup_with_handle', rs_line_blue_dot_recent: true,
        correction_survivor: true,
      };
      const leadOf = (sortBy) => {
        renderWithProviders(<ResultsTable {...defaultProps} results={[row]} sortBy={sortBy} />);
        const text = screen.getByTestId('card-sort-note').textContent;
        cleanup();
        return text;
      };

      expect(leadOf('gics_sector')).toBe('Technology');
      expect(leadOf('se_pattern_primary')).toBe('Cup With Handle');
      expect(leadOf('rs_line_blue_dot_recent')).toBe('RS blue dot');
      expect(leadOf('correction_survivor')).toBe('Correction survivor');
    });

    it('offers every sortable column in the picker, common ones first', async () => {
      const onSortChange = vi.fn();
      renderWithProviders(<ResultsTable {...defaultProps} onSortChange={onSortChange} />);

      await userEvent.click(screen.getByRole('combobox', { name: 'Sort by' }));
      const options = screen.getAllByRole('option').map((option) => option.textContent);
      expect(options.slice(0, 3)).toEqual(['Composite score', 'Minervini', 'CANSLIM']);
      for (const name of ['Price', 'EPS growth Q/Q', 'VCP score', 'Market cap', 'ADR', 'Beta']) {
        expect(options).toContain(name);
      }
      expect(new Set(options).size).toBe(options.length);

      await userEvent.click(screen.getByRole('option', { name: 'Market cap' }));
      expect(onSortChange).toHaveBeenLastCalledWith('market_cap', 'desc');
    });

    it('keeps the desktop symbol cell details on the card', async () => {
      const row = {
        ...fullSeRow,
        market: 'HK',
        matched_groups: [{ name: 'VCP setup' }, { name: 'Leaders' }],
        field_availability: { eps_growth_qq: { status: 'unavailable', reason: 'no quarterly data' } },
      };
      renderWithProviders(<ResultsTable {...defaultProps} results={[row]} />);

      const card = screen.getByRole('listitem');
      expect(within(card).getByText('HK')).toBeInTheDocument(); // market badge
      expect(within(card).getByText('VCP setup +1')).toBeInTheDocument(); // matched setups
      // Data-availability chip stays interactive and opens its details dialog.
      await userEvent.click(within(card).getByRole('button', { name: /unavailable or computed/ }));
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('shows the IPO date (what the list sorts by), not an age', () => {
      const row = { ...fullSeRow, ipo_date: '2024-03-15' };
      renderWithProviders(<ResultsTable {...defaultProps} results={[row]} sortBy="ipo_date" />);

      expect(screen.getByText('IPO date 2024-03-15')).toBeInTheDocument();
    });

    it('labels the price sort alias in the picker', () => {
      // scanFilterFields.json marks `price` sortable (same column as current_price).
      renderWithProviders(<ResultsTable {...defaultProps} sortBy="price" />);

      expect(screen.getByRole('combobox', { name: 'Sort by' })).toHaveTextContent('Price');
    });

    it('labels a preset-only active sort in the picker without exposing its field id', () => {
      renderWithProviders(<ResultsTable {...defaultProps} sortBy="gap_percent" />);

      expect(screen.getByRole('combobox', { name: 'Sort by' })).toHaveTextContent('Gap');
      expect(screen.getByRole('combobox', { name: 'Sort by' })).not.toHaveTextContent('gap_percent');
    });

    it('keeps the watchlist action on cards, including rows that cannot open a chart', () => {
      const row = { ...fullSeRow, is_scannable: false };
      const { rerender } = renderWithProviders(<ResultsTable {...defaultProps} results={[row]} />);
      expect(within(screen.getByRole('listitem')).getByTestId(`watchlist-${row.symbol}`)).toBeInTheDocument();

      rerender(<ResultsTable {...defaultProps} results={[row]} showWatchlistMenu={false} />);
      expect(screen.queryByTestId(`watchlist-${row.symbol}`)).not.toBeInTheDocument();
    });

    it('keeps an active sort that the picker does not list', () => {
      renderWithProviders(<ResultsTable {...defaultProps} sortBy="beta" />);

      expect(screen.getByRole('combobox', { name: 'Sort by' })).toHaveTextContent('Beta');
    });
  });

  it('tints score cells by value and leaves a could-not-score 0 plain', () => {
    const row = { ...fullSeRow, minervini_score: 95.4, canslim_score: 12.3, volume_breakthrough_score: 0, custom_score: 0.04 };
    renderWithProviders(<ResultsTable {...defaultProps} results={[row]} />);

    expect(screen.getByText('95.4')).toHaveClass('rt-heat-up-5');
    expect(screen.getByText('12.3')).toHaveClass('rt-heat-down-3');
    // Exact 0 and a value displayed as "0.0" look the same: untinted.
    const zeros = screen.getAllByText('0.0');
    expect(zeros).toHaveLength(2);
    zeros.forEach((cell) => expect(cell.className).not.toMatch(/rt-heat/));
    // The generated rule paints the tint (success.main at step 5).
    expect(screen.getByText('95.4')).toHaveStyle({ backgroundColor: 'rgba(46, 189, 133, 0.5)' });
  });
});
