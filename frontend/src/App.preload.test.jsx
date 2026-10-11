import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const requested = vi.hoisted(() => new Set());

vi.mock('./config/runtimeMode', () => ({ STATIC_SITE_MODE: false }));

// Capabilities are still in flight: the app shell shows its loading screen.
vi.mock('./contexts/RuntimeContext', () => ({
  RuntimeProvider: ({ children }) => <>{children}</>,
  useRuntime: () => ({ runtimeReady: false }),
}));

vi.mock('./pages/MarketScanPage', () => {
  requested.add('MarketScanPage');
  return { default: () => null };
});
vi.mock('./pages/BreadthPage', () => {
  requested.add('BreadthPage');
  return { default: () => null };
});

describe('App cold start', () => {
  afterEach(() => {
    requested.clear();
    window.history.pushState({}, '', '/');
  });

  it("requests the current route's page code before capabilities resolve", async () => {
    window.history.pushState({}, '', '/breadth');
    vi.resetModules();
    const { default: App } = await import('./App');

    render(<App />);

    expect(screen.getByText('STOCK SCANNER')).toBeInTheDocument();
    await vi.waitFor(() => expect(requested).toContain('BreadthPage'));
    expect(requested).not.toContain('MarketScanPage');
  });
});
