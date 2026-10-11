import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderWithProviders } from '../../test/renderWithProviders';
import KeyMarketCard from './KeyMarketCard';

const card = (closes, change1d = 0.49) => renderWithProviders(
  <KeyMarketCard
    item={{
      symbol: 'IWM',
      display_name: 'Russell 2000 ETF',
      latest_close: closes[closes.length - 1],
      change_1d: change1d,
      history: closes.map((close) => ({ close })),
    }}
    currency="USD"
  />,
);

describe('KeyMarketCard', () => {
  it('labels an up day and a falling window separately', () => {
    card([100, 98, 96, 95]);

    expect(screen.getByText('+0.49%')).toBeInTheDocument();
    expect(screen.getByText('3D ▼ 5.0%')).toBeInTheDocument();
  });

  it('counts the sessions a gap spans, not the closes left after it', () => {
    card([100, null, 102]);

    expect(screen.getByText('2D ▲ 2.0%')).toBeInTheDocument();
  });

  it('marks a flat window as neither up nor down', () => {
    card([100, 101, 100], 0);

    expect(screen.getByText('2D – 0.0%')).toBeInTheDocument();
  });
});
