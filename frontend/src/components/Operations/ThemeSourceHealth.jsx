import {
  CircularProgress,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material';
import { activeSources, failingSources, useContentSources } from './contentSources';

function formatTime(value) {
  return value ? new Date(value).toLocaleString() : 'Never';
}

export default function ThemeSourceHealth() {
  const { data, isLoading, isError } = useContentSources();
  const active = activeSources(data);
  const failing = failingSources(data);

  return (
    <div>
      <Typography variant="h6" sx={{ mb: 1 }}>
        Theme content sources
      </Typography>
      {isLoading ? (
        <CircularProgress size={20} />
      ) : isError ? (
        <Typography variant="body2" color="error">
          Failed to load theme content sources.
        </Typography>
      ) : (
        <>
          <Typography
            variant="body2"
            color={failing.length === 0 ? 'success.main' : 'text.secondary'}
            sx={{ mb: 1.5 }}
          >
            {active.length} active · {failing.length === 0 ? 'all healthy' : `${failing.length} failing`}
          </Typography>
          {failing.length > 0 && (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Source</TableCell>
                  <TableCell>Failures</TableCell>
                  <TableCell>Last success</TableCell>
                  <TableCell>Last error</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {failing.map((source) => (
                  <TableRow key={source.id}>
                    <TableCell>
                      <Typography variant="body2">{source.name}</Typography>
                      <Typography variant="caption" color="text.secondary">
                        {source.source_type}
                      </Typography>
                    </TableCell>
                    <TableCell>{source.consecutive_failures}× in a row</TableCell>
                    <TableCell>{formatTime(source.last_success_at)}</TableCell>
                    <TableCell sx={{ maxWidth: 420 }}>
                      <Typography variant="body2" noWrap title={source.last_error || undefined}>
                        {source.last_error || '—'}
                      </Typography>
                      {source.last_error_at && (
                        <Typography variant="caption" color="text.secondary">
                          {formatTime(source.last_error_at)}
                        </Typography>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}
    </div>
  );
}
