import { Box, Paper, Skeleton, Stack } from '@mui/material';

// Placeholders shaped like the content that is loading, so the page has its
// final layout immediately and nothing jumps when data arrives. Each is a
// labelled status region, so screen readers hear what is loading.

export function TableSkeleton({ label = 'Loading', rows = 8, columns = 6, title = true }) {
  return (
    <Paper variant="outlined" role="status" aria-busy="true" aria-label={label} sx={{ p: 1.5 }}>
      {title ? <Skeleton variant="text" width={180} height={24} sx={{ mb: 1 }} /> : null}
      <Skeleton variant="rounded" height={28} sx={{ mb: 1 }} />
      <Stack spacing={1}>
        {Array.from({ length: rows }, (_, row) => (
          <Box
            key={row}
            sx={{ display: 'grid', gridTemplateColumns: `2fr repeat(${Math.max(columns - 1, 1)}, 1fr)`, gap: 1.5 }}
          >
            {Array.from({ length: columns }, (_, col) => <Skeleton key={col} variant="text" height={20} />)}
          </Box>
        ))}
      </Stack>
    </Paper>
  );
}

export function PageSkeleton({ label = 'Loading page' }) {
  return (
    <Box role="status" aria-busy="true" aria-label={label} sx={{ py: 1 }}>
      <Skeleton variant="text" width={260} height={36} />
      <Skeleton variant="text" width={180} height={20} sx={{ mb: 2 }} />
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr 1fr', md: 'repeat(4, 1fr)' }, gap: 1.5, mb: 2 }}>
        {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} variant="rounded" height={96} />)}
      </Box>
      <Skeleton variant="rounded" height={320} />
    </Box>
  );
}
