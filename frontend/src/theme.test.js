import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { COLOR_MODE_STORAGE_KEY, readColorMode, saveColorMode, useColorMode } from './theme';

const prefersLight = (matches) => {
  window.matchMedia = vi.fn().mockReturnValue({ matches });
};

// A matchMedia whose answer can flip, notifying 'change' listeners like a browser.
const switchableOs = (initiallyLight) => {
  const listeners = new Set();
  const query = {
    matches: initiallyLight,
    addEventListener: (_type, fn) => listeners.add(fn),
    removeEventListener: (_type, fn) => listeners.delete(fn),
  };
  window.matchMedia = vi.fn().mockReturnValue(query);
  return (light) => {
    query.matches = light;
    listeners.forEach((fn) => fn());
  };
};

afterEach(() => {
  window.localStorage.clear();
  delete window.matchMedia;
});

describe('readColorMode', () => {
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

describe('useColorMode', () => {
  it('tracks OS changes until the user toggles, then keeps the choice', () => {
    const setOsLight = switchableOs(false);
    const { result } = renderHook(() => useColorMode());
    expect(result.current.mode).toBe('dark');

    act(() => setOsLight(true));
    expect(result.current.mode).toBe('light');

    act(() => result.current.toggleColorMode());
    expect(result.current.mode).toBe('dark');
    expect(window.localStorage.getItem(COLOR_MODE_STORAGE_KEY)).toBe('dark');

    act(() => setOsLight(false));
    act(() => setOsLight(true));
    expect(result.current.mode).toBe('dark');
  });
});
