/**
 * Scheduled jobs on the Operations page (replaces the header gear modal).
 *
 * Run status comes from the server's task history, which the worker writes
 * (#588), so a run started here or by Beat shows up here whether or not this
 * page was open; there is no client-side tracking of triggered runs.
 */
import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  IconButton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import { getScheduledTasks, triggerTask } from '../../api/tasks';

const ACTIVE_STATUSES = new Set(['queued', 'running']);
// A run still queued after this has likely lost its message or has no worker
// on its queue; it would otherwise block Run until it reads as lost (~25h).
const STALE_QUEUED_MS = 10 * 60 * 1000;

const STATUS_DISPLAY = {
  completed: { color: 'success', label: 'Completed' },
  running: { color: 'info', label: 'Running' },
  queued: { color: 'warning', label: 'Queued' },
  failed: { color: 'error', label: 'Failed' },
  cancelled: { color: 'default', label: 'Cancelled' },
  // No outcome recorded within Celery's hard time limit: never picked up, or
  // the worker died without a stored result. Not known to have failed.
  lost: { color: 'warning', label: 'Unknown' },
};

function ageMs(dateString) {
  return Date.now() - new Date(dateString).getTime();
}

function formatRelativeTime(dateString) {
  const diffMin = Math.floor(ageMs(dateString) / 60000);
  if (Number.isNaN(diffMin)) return '—';
  if (diffMin >= 1440) return `${Math.floor(diffMin / 1440)}d ago`;
  if (diffMin >= 60) return `${Math.floor(diffMin / 60)}h ago`;
  if (diffMin >= 1) return `${diffMin}m ago`;
  return 'Just now';
}

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// Why Run is unavailable for this run, or null when it may run again.
function runBlockedReason(run) {
  if (run?.status === 'running') return 'Already running';
  if (run?.status === 'queued' && !(ageMs(run.started_at) >= STALE_QUEUED_MS)) return 'Already queued';
  return null;
}

function LastRunCell({ run }) {
  if (!run) return <Typography variant="body2" color="text.secondary">Never</Typography>;
  const when = run.started_at ? formatRelativeTime(run.started_at) : '—';
  const label = run.duration_seconds != null ? `${when} · ${formatDuration(run.duration_seconds)}` : when;
  return (
    <Tooltip title={run.triggered_by ? `Triggered: ${run.triggered_by}` : ''}>
      <Typography variant="body2">{label}</Typography>
    </Tooltip>
  );
}

function StatusCell({ run }) {
  if (!run) return <Typography variant="body2" color="text.secondary">—</Typography>;
  const display = STATUS_DISPLAY[run.status] || { color: 'default', label: run.status };
  return (
    <Box>
      <Chip
        size="small"
        color={display.color}
        label={display.label}
        icon={ACTIVE_STATUSES.has(run.status) ? <CircularProgress size={12} color="inherit" /> : undefined}
      />
      {run.status === 'failed' && run.error_message && (
        <Typography
          variant="caption"
          color="error"
          sx={{ display: 'block', mt: 0.5, maxWidth: 320, overflowWrap: 'anywhere' }}
        >
          {run.error_message}
        </Typography>
      )}
    </Box>
  );
}

function markQueued(data, taskName, taskId) {
  if (!data?.tasks) return data;
  return {
    ...data,
    tasks: data.tasks.map((task) => (task.name !== taskName ? task : {
      ...task,
      last_run: {
        ...task.last_run,
        task_id: taskId,
        status: 'queued',
        started_at: new Date().toISOString(),
        completed_at: null,
        duration_seconds: null,
        triggered_by: 'manual',
        error_message: null,
      },
    })),
  };
}

/**
 * @param {boolean} contentAboveReady - false while sections above this one
 *   are still loading; the #schedules scroll repeats once when it turns true
 *   so late content above does not push the section out of view.
 */
