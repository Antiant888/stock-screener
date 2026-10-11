import { describe, expect, it } from 'vitest';
import { getStageColor, onPalette } from './colorUtils';

describe('getStageColor', () => {
  it('maps stages to theme tones, unknown stages to neutral grey', () => {
    expect([1, 2, 3, 4, null].map(getStageColor))
      .toEqual(['grey.500', 'success.main', 'warning.main', 'error.main', 'grey.500']);
  });
});

describe('onPalette', () => {
  it('pairs each kind of fill with readable text', () => {
    expect(onPalette('success.main')).toEqual({ bgcolor: 'success.main', color: 'success.contrastText' });
    expect(onPalette('grey.400')).toEqual({ bgcolor: 'grey.400', color: 'grey.900' });
    expect(onPalette('text.secondary')).toEqual({ bgcolor: 'text.secondary', color: 'background.paper' });
    expect(onPalette(undefined)).toEqual({ bgcolor: 'grey.500', color: 'grey.900' });
  });
});
