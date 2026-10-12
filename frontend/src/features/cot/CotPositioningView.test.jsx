import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import CotPositioningView from './CotPositioningView';
import {
  cotCatalogFixture,
  cotHistoryFixture,
} from './__fixtures__/cotResponses';

describe('CotPositioningView', () => {
  it('renders shared controls, summary, chart description, and callbacks', () => {
    const onSelectInstrument = vi.fn();
    const onRangeChange = vi.fn();
    render(
      <CotPositioningView
        catalog={cotCatalogFixture}
        history={cotHistoryFixture}
        selectedSlug="sp-500"
        range="1y"
        onSelectInstrument={onSelectInstrument}
        onRangeChange={onRangeChange}
      />,
    );

    expect(screen.getByRole('heading', { name: /COT Positioning/i })).toBeInTheDocument();
    expect(screen.getByText('Leveraged Funds Long')).toBeInTheDocument();
    expect(screen.getByText('3Y Percentile')).toBeInTheDocument();
    expect(screen.getAllByText(/Exact future/i).length).toBeGreaterThan(0);
    expect(screen.getByRole('img', { name: /S&P 500 net positioning/i })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('COT instrument'), { target: { value: 'nasdaq-100' } });
    expect(onSelectInstrument).toHaveBeenCalledWith('nasdaq-100');
    fireEvent.click(screen.getByRole('button', { name: '3Y' }));
    expect(onRangeChange).toHaveBeenCalledWith('3y');
    fireEvent.click(screen.getByRole('button', { name: 'Long & Short' }));
    expect(screen.getByLabelText('CFTC participant')).toBeInTheDocument();
  });

  it('shows stale, insufficient-history, proxy/partial, and link-only states', () => {
    const catalog = structuredClone(cotCatalogFixture);
    catalog.publication.stale = true;
    const history = structuredClone(cotHistoryFixture);
    history.publication.stale = true;
    history.price_mapping_kind = 'unavailable';
    history.price_symbol = null;
    history.price_coverage_state = 'unavailable';
    history.tradingview_url = 'https://www.tradingview.com/symbols/ICEUS-RS1%21/contracts/';
    history.weeks.forEach((week) => {
      week.price_date = null;
      week.price_close = null;
      week.price_change_pct = null;
      week.positions.forEach((position) => {
        position.percentile_3y = null;
        position.percentile_status = 'insufficient_history';
      });
    });

    render(
      <CotPositioningView
        catalog={catalog}
        history={history}
        selectedSlug="sp-500"
        range="1y"
        onSelectInstrument={() => {}}
        onRangeChange={() => {}}
      />,
    );

    expect(screen.getByText(/COT publication is stale/i)).toBeInTheDocument();
    expect(screen.getByText(/Insufficient 3Y history/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Price unavailable/i).length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: /View contract on TradingView/i })).toHaveAttribute('target', '_blank');
  });

  it('renders explicit loading, error, and empty states', () => {
    const { rerender } = render(<CotPositioningView isLoading />);
    expect(screen.getByRole('status')).toHaveTextContent(/Loading COT positioning/i);
    rerender(<CotPositioningView error={new Error('failed')} />);
    expect(screen.getByText(/Unable to load COT positioning/i)).toBeInTheDocument();
    rerender(<CotPositioningView catalog={cotCatalogFixture} history={null} />);
    expect(screen.getByText(/No COT history is available/i)).toBeInTheDocument();
  });
});
