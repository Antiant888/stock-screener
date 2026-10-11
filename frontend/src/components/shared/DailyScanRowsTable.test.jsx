import { fireEvent, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithProviders } from '../../test/renderWithProviders';
import DailyScanRowsTable from './DailyScanRowsTable';

vi.mock('../Scan/PriceSparkline', () => ({ default: () => <span data-testid="price-sparkline" /> }));
vi.mock('../Scan/RSSparkline', () => ({ default: () => <span data-testid="rs-sparkline" /> }));

const rows = [
  { symbol: 'FORM', company_name: 'FormFactor Inc', composite_score: 65.4, rs_rating: 92, rating: 'Watch', current_price: 139.67, currency: 'USD', price_change_1d: 1.9 },
  { symbol: 'NOCH', company_name: 'No Chart Co', composite_score: 50, rs_rating: 40, rating: 'Pass', current_price: 10, currency: 'USD', price_change_1d: -0.5 },
];

describe('DailyScanRowsTable on a phone-width screen', () => {
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

  it('renders one card per row and opens charts only where they exist', () => {
    const onOpenChart = vi.fn();
    renderWithProviders(
      <DailyScanRowsTable
        title="Top scan candidates"
        rows={rows}
        showRs
        showRating
        chartEnabledSymbols={new Set(['FORM'])}
        navigationSymbols={['FORM']}
        onOpenChart={onOpenChart}
        emptyMessage="Nothing here"
      />,
    );

    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    const [first] = screen.getAllByRole('listitem');
    expect(within(first).getByText('Watch')).toBeInTheDocument();
    expect(within(first).getByText('RS 92')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Open FORM chart' }));
    expect(onOpenChart).toHaveBeenCalledWith('FORM', ['FORM']);
    expect(screen.queryByRole('button', { name: 'Open NOCH chart' })).not.toBeInTheDocument();
  });

  it('keeps the empty state', () => {
    renderWithProviders(<DailyScanRowsTable title="Leaders" rows={[]} emptyMessage="Nothing here" />);

    expect(screen.getByText('Nothing here')).toBeInTheDocument();
  });
});
