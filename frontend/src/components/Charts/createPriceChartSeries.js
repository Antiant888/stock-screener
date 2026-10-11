import {
  createChart,
  CrosshairMode,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  createSeriesMarkers,
} from 'lightweight-charts';

// Create the price chart and all of its series in one place, returning every
// handle the component needs to drive. Vertical bands (scaleMargins) are neutral
// defaults here; the component's "RS strip layout" and "dynamic RS band" effects
// reapply them reactively based on whether the RS line is shown.
// `chrome` carries the theme's surface colours (see CandlestickChart) so the
// canvas matches the cards around it in both modes.
export function createPriceChartSeries(container, { width, height, chrome, interactive }) {
  const chart = createChart(container, {
    width,
    height,
    layout: {
      background: { type: 'solid', color: chrome.background },
      textColor: chrome.text,
    },
    grid: {
      vertLines: { color: chrome.grid },
      horzLines: { color: chrome.grid },
    },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: {
      borderColor: chrome.grid,
      mode: 1, // Logarithmic scale
    },
    timeScale: {
      borderColor: chrome.grid,
      timeVisible: true,
      secondsVisible: false,
    },
    handleScroll: interactive,
    handleScale: interactive,
  });

  // Volume (bottom). Neutral scaleMargins; reapplied by the RS strip layout effect.
  const volumeSeries = chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });

  // Candlesticks. Neutral scaleMargins; reapplied by the RS strip layout effect.
  const candlestickSeries = chart.addSeries(CandlestickSeries, {
    upColor: '#2196f3',
    downColor: '#E619CD',
    borderVisible: false,
    wickUpColor: '#2196f3',
    wickDownColor: '#E619CD',
    priceScaleId: 'right',
  });
  candlestickSeries.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.3 } });

  // EMA 10 / 20 / 50 — share the price ('right') scale.
  const ema10Series = chart.addSeries(LineSeries, { color: '#4CF64D', lineWidth: 2, priceScaleId: 'right' });
  const ema20Series = chart.addSeries(LineSeries, { color: '#87FBFB', lineWidth: 2, priceScaleId: 'right' });
  const ema50Series = chart.addSeries(LineSeries, { color: '#38CD07', lineWidth: 2, priceScaleId: 'right' });

  // RS line on its own hidden overlay scale (orange — distinct from the EMAs). It
  // sits in a band below the candles; blue-dot markers attach to it. The band is
  // sized dynamically by the "dynamic RS band" effect.
  const rsLineSeries = chart.addSeries(LineSeries, {
    color: '#FFA726',
    lineWidth: 2,
    priceScaleId: 'rs',
    lastValueVisible: false,
    priceLineVisible: false,
  });
  rsLineSeries.priceScale().applyOptions({ scaleMargins: { top: 0.66, bottom: 0.22 }, visible: false });
  const rsMarkers = createSeriesMarkers(rsLineSeries, []);

  return {
    chart,
    volumeSeries,
    candlestickSeries,
    ema10Series,
    ema20Series,
    ema50Series,
    rsLineSeries,
    rsMarkers,
  };
}
