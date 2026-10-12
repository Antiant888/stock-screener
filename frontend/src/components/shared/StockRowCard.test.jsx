import { fireEvent, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { renderWithProviders } from '../../test/renderWithProviders';
import StockRowCard from './StockRowCard';

vi.mock('../Scan/PriceSparkline', () => ({ default: () => <span data-testid="price-sparkline" /> }));

const row = {
  symbol: 'FORM',
  company_name: 'FormFactor Inc',
  ibd_industry_group: 'Elec-Semiconductor Equip',
  ibd_group_rank: 27,
  composite_score: 65.43,
  current_price: 139.67,
  currency: 'USD',
  price_change_1d: 1.94,
  price_sparkline_data: [1, 2, 3],
  price_trend: 1,
};

describe('StockRowCard', () => {
  it('shows the fields a reader scans for', () => {
    renderWithProviders(<StockRowCard row={row} meta="RS 92" />);

    expect(screen.getByText('FORM')).toBeInTheDocument();
    expect(screen.getByText('FormFactor Inc')).toBeInTheDocument();
    expect(screen.getByText('Grp #27 · Elec-Semiconductor Equip · RS 92')).toBeInTheDocument();
    expect(screen.getByText('Score 65.4')).toBeInTheDocument();
    expect(screen.getByText('65.4')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('$139.67')).toBeInTheDocument();
    expect(screen.getByText('+1.9%')).toBeInTheDocument();
    expect(screen.getByTestId('price-sparkline')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('opens through a real button while the rest stays readable content', () => {
    const onOpen = vi.fn();
    renderWithProviders(<StockRowCard row={row} onOpen={onOpen} />);

    const open = screen.getByRole('button', { name: 'Open FORM chart' });
    expect(open.tagName).toBe('BUTTON'); // native Enter/Space handling
    expect(open).not.toContainElement(screen.getByText('$139.67'));
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('keeps a clickable badge separate from opening the chart', () => {
    const onOpen = vi.fn();
    const onBadge = vi.fn();
    renderWithProviders(
      <StockRowCard row={row} onOpen={onOpen} badge={<button type="button" onClick={onBadge}>Watch</button>} />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Watch' }));
    expect(onBadge).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Open FORM chart' })).not.toContainElement(
      screen.getByRole('button', { name: 'Watch' }),
    );
  });

  it('lets taps on an inert badge reach the card while interactive badges stay clickable', () => {
    const { rerender } = renderWithProviders(
      <StockRowCard row={row} onOpen={vi.fn()} badge={<span>Strong Buy</span>} />,
    );
    // The badge slot sits above the stretched open-button overlay; it must not swallow taps.
    expect(screen.getByText('Strong Buy').parentElement).toHaveStyle({ pointerEvents: 'none' });

    rerender(<StockRowCard row={row} onOpen={vi.fn()} badge={<button type="button">Watch</button>} />);
    expect(screen.getByRole('button', { name: 'Watch' })).toHaveStyle({ pointerEvents: 'auto' });
  });

  it('keeps the ticker at full width next to a long badge and actions', () => {
    renderWithProviders(
      <StockRowCard row={row} onOpen={vi.fn()} badge={<span>Deteriorating</span>} actions={<button type="button">Add</button>} />,
    );

    // jsdom has no layout, so check the flex rules: the ticker never shrinks,
    // the badge slot can (it truncates), the actions keep their size.
    expect(screen.getByRole('button', { name: 'Open FORM chart' })).toHaveStyle({ flexShrink: '0' });
    expect(screen.getByText('Deteriorating').parentElement).toHaveStyle({ minWidth: '0' });
    expect(screen.getByRole('button', { name: 'Add' }).parentElement).toHaveStyle({ flexShrink: '0' });
  });

  it('shows a dash for a missing or non-numeric change', () => {
    renderWithProviders(<StockRowCard row={{ ...row, price_change_1d: 'n/a' }} />);

    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
    expect(screen.getByText('-')).toBeInTheDocument();
  });
});
