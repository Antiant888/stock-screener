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

function formatRelativeTime(dateString) {
  const diffMin = Math.floor((Date.now() - new Date(dateString)) / 60000);
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

export default function SchedulesSection() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const headingRef = useRef(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    // React Router does not scroll to hash targets; the header gear links here.
    if (location.hash === '#schedules') headingRef.current?.scrollIntoView({ block: 'start' });
  }, [location.hash, location.key]);

  const tasksQuery = useQuery({
    queryKey: ['scheduledTasks'],
    queryFn: getScheduledTasks,
    refetchInterval: (query) => (
      query.state.data?.tasks?.some((task) => ACTIVE_STATUSES.has(task.last_run?.status)) ? 5000 : 30000
    ),
  });

  const runMutation = useMutation({
    mutationFn: (taskName) => triggerTask(taskName),
    onMutate: () => setError(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scheduledTasks'] });
      queryClient.invalidateQueries({ queryKey: ['operations', 'jobs'] });
    },
    onError: (err) => setError(err?.response?.data?.detail || err?.message || 'Failed to trigger task'),
  });
  const pendingName = runMutation.isPending ? runMutation.variables : null;
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
              const active = ACTIVE_STATUSES.has(task.last_run?.status);
              const pending = pendingName === task.name;
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
                    <Tooltip title={active ? 'Already queued or running' : 'Run this job now'}>
                      <span>
                        <IconButton
                          size="small"
                          color="primary"
                          aria-label={`Run ${task.display_name} now`}
                          onClick={() => runMutation.mutate(task.name)}
                          disabled={active || pending}
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
