import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderWithProviders } from '../../test/renderWithProviders';
import { PageSkeleton, TableSkeleton } from './LoadingSkeletons';

describe('loading skeletons', () => {
  it('announce what is loading as a busy status region', () => {
    renderWithProviders(<TableSkeleton label="Loading group rankings" rows={3} columns={4} />);

    const status = screen.getByRole('status', { name: 'Loading group rankings' });
    expect(status).toHaveAttribute('aria-busy', 'true');
  });

  it('draw the requested table shape', () => {
    const { container } = renderWithProviders(<TableSkeleton rows={3} columns={4} title={false} />);

    // header bar + 3 rows x 4 cells
    expect(container.querySelectorAll('.MuiSkeleton-root')).toHaveLength(1 + 3 * 4);
  });

  it('page skeleton defaults to a generic label', () => {
    renderWithProviders(<PageSkeleton />);

    expect(screen.getByRole('status', { name: 'Loading page' })).toBeInTheDocument();
  });
});