export default function SchedulesSection({ contentAboveReady = true }) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const headingRef = useRef(null);
  const [error, setError] = useState(null);
  const [pendingNames, setPendingNames] = useState(() => new Set());

  useEffect(() => {
    // React Router does not scroll to hash targets; the header gear links here.
    if (location.hash === '#schedules') headingRef.current?.scrollIntoView({ block: 'start' });
  }, [location.hash, location.key, contentAboveReady]);

  const tasksQuery = useQuery({
    queryKey: ['scheduledTasks'],
    queryFn: getScheduledTasks,
    refetchInterval: (query) => (
      query.state.data?.tasks?.some((task) => ACTIVE_STATUSES.has(task.last_run?.status)) ? 5000 : 30000
    ),
  });

  const setPending = (taskName, isPending) => setPendingNames((current) => {
    const next = new Set(current);
    if (isPending) next.add(taskName);
    else next.delete(taskName);
    return next;
  });

  const runMutation = useMutation({
    mutationFn: (taskName) => triggerTask(taskName),
    onMutate: (taskName) => {
      setError(null);
      setPending(taskName, true);
    },
    onSuccess: (response, taskName) => {
      // The server has committed a queued row; show it now so Run stays
      // blocked until the refetch confirms it.
      queryClient.setQueryData(['scheduledTasks'], (data) => markQueued(data, taskName, response?.task_id));
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: ['scheduledTasks'] }),
        queryClient.invalidateQueries({ queryKey: ['operations', 'jobs'] }),
      ]);
    },
    onError: (err) => setError(err?.response?.data?.detail || err?.message || 'Failed to trigger task'),
    onSettled: (_data, _error, taskName) => setPending(taskName, false),
  });
  const tasks = tasksQuery.data?.tasks ?? [];

  return (
    <Box sx={{ mt: 3 }}>
      <Typography id="schedules" ref={headingRef} variant="h6" sx={{ mb: 1 }}>
        Scheduled jobs
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        Beat schedules and their last run. Run now queues the job immediately; it then appears in the job console above.
      </Typography>
      {error && (
        <Alert severity="error" sx={{ mb: 1.5 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {tasksQuery.isError && tasks.length > 0 && (
        <Alert severity="warning" sx={{ mb: 1.5 }}>
          Couldn&apos;t refresh scheduled jobs. Showing the last known state.
        </Alert>
      )}
      {tasksQuery.isLoading ? (
        <CircularProgress size={20} />
      ) : tasksQuery.isError && tasks.length === 0 ? (
        <Typography variant="body2" color="error">Failed to load scheduled tasks.</Typography>
      ) : tasks.length === 0 ? (
        <Typography variant="body2" color="text.secondary">No scheduled tasks configured.</Typography>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Job</TableCell>
              <TableCell>Schedule</TableCell>
              <TableCell>Last run</TableCell>
              <TableCell>Status</TableCell>
              <TableCell align="center">Run</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {tasks.map((task) => {
              const blockedReason = runBlockedReason(task.last_run);
              const pending = pendingNames.has(task.name);
              return (
                <TableRow key={task.name} hover>
                  <TableCell>
                    <Typography variant="body2" fontWeight="medium">{task.display_name}</Typography>
                    <Typography variant="caption" color="text.secondary">{task.description}</Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">{task.schedule_description}</Typography>
                  </TableCell>
                  <TableCell><LastRunCell run={task.last_run} /></TableCell>
                  <TableCell><StatusCell run={task.last_run} /></TableCell>
                  <TableCell align="center">
                    <Tooltip title={blockedReason || 'Run this job now'}>
                      <span>
                        <IconButton
                          size="small"
                          color="primary"
                          aria-label={`Run ${task.display_name} now`}
                          onClick={() => runMutation.mutate(task.name)}
                          disabled={Boolean(blockedReason) || pending}
                        >
                          {pending ? <CircularProgress size={18} /> : <PlayArrowIcon />}
                        </IconButton>
                      </span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </Box>
  );
}
