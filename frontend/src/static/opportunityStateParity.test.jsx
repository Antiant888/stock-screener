import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../test/renderWithProviders';
import StaticHomePage from './pages/StaticHomePage';
import StaticScanPage from './pages/StaticScanPage';


const EXPECTED_LABELS = {
  exit_risk: 'Exit Risk',
  deteriorating: 'Deteriorating',
  event_risk: 'Event Risk',
  extended: 'Extended',
  data_limited: 'Data Limited',
  setup_ready: 'Setup Ready',
  watch: 'Watch',
};

const EXPECTED_SURVIVOR_SYMBOLS = [
  'READY',
  'EVENT',
  'DETERIORATING',
  'EXTENDED',
  'WATCH',
];

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: ({ count }) => ({
    getVirtualItems: () => Array.from({ length: count }, (_, index) => ({
      index,
      start: index * 48,
      end: (index + 1) * 48,
      size: 48,
      key: index,
    })),
    getTotalSize: () => count * 48,
    measure: () => {},
  }),
}));

vi.mock('../components/MarketScan/MarketHealthExposure', () => ({
  default: () => <div data-testid="market-health-exposure" />,
}));

const loadParityFixture = async () => {
  try {
    return await vi.importActual('../test/fixtures/opportunityStateFixtures.js');
  } catch {
    return null;
  }
};

const clone = (value) => JSON.parse(JSON.stringify(value));

const installStaticFetch = (fixture, capability) => {
  const payloads = {
    'manifest.json': fixture.buildStaticRootManifest(capability),
    'markets/us/scan/manifest.json': fixture.STATIC_SCAN_MANIFEST,
    'markets/us/home.json': fixture.STATIC_HOME_PAYLOAD,
  };
  globalThis.fetch = vi.fn(async (url) => {
    const path = String(url).split('/static-data/')[1];
    const payload = payloads[path];
    if (!payload) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => clone(payload) };
  });
};

const fixtureSymbolsInTable = (table, allSymbols) => (
  within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => allSymbols.find((symbol) => within(row).queryByText(symbol)))
    .filter(Boolean)
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('opportunity-state static/live parity fixture', () => {
  it('renders all labels, compact provenance, preset membership, and legacy nulls on Scan', async () => {
    const fixture = await loadParityFixture();
    expect(fixture).not.toBeNull();
    installStaticFetch(fixture, true);

    renderWithProviders(<StaticScanPage />);

    expect(await screen.findByRole('heading', { name: 'Daily Scan' })).toBeInTheDocument();
    const table = await screen.findByRole('table', {}, { timeout: 10_000 });
    expect(within(table).getByRole('columnheader', { name: 'Res' })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: 'Action' })).toBeInTheDocument();
    Object.values(EXPECTED_LABELS).forEach((label) => {
      expect(within(table).getAllByText(label).length).toBeGreaterThan(0);
    });
    expect(within(table).getByText('Not computed')).toBeInTheDocument();
    expect(within(table).queryByRole('button', { name: 'Not computed' })).not.toBeInTheDocument();

    fireEvent.click(within(table).getByRole('button', { name: 'Event Risk' }));
    expect(await screen.findByRole('heading', { name: 'Opportunity evidence' })).toBeInTheDocument();
    expect(screen.getByText('16.6')).toBeInTheDocument();
    expect(screen.getByText('Benchmark date lag')).toBeInTheDocument();
    expect(screen.getByText('earnings_soon')).toBeInTheDocument();
    expect(screen.getByText('XNAS')).toBeInTheDocument();
    expect(screen.getByText('SPY')).toBeInTheDocument();
    expect(screen.getByText('2026-08-20')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close opportunity evidence' }));

    fireEvent.click(screen.getByText('Survivors (5)'));
    await waitFor(() => {
      expect(
        fixtureSymbolsInTable(table, fixture.ALL_SYMBOLS),
      ).toEqual(EXPECTED_SURVIVOR_SYMBOLS);
    });
    expect(within(table).queryByText('EXIT')).not.toBeInTheDocument();
    expect(within(table).queryByText('LIMITED')).not.toBeInTheDocument();
    expect(within(table).queryByText('LEGACY')).not.toBeInTheDocument();
  }, 60_000);

  it('renders the same ordered membership and evidence through the static Daily panel', async () => {
    const fixture = await loadParityFixture();
    expect(fixture).not.toBeNull();
    installStaticFetch(fixture, true);

    renderWithProviders(<StaticHomePage />);
    const panel = await screen.findByTestId('correction-survivors-panel');

    expect(within(panel).getByText('Total survivors: 5')).toBeInTheDocument();
    expect(within(panel).getByText('Exit Risk: 0')).toBeInTheDocument();
    expect(within(panel).getByText('Deteriorating: 1')).toBeInTheDocument();
    expect(within(panel).getByText('Event Risk: 1')).toBeInTheDocument();
    expect(within(panel).getByText('Extended: 1')).toBeInTheDocument();
    expect(within(panel).getByText('Data Limited: 0')).toBeInTheDocument();
    expect(within(panel).getByText('Setup Ready: 1')).toBeInTheDocument();
    expect(within(panel).getByText('Watch: 1')).toBeInTheDocument();
    expect(
      fixtureSymbolsInTable(panel, fixture.ALL_SYMBOLS),
    ).toEqual(EXPECTED_SURVIVOR_SYMBOLS);

    fireEvent.click(within(panel).getByRole('button', { name: 'Event Risk' }));
    expect(await screen.findByText('Benchmark date lag')).toBeInTheDocument();
    expect(screen.getByText('earnings_soon')).toBeInTheDocument();
    expect(screen.getByText('XNAS')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close opportunity evidence' }));
  });

  it.each([
    ['missing', undefined],
    ['false', false],
  ])('hides preset, columns, and Daily panel when capability is %s', async (_label, capability) => {
    const fixture = await loadParityFixture();
    expect(fixture).not.toBeNull();
    installStaticFetch(fixture, capability);

    const scanView = renderWithProviders(<StaticScanPage />);
    expect(await screen.findByRole('heading', { name: 'Daily Scan' })).toBeInTheDocument();
    const table = await screen.findByRole('table', {}, { timeout: 10_000 });
    expect(within(table).queryByRole('columnheader', { name: 'Res' })).not.toBeInTheDocument();
    expect(within(table).queryByRole('columnheader', { name: 'Action' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Survivors (5)' })).not.toBeInTheDocument();
    scanView.unmount();

    renderWithProviders(<StaticHomePage />);
    expect(await screen.findByText('Top Scan Candidates')).toBeInTheDocument();
    expect(screen.queryByTestId('correction-survivors-panel')).not.toBeInTheDocument();
  }, 40_000);
});
