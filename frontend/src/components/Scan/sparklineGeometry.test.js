import { describe, expect, it } from 'vitest';

import { barRects, monotoneXPath, roundedTopRectPath, scaleLinear } from './sparklineGeometry';

// Expected values are what recharts 2.15 drew for the same inputs (dumped
// from the browser), so the SVG sparklines reproduce its geometry.
const numbers = (d) => d.match(/-?\d+(\.\d+)?/g).map(Number);

describe('scaleLinear', () => {
  it('maps the padded domain onto the plot, top margin to bottom margin', () => {
    // [10, 9, 11] with 5% padding on a 28px-high chart with 2px margins.
    const y = scaleLinear([8.9, 11.1], [26, 2]);
    expect(y(10)).toBeCloseTo(14, 6);
    expect(y(9)).toBeCloseTo(24.909, 3);
    expect(y(11)).toBeCloseTo(3.091, 3);
  });
});

describe('monotoneXPath', () => {
  it("draws d3's monotone-X curve, as recharts' type=\"monotone\" does", () => {
    // The unrounded points recharts computed for [10, 9, 11] in 66x28.
    const y = scaleLinear([8.9, 11.1], [26, 2]);
    const d = monotoneXPath([[0, y(10)], [33, y(9)], [66, y(11)]]);
    // recharts: M0,14C11,19.455,22,24.909,33,24.909C44,24.909,55,14,66,3.091
    expect(numbers(d).map((n) => Number(n.toFixed(3)))).toEqual([
      0, 14, 11, 19.455, 22, 24.909, 33, 24.909, 44, 24.909, 55, 14, 66, 3.091,
    ]);
  });

  it('joins two points with a straight line', () => {
    expect(monotoneXPath([[0, 5], [10, 1]])).toBe('M0,5L10,1');
  });

  it('draws nothing for an empty series', () => {
    expect(monotoneXPath([])).toBe('');
  });
});

describe('barRects', () => {
  it('centres bars capped by maxBarSize in their band', () => {
    // [10, 9, 11] in 60x20 with 1px margins and 10% padding.
    const rects = barRects([10, 9, 11], { width: 60, height: 20, margin: 1, domain: [8.8, 11.2], maxBarSize: 3 });
    expect(rects.map((r) => [r.x, r.width])).toEqual([[8.5, 3], [28.5, 3], [48.5, 3]]);
    expect(rects.map((r) => Number(r.y.toFixed(6)))).toEqual([10, 17.5, 2.5]);
    expect(rects.map((r) => Number(r.height.toFixed(6)))).toEqual([9, 1.5, 16.5]);
  });

  it('truncates bar width to whole pixels when bars are crowded', () => {
    const values = Array.from({ length: 30 }, (_, i) => i + 1);
    const narrow = barRects(values, { width: 60, height: 20, margin: 1, domain: [-2, 33], maxBarSize: 3 });
    expect(narrow[0].width).toBe(1);
    expect(narrow[0].x).toBeCloseTo(0.2, 9);
    expect(narrow[1].x).toBeCloseTo(2.2, 9);

    const wider = barRects(values, { width: 110, height: 32, margin: 1, domain: [-2, 33], maxBarSize: 3 });
    expect(wider[0].width).toBe(2);
    expect(wider[0].x).toBeCloseTo(0.36666666666666664, 9);
    expect(wider[1].x).toBeCloseTo(4.033333333333333, 9);
  });
});

describe('roundedTopRectPath', () => {
  it('rounds the top corners, shrinking the radius to fit short bars', () => {
    // recharts: M8.5,11A 1,1,0,0,1,9.5,10L 10.5,10A 1,1,0,0,1,11.5,11L 11.5,19L 8.5,19Z
    expect(numbers(roundedTopRectPath({ x: 8.5, y: 10, width: 3, height: 9 }, 1))).toEqual([
      8.5, 11, 1, 1, 0, 0, 1, 9.5, 10, 10.5, 10, 1, 1, 0, 0, 1, 11.5, 11, 11.5, 19, 8.5, 19,
    ]);
    // A 1.5px-tall bar gets a 0.75 radius (half its height).
    expect(numbers(roundedTopRectPath({ x: 28.5, y: 17.5, width: 3, height: 1.5 }, 1)).slice(2, 4)).toEqual([0.75, 0.75]);
  });
});
