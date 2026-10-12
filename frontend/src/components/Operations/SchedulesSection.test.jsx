import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
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

function renderSection(path = '/operations') {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <SchedulesSection />
    </MemoryRouter>,
  );
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
    // No recorded outcome is not a failure.
    expect(within(rowFor('Monthly Price Data Cleanup')).getByText('Unknown')).toBeInTheDocument();
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

  it.each(['queued', 'running'])('blocks a second run while the task is %s', async (status) => {
    getScheduledTasks.mockResolvedValue(payload([
      task('refresh', 'Weekly Full Refresh', lastRun(status)),
    ]));

    renderSection();

    const button = await screen.findByRole('button', { name: 'Run Weekly Full Refresh now' });
    expect(button).toBeDisabled();
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

  describe('when linked from the scheduled-tasks gear', () => {
    const scrollIntoView = vi.fn();

    beforeEach(() => {
      scrollIntoView.mockReset();
      Element.prototype.scrollIntoView = scrollIntoView;
      getScheduledTasks.mockResolvedValue(payload([]));
    });

    afterEach(() => {
      delete Element.prototype.scrollIntoView;
    });

    it('scrolls itself into view for the #schedules link', () => {
      renderSection('/operations#schedules');

      expect(screen.getByRole('heading', { name: 'Scheduled jobs' })).toHaveAttribute('id', 'schedules');
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });

    it('stays put without the hash', () => {
      renderSection('/operations');

      expect(scrollIntoView).not.toHaveBeenCalled();
    });
  });
});
