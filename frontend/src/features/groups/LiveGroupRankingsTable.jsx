import {
  Box,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TableSortLabel,
} from '@mui/material';
import TrendingUpIcon from '@mui/icons-material/TrendingUp';
import TrendingDownIcon from '@mui/icons-material/TrendingDown';
import {
  LIVE_GROUP_RANKING_COLUMNS,
  deriveHistoricalRank,
  derivePctRsAbove80,
  formatGroupRs,
} from './groupRankingFields';
import { groupRsCellSx, groupRsTone } from './groupRsVisualEncoding';

const RankChangeCell = ({ value }) => {
  if (value === null || value === undefined) {
    return <Box sx={{ color: 'text.secondary', fontFamily: 'monospace' }}>-</Box>;
  }
  const color = value > 0 ? 'success.main' : value < 0 ? 'error.main' : 'text.secondary';
  const prefix = value > 0 ? '+' : '';
  return (
    <Box display="flex" alignItems="center" justifyContent="flex-end" sx={{ fontFamily: 'monospace' }}>
      {value > 0 && <TrendingUpIcon sx={{ fontSize: 12, mr: 0.25, color }} />}
      {value < 0 && <TrendingDownIcon sx={{ fontSize: 12, mr: 0.25, color }} />}
      <Box component="span" sx={{ color, fontWeight: value !== 0 ? 600 : 400, fontSize: '11px' }}>
        {prefix}{value}
      </Box>
    </Box>
  );
};

const HistoricalRankCell = ({ row, rankChange }) => {
  const historicalRank = deriveHistoricalRank(row, rankChange);
  if (historicalRank == null) {
    return <Box sx={{ color: 'text.secondary', fontFamily: 'monospace', fontSize: '11px' }}>-</Box>;
  }
  return (
    <Box sx={{ fontFamily: 'monospace', fontSize: '11px', textAlign: 'right' }}>
      {historicalRank}
    </Box>
  );
};

const SortableHeaderCell = ({ column, order, orderBy, onSort }) => (
  <TableCell align={column.align}>
    <TableSortLabel
      active={orderBy === column.field}
      direction={orderBy === column.field ? order : 'asc'}
      onClick={() => onSort(column.field)}
      title={column.description}
    >
      {column.label}
    </TableSortLabel>
  </TableCell>
);

const RankBadge = ({ rank }) => (
  <Box
    component="span"
    sx={{
      backgroundColor: rank <= 20 ? 'success.main' : rank >= 177 ? 'error.main' : 'warning.main',
      color: rank <= 20 ? 'success.contrastText' : rank >= 177 ? 'error.contrastText' : 'warning.contrastText',
      padding: '1px 5px',
      borderRadius: '2px',
      fontSize: '10px',
      fontWeight: 600,
      fontFamily: 'monospace',
    }}
  >
    {rank}
  </Box>
);

const LiveRankingCell = ({ row, column, showHistoricalRanks }) => {
  if (column.kind === 'rank') {
    return (
      <TableCell>
        <RankBadge rank={row.rank} />
      </TableCell>
    );
  }

  if (column.kind === 'group') {
    return (
      <TableCell sx={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {row.industry_group}
      </TableCell>
    );
  }

  if (column.kind === 'rankChange') {
    return (
      <TableCell align={column.align}>
        {showHistoricalRanks ? (
          <HistoricalRankCell row={row} rankChange={row[column.field]} />
        ) : (
          <RankChangeCell value={row[column.field]} />
        )}
      </TableCell>
    );
  }

  if (column.kind === 'pctAbove80') {
    const pctRsAbove80 = derivePctRsAbove80(row);
    return (
      <TableCell align={column.align} sx={{ fontFamily: 'monospace' }}>
        {pctRsAbove80 != null ? `${pctRsAbove80.toFixed(1)}%` : '-'}
      </TableCell>
    );
  }

  if (column.kind === 'topStock') {
    return (
      <TableCell align={column.align} sx={{ color: 'text.secondary', fontWeight: 500 }}>
        {row.top_symbol || '-'}
      </TableCell>
    );
  }

  if (column.kind === 'integer') {
    return (
      <TableCell align={column.align} sx={{ fontFamily: 'monospace' }}>
        {row[column.field]}
      </TableCell>
    );
  }

  const value = row[column.field];
  const isRsRating = column.kind === 'rs';
  return (
    <TableCell
      align={column.align}
      data-rs-tone={isRsRating ? groupRsTone(value) : undefined}
      sx={{
        fontFamily: 'monospace',
        ...(isRsRating && groupRsCellSx(value)),
      }}
    >
      {formatGroupRs(value)}
    </TableCell>
  );
};

export default function LiveGroupRankingsTable({
  rankings,
  order,
  orderBy,
  onSort,
  onSelectGroup,
  showHistoricalRanks,
}) {
  return (
    <TableContainer sx={{ maxHeight: 'calc(100vh - 200px)', flexGrow: 1 }}>
      <Table stickyHeader size="small" sx={{ minWidth: 1120 }}>
        <TableHead>
          <TableRow>
            {LIVE_GROUP_RANKING_COLUMNS.map((column) => (
              <SortableHeaderCell
                key={column.field}
                column={column}
                order={order}
                orderBy={orderBy}
                onSort={onSort}
              />
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          {rankings.map((row) => (
            <TableRow
              key={row.industry_group}
              hover
              onClick={() => onSelectGroup(row.industry_group)}
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelectGroup(row.industry_group);
                }
              }}
              sx={{ cursor: 'pointer' }}
            >
              {LIVE_GROUP_RANKING_COLUMNS.map((column) => (
                <LiveRankingCell
                  key={column.field}
                  row={row}
                  column={column}
                  showHistoricalRanks={showHistoricalRanks}
                />
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
