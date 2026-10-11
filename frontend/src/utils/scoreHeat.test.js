import { describe, expect, it } from 'vitest';
import { createTheme } from '@mui/material';
import { getDesignTokens } from '../theme';
import { heatClass, heatFill, heatStep } from './scoreHeat';

describe('heatStep', () => {
  it.each([
    [100, { dir: 'up', step: 5 }],
    [92, { dir: 'up', step: 5 }],
    [61, { dir: 'up', step: 1 }],
    [59.96, { dir: 'up', step: 1 }], // displayed as 60.0
    [50, null],
    [40.04, { dir: 'down', step: 1 }], // displayed as 40.0
    [27, { dir: 'down', step: 1 }],
    [20, { dir: 'down', step: 2 }],
    [5, { dir: 'down', step: 3 }],
  ])('buckets %s by its displayed value', (value, expected) => {
    expect(heatStep(value)).toEqual(expected);
  });

  it('treats 0 as a real low value and leaves missing values untinted', () => {
    expect(heatStep(0)).toEqual({ dir: 'down', step: 3 });
    expect(heatStep(null)).toBeNull();
    expect(heatStep(Number.NaN)).toBeNull();
    expect(heatClass(null)).toBe('');
  });
});

describe('heatFill', () => {
  it('deepens with the step and follows the theme tones', () => {
    const { palette } = createTheme(getDesignTokens('dark'));
    const alphaOf = (rgba) => Number(rgba.match(/[\d.]+\)$/)[0].replace(')', ''));
    expect(heatFill(palette, { dir: 'up', step: 1 })).toMatch(/^rgba\(46, 189, 133,/);
    expect(alphaOf(heatFill(palette, { dir: 'up', step: 5 })))
      .toBeGreaterThan(alphaOf(heatFill(palette, { dir: 'up', step: 1 })));
    expect(heatFill(palette, { dir: 'down', step: 3 })).toMatch(/^rgba\(246, 70, 93,/);
  });
});
