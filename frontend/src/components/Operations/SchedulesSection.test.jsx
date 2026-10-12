import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, useQuery } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../test/renderWithProviders';
import SchedulesSection from './SchedulesSection';

const getScheduledTasks = vi.fn();
const triggerTask = vi.fn();

vi.mock('../../api/tasks', () => ({
  getScheduledTasks: (...args) => getScheduledTasks(...args),
  triggerTask: (...args) => triggerTask(...args),
}));

const lastRun = (status, extra = {}) => ({
  id: 1,
  task_id: `run-${status}`,
  status,
  started_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
  completed_at: null,
  duration_seconds: null,
  triggered_by: 'auto',
  error_message: null,
  ...extra,
});

const task = (name, displayName, run) => ({
  name,
  display_name: displayName,
  task_function: `app.tasks.${name}`,
  description: `${displayName} description`,
  schedule_description: 'Sunday 2:00 AM ET',
  is_enabled: true,
  last_run: run,
});

function payload(tasks) {
  return { tasks, total_tasks: tasks.length };
}

function rowFor(displayName) {
  return screen.getByText(displayName).closest('tr');
}

function renderSection() {
  return renderWithProviders(<SchedulesSection />);
}

describe('SchedulesSection', () => {
  beforeEach(() => {
    getScheduledTasks.mockReset();
    triggerTask.mockReset();
  });

  it('shows each task with its server-reported last run', async () => {
    getScheduledTasks.mockResolvedValue(payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed', { duration_seconds: 42 })),
      task('refresh', 'Weekly Full Refresh', lastRun('failed', { error_message: 'db locked' })),
      task('prune', 'Monthly Price Data Cleanup', lastRun('lost')),
      task('universe', 'Weekly Universe Refresh (US)', null),
    ]));

    renderSection();

    expect(await screen.findByText('Weekly Scan Cleanup')).toBeInTheDocument();
    expect(within(rowFor('Weekly Scan Cleanup')).getByText('Completed')).toBeInTheDocument();
    expect(within(rowFor('Weekly Scan Cleanup')).getByText('5m ago · 42s')).toBeInTheDocument();
    expect(within(rowFor('Weekly Full Refresh')).getByText('Failed')).toBeInTheDocument();
    expect(within(rowFor('Weekly Full Refresh')).getByText('db locked')).toBeInTheDocument();
    // No recorded outcome is not a failure, and must not look like Queued.
    const unknown = within(rowFor('Monthly Price Data Cleanup')).getByText('Unknown').closest('.MuiChip-root');
    expect(unknown).toHaveClass('MuiChip-outlined');
    expect(unknown).not.toHaveClass('MuiChip-colorWarning');
    expect(within(rowFor('Weekly Universe Refresh (US)')).getByText('Never')).toBeInTheDocument();
  });

  it('runs only the clicked task and refreshes the list', async () => {
    getScheduledTasks.mockResolvedValue(payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed')),
      task('refresh', 'Weekly Full Refresh', lastRun('completed')),
    ]));
    let finishTrigger;
    triggerTask.mockReturnValue(new Promise((resolve) => { finishTrigger = resolve; }));

    renderSection();
    await screen.findByText('Weekly Scan Cleanup');

    fireEvent.click(screen.getByRole('button', { name: 'Run Weekly Scan Cleanup now' }));

    await waitFor(() => expect(triggerTask).toHaveBeenCalledWith('cleanup'));
    expect(screen.getByRole('button', { name: 'Run Weekly Scan Cleanup now' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run Weekly Full Refresh now' })).toBeEnabled();

    const callsBefore = getScheduledTasks.mock.calls.length;
    finishTrigger({ task_id: 'new-run', status: 'queued' });
    await waitFor(() => expect(getScheduledTasks.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it('keeps a just-triggered row blocked until the refreshed list arrives', async () => {
    getScheduledTasks.mockResolvedValueOnce(payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed')),
    ]));
    // The refetch after the trigger never returns.
    getScheduledTasks.mockReturnValue(new Promise(() => {}));
    triggerTask.mockResolvedValue({ task_id: 'new-run', status: 'queued' });

    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Run Weekly Scan Cleanup now' }));

    expect(await within(rowFor('Weekly Scan Cleanup')).findByText('Queued')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run Weekly Scan Cleanup now' })).toBeDisabled();
  });

  it('does not keep a row busy while the slow job console refetches', async () => {
    // The job inventory queries Celery workers and can take many seconds.
    function JobConsoleQuery() {
      useQuery({ queryKey: ['operations', 'jobs'], queryFn: () => new Promise(() => {}) });
      return null;
    }
    getScheduledTasks.mockResolvedValue(payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed')),
    ]));
    triggerTask.mockResolvedValue({ task_id: 'new-run', status: 'queued' });

    renderWithProviders(<><SchedulesSection /><JobConsoleQuery /></>);
    fireEvent.click(await screen.findByRole('button', { name: 'Run Weekly Scan Cleanup now' }));

    // The schedules list has refetched (completed again), so the row is free.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run Weekly Scan Cleanup now' })).toBeEnabled());
  });

  it('keeps every in-flight row blocked when several are triggered', async () => {
    getScheduledTasks.mockResolvedValue(payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed')),
      task('refresh', 'Weekly Full Refresh', lastRun('completed')),
    ]));
    triggerTask.mockReturnValue(new Promise(() => {}));

    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Run Weekly Scan Cleanup now' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run Weekly Full Refresh now' }));

    await waitFor(() => expect(triggerTask).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Run Weekly Scan Cleanup now' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run Weekly Full Refresh now' })).toBeDisabled();
  });

  it.each([
    ['running', 60],
    ['queued', 2],
  ])('blocks a second run while the task is %s (%i min)', async (status, ageMinutes) => {
    getScheduledTasks.mockResolvedValue(payload([
      task('refresh', 'Weekly Full Refresh', lastRun(status, {
        started_at: new Date(Date.now() - ageMinutes * 60 * 1000).toISOString(),
      })),
    ]));

    renderSection();

    const button = await screen.findByRole('button', { name: 'Run Weekly Full Refresh now' });
    expect(button).toBeDisabled();
  });

  it('lets a run that has sat in the queue too long be started again', async () => {
    // A lost broker message or an undeployed worker leaves a run queued until
    // it reads as lost (~25h); the operator must not be locked out meanwhile.
    getScheduledTasks.mockResolvedValue(payload([
      task('refresh', 'Weekly Full Refresh', lastRun('queued', {
        started_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
      })),
    ]));

    renderSection();

    const button = await screen.findByRole('button', { name: 'Run Weekly Full Refresh now' });
    expect(button).toBeEnabled();
    // Says why this Queued row can run while a fresh Queued row cannot.
    expect(within(rowFor('Weekly Full Refresh')).getByText('Not picked up for 30m')).toBeInTheDocument();
  });

  it('does not flag a freshly queued run as stuck', async () => {
    getScheduledTasks.mockResolvedValue(payload([
      task('refresh', 'Weekly Full Refresh', lastRun('queued', {
        started_at: new Date(Date.now() - 2 * 60 * 1000).toISOString(),
      })),
    ]));

    renderSection();
    await screen.findByText('Weekly Full Refresh');

    expect(screen.queryByText(/Not picked up/)).not.toBeInTheDocument();
  });

  it('warns when the list could not be refreshed but keeps the last data', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(['scheduledTasks'], payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed')),
    ]));
    getScheduledTasks.mockRejectedValue(new Error('network down'));

    renderWithProviders(<SchedulesSection />, { queryClient });
    await queryClient.refetchQueries({ queryKey: ['scheduledTasks'] });

    expect(await screen.findByText(/Couldn't refresh scheduled jobs/)).toBeInTheDocument();
    expect(screen.getByText('Weekly Scan Cleanup')).toBeInTheDocument();
  });

  it('shows a dash for an unreadable run time', async () => {
    getScheduledTasks.mockResolvedValue(payload([
      task('cleanup', 'Weekly Scan Cleanup', lastRun('completed', { started_at: 'not-a-date' })),
    ]));

    renderSection();
    await screen.findByText('Weekly Scan Cleanup');

    expect(within(rowFor('Weekly Scan Cleanup')).getByText('—')).toBeInTheDocument();
    expect(screen.queryByText('Just now')).not.toBeInTheDocument();
  });

  it('shows why a run was refused', async () => {
    getScheduledTasks.mockResolvedValue(payload([
      task('social', 'Refresh Social Signals', null),
    ]));
    triggerTask.mockRejectedValue({ response: { data: { detail: 'manual_refresh_cooldown' } } });

    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Run Refresh Social Signals now' }));

    expect(await screen.findByText(/manual_refresh_cooldown/)).toBeInTheDocument();
  });

  describe('on a phone-width screen', () => {
    beforeEach(() => {
      // useMediaQuery(breakpoints.down('md')) matches, as in the Layout tests.
      window.matchMedia = vi.fn().mockReturnValue({
        matches: true,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
    });

    afterEach(() => {
      delete window.matchMedia;
    });

    it('lists jobs as cards instead of a five-column table', async () => {
      getScheduledTasks.mockResolvedValue(payload([
        task('refresh', 'Weekly Full Refresh', lastRun('failed', { duration_seconds: 42, error_message: 'db locked' })),
        task('cleanup', 'Weekly Scan Cleanup', lastRun('running')),
      ]));

      renderSection();

      const list = await screen.findByRole('list', { name: 'Scheduled jobs' });
      expect(screen.queryByRole('table')).not.toBeInTheDocument();
      const card = within(list).getByText('Weekly Full Refresh').closest('li');
      expect(within(card).getByText('Failed')).toBeInTheDocument();
      expect(within(card).getByText('5m ago · 42s')).toBeInTheDocument();
      expect(within(card).getByText('Sunday 2:00 AM ET')).toBeInTheDocument();
      expect(within(card).getByText('db locked')).toBeInTheDocument();
      // Descriptions are dropped on phones; the name and schedule say enough.
      expect(within(card).queryByText('Weekly Full Refresh description')).not.toBeInTheDocument();
      expect(within(card).getByRole('button', { name: 'Run Weekly Full Refresh now' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Run Weekly Scan Cleanup now' })).toBeDisabled();
    });
  });
});
