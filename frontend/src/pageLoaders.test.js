import { describe, expect, it } from 'vitest';

import {
  loadBreadthPage,
  loadMarketScanPage,
  loadOptionsSymbolPage,
  loadScanPage,
  loadStockDetails,
  loaderForPath,
} from './pageLoaders';

describe('loaderForPath', () => {
  it.each([
    ['/', loadMarketScanPage],
    ['/scan', loadScanPage],
    ['/breadth', loadBreadthPage],
    ['/stocks/AAPL', loadStockDetails],
    ['/options/AAPL', loadOptionsSymbolPage],
    // The router renders these too: it ignores a trailing slash and case.
    ['/scan/', loadScanPage],
    ['/Breadth', loadBreadthPage],
    ['/Stocks/AAPL', loadStockDetails],
  ])('maps %s to its page', (path, loader) => {
    expect(loaderForPath(path)).toBe(loader);
  });

  it('has nothing to load for an unknown path', () => {
    expect(loaderForPath('/nope')).toBeNull();
  });
});
