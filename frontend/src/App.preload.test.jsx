import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const loadedPaths = vi.hoisted(() => []);

vi.mock('./config/runtimeMode', () => ({ STATIC_SITE_MODE: false }));

// Capabilities are still in flight: the app shell shows its loading screen.
vi.mock('./contexts/RuntimeContext', () => ({
  RuntimeProvider: ({ children }) => <>{children}</>,
  useRuntime: () => ({ runtimeReady: false }),
}));

vi.mock('./pageLoaders', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    loaderForPath: (path) => {
      const loader = actual.loaderForPath(path);
      return loader && (() => {
        loadedPaths.push(path);
        return loader();
      });
    },
  };
});
vi.mock('./pages/MarketScanPage', () => ({ default: () => null }));
vi.mock('./pages/BreadthPage', () => ({ default: () => null }));
vi.mock('./static/StaticAppShell', () => ({ default: () => <div>Static shell</div> }));

async function importAppAt(path) {
  window.history.pushState({}, '', path);
  vi.resetModules();
  return (await import('./App')).default;
}

describe('App cold start', () => {
  afterEach(() => {
    loadedPaths.length = 0;
    vi.doUnmock('./config/runtimeMode');
    window.history.pushState({}, '', '/');
  });

  it("requests the current route's page code before capabilities resolve", async () => {
    const App = await importAppAt('/breadth');

    expect(loadedPaths).toEqual(['/breadth']);
    render(<App />);
    expect(screen.getByText('STOCK SCANNER')).toBeInTheDocument();
  });

  it('leaves live page code alone in the static site', async () => {
    vi.doMock('./config/runtimeMode', () => ({ STATIC_SITE_MODE: true }));
    const App = await importAppAt('/breadth');

    render(<App />);

    expect(await screen.findByText('Static shell')).toBeInTheDocument();
    expect(loadedPaths).toEqual([]);
  });
});
