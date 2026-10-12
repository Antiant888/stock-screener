import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createTheme, ThemeProvider } from '@mui/material';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import SocialEvidenceDrawer from './SocialEvidenceDrawer';

function renderDrawer(data) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ThemeProvider theme={createTheme()}>
        <SocialEvidenceDrawer
          open
          onClose={() => {}}
          selected={{ canonical_symbol: 'CEG' }}
          query={{ data }}
          onOpenChart={() => {}}
          onOpenTheme={() => {}}
          onScan={() => {}}
        />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

const claim = (overrides) => ({
  post_id: 'p-1', author_handle: 'analyst', created_at: '2026-09-07T10:00:00Z',
  url: 'https://x.com/analyst/status/p-1', theme: 'Nuclear power',
  relationship: 'generator', excerpt: 'signed a 20-year nuclear PPA', support: 'supported',
  ...overrides,
});

describe('SocialEvidenceDrawer', () => {
  it('shows each extracted claim with its theme, relationship, support and source', () => {
    renderDrawer({
      available: true, item: { canonical_symbol: 'CEG' }, posts: [],
      claims: [claim(), claim({ post_id: 'p-2', theme: 'Data center power', support: 'uncertain',
        excerpt: 'maybe adds a second PPA', url: 'https://x.com/analyst/status/p-2' })],
    });

    const list = screen.getByRole('list', { name: 'Extracted claims' });
    const [first, second] = within(list).getAllByRole('listitem');
    expect(within(first).getByText('“signed a 20-year nuclear PPA”')).toBeInTheDocument();
    expect(within(first).getByText('Nuclear power · generator')).toBeInTheDocument();
    expect(within(first).getByText('Supported')).toBeInTheDocument();
    expect(within(first).getByRole('link', { name: '@analyst' }))
      .toHaveAttribute('href', 'https://x.com/analyst/status/p-1');
    expect(within(second).getByText('Uncertain')).toBeInTheDocument();
  });

  it('credits attachment wording to its source, not to the poster', () => {
    renderDrawer({
      available: true, item: { canonical_symbol: 'CEG' }, posts: [],
      claims: [claim({ source: 'article', source_url: 'https://publisher.example.com/ceg' })],
    });

    const [item] = within(screen.getByRole('list', { name: 'Extracted claims' })).getAllByRole('listitem');
    expect(within(item).getByRole('link', { name: 'linked article' }))
      .toHaveAttribute('href', 'https://publisher.example.com/ceg');
    expect(within(item).getByText(/shared by/)).toBeInTheDocument();
    expect(within(item).getByRole('link', { name: '@analyst' }))
      .toHaveAttribute('href', 'https://x.com/analyst/status/p-1');
  });

  it('says so when no claims were extracted', () => {
    renderDrawer({ available: true, item: { canonical_symbol: 'CEG' }, posts: [], claims: [] });
    expect(screen.getByText('No company claims extracted in this window.')).toBeInTheDocument();
  });

  it('omits the section for an API that does not report claims', () => {
    renderDrawer({ available: true, item: { canonical_symbol: 'CEG' }, posts: [] });
    expect(screen.queryByText('What X is claiming')).not.toBeInTheDocument();
  });
});
