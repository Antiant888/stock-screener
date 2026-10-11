import { describe, expect, it } from 'vitest';
import { onPalette } from './colorUtils';

describe('onPalette', () => {
  it('pairs each kind of fill with readable text', () => {
    expect(onPalette('success.main')).toEqual({ bgcolor: 'success.main', color: 'success.contrastText' });
    expect(onPalette('grey.400')).toEqual({ bgcolor: 'grey.400', color: 'grey.900' });
    expect(onPalette('text.secondary')).toEqual({ bgcolor: 'text.secondary', color: 'background.paper' });
  });
});
