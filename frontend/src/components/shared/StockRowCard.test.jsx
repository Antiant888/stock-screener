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
    expect(screen.getByLabelText('Score 65.4')).toBeInTheDocument();
    expect(screen.getByText('$139.67')).toBeInTheDocument();
    expect(screen.getByText('+1.9%')).toBeInTheDocument();
    expect(screen.getByTestId('price-sparkline')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('opens by click and by keyboard when openable', () => {
    const onOpen = vi.fn();
    renderWithProviders(<StockRowCard row={row} onOpen={onOpen} />);

    const card = screen.getByRole('button', { name: 'Open FORM chart' });
    fireEvent.click(card);
    fireEvent.keyDown(card, { key: 'Enter' });
    fireEvent.keyDown(card, { key: ' ' });
    expect(onOpen).toHaveBeenCalledTimes(3);
  });
});
