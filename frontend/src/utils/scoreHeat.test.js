import { describe, expect, it } from 'vitest';
import { createTheme } from '@mui/material';
import { getDesignTokens } from '../theme';
import { heatClass, heatClassStyles, heatFill, heatStep } from './scoreHeat';

describe('heatStep', () => {
  it.each([
    [120, { dir: 'up', step: 5 }],
    [100, { dir: 'up', step: 5 }],
    [92, { dir: 'up', step: 5 }],
    [91.9, { dir: 'up', step: 4 }],
    [68, { dir: 'up', step: 2 }],
    [67.9, { dir: 'up', step: 1 }],
    [61, { dir: 'up', step: 1 }],
    [59.96, { dir: 'up', step: 1 }], // displayed as 60.0
    [59.94, null], // displayed as 59.9
    [50, null],
    [40.04, { dir: 'down', step: 1 }], // displayed as 40.0
    [27.1, { dir: 'down', step: 1 }],
    [27, { dir: 'down', step: 2 }],
    [26.7, { dir: 'down', step: 2 }],
    [14, { dir: 'down', step: 3 }],
    [0, { dir: 'down', step: 3 }],
    [-5, { dir: 'down', step: 3 }],
  ])('buckets %s by its displayed value', (value, expected) => {
    expect(heatStep(value)).toEqual(expected);
  });

  it('leaves missing and non-numeric values untinted', () => {
    expect(heatStep(null)).toBeNull();
    expect(heatStep(undefined)).toBeNull();
    expect(heatStep(Number.NaN)).toBeNull();
    expect(heatStep(Infinity)).toBeNull();
    expect(heatStep('80')).toBeNull();
    expect(heatClass(null)).toBe('');
  });
});

describe('heatFill / heatClassStyles', () => {
  const { palette } = createTheme(getDesignTokens('dark'));
  const alphaOf = (rgba) => Number(rgba.match(/, ([\d.]+)\)$/)[1]);

  it('deepens with the step and follows the theme tones', () => {
    const ups = [1, 2, 3, 4, 5].map((step) => heatFill(palette, { dir: 'up', step }));
    expect(ups[0]).toMatch(/^rgba\(46, 189, 133,/);
    expect(ups.map(alphaOf)).toEqual([...ups.map(alphaOf)].sort((a, b) => a - b));
    expect(heatFill(palette, { dir: 'down', step: 3 })).toMatch(/^rgba\(246, 70, 93,/);
  });

  it('generates a rule for every class heatClass can return', () => {
    const rules = heatClassStyles(palette);
    for (const value of [100, 92, 84, 76, 68, 60, 40, 27, 14]) {
      expect(rules).toHaveProperty(`& .${heatClass(value)}`);
    }
  });
});
