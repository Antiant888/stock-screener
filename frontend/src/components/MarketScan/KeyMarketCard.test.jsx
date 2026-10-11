import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderWithProviders } from '../../test/renderWithProviders';
import KeyMarketCard from './KeyMarketCard';

describe('KeyMarketCard', () => {
  it('labels an up day and a falling window separately', () => {
    renderWithProviders(
      <KeyMarketCard
        item={{ symbol: 'IWM', display_name: 'Russell 2000 ETF', latest_close: 95, change_1d: 0.49 }}
        closes={[100, 98, 96, 95]}
        currency="USD"
      />,
    );

    expect(screen.getByText('+0.49%')).toBeInTheDocument();
    expect(screen.getByText('3D ▼ 5.0%')).toBeInTheDocument();
  });
});
