import { afterEach, describe, expect, it, vi } from 'vitest';
import { COLOR_MODE_STORAGE_KEY, readColorMode, saveColorMode } from './theme';

const prefersLight = (matches) => {
  window.matchMedia = vi.fn().mockReturnValue({ matches });
};

describe('readColorMode', () => {
  afterEach(() => {
    window.localStorage.clear();
    delete window.matchMedia;
  });

  it('uses the saved choice over the OS preference', () => {
    prefersLight(true);
    saveColorMode('dark');
    expect(readColorMode()).toBe('dark');
  });

  it('follows the OS preference when nothing valid is saved', () => {
    window.localStorage.setItem(COLOR_MODE_STORAGE_KEY, 'sepia');
    prefersLight(true);
    expect(readColorMode()).toBe('light');
    prefersLight(false);
    expect(readColorMode()).toBe('dark');
  });

  it('defaults to dark without matchMedia', () => {
    expect(readColorMode()).toBe('dark');
  });
});
