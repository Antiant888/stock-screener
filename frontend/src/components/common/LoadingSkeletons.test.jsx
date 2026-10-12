import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderWithProviders } from '../../test/renderWithProviders';
import { PageSkeleton, TableSkeleton } from './LoadingSkeletons';

describe('loading skeletons', () => {
  it('announce what is loading through text in a status region', () => {
    renderWithProviders(<TableSkeleton label="Loading group rankings" rows={3} columns={4} />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading group rankings');
  });

  it('draw the requested table shape, with or without a title bar', () => {
    const { container, rerender } = renderWithProviders(<TableSkeleton rows={3} columns={4} title={false} />);
    // header bar + 3 rows x 4 cells
    expect(container.querySelectorAll('.MuiSkeleton-root')).toHaveLength(1 + 3 * 4);

    rerender(<TableSkeleton rows={3} columns={4} />);
    expect(container.querySelectorAll('.MuiSkeleton-root')).toHaveLength(1 + 1 + 3 * 4);
  });

  it('can sit bare inside an existing card', () => {
    const { container } = renderWithProviders(<TableSkeleton bare rows={1} columns={1} />);

    expect(container.querySelector('.MuiPaper-root')).toBeNull();
  });

  it('page skeleton defaults to a generic label', () => {
    renderWithProviders(<PageSkeleton />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading page');
  });
});
