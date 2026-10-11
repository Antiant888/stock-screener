import { Box, Typography } from '@mui/material';
import PriceSparkline from '../Scan/PriceSparkline';
import { formatLocalCurrency } from '../../utils/formatUtils';

const oneDecimal = (value) => (value == null ? '-' : Number(value).toFixed(1));

/**
 * Phone-width stand-in for a stock table row: everything a reader scans for
 * (symbol, score, price, 1D change, trend, group) in ~370px, no sideways scroll.
 * `badge` sits next to the symbol (e.g. an action state); `onOpen` makes the
 * card open the chart by click or Enter/Space, like the table rows do.
 */
export default function StockRowCard({ row, scoreField = 'composite_score', badge = null, meta = null, onOpen = null }) {
  const change = row.price_change_1d;
  // Rank first: long industry names truncate, and the rank is the part worth keeping.
  const group = [row.ibd_group_rank != null ? `Grp #${row.ibd_group_rank}` : null, row.ibd_industry_group, meta]
    .filter(Boolean)
    .join(' · ');

  return (
    <Box
      role={onOpen ? 'button' : undefined}
      tabIndex={onOpen ? 0 : undefined}
      aria-label={onOpen ? `Open ${row.symbol} chart` : undefined}
      onClick={onOpen || undefined}
      onKeyDown={onOpen ? (event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpen();
        }
      } : undefined}
      sx={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) 88px 72px',
        alignItems: 'center',
        gap: 1.25,
        px: 1.5,
        py: 1,
        cursor: onOpen ? 'pointer' : 'default',
        '&:hover': onOpen ? { backgroundColor: 'action.hover' } : undefined,
        '&:focus-visible': { outline: 2, outlineColor: 'primary.main', outlineOffset: -2 },
      }}
    >
      <Box sx={{ minWidth: 0 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
          <Typography component="span" sx={{ fontWeight: 700, fontSize: '14px' }}>{row.symbol}</Typography>
          {badge}
        </Box>
        {row.company_name ? (
          <Typography noWrap sx={{ fontSize: '12px', color: 'text.secondary' }}>{row.company_name}</Typography>
        ) : null}
        {group ? (
          <Typography noWrap sx={{ fontSize: '11px', color: 'text.disabled' }}>{group}</Typography>
        ) : null}
      </Box>

      <Box>
        {row.price_sparkline_data ? (
          <PriceSparkline
            data={row.price_sparkline_data}
            trend={row.price_trend}
            change1d={null}
            industry={row.ibd_industry_group}
            width={88}
            height={32}
            showChange={false}
          />
        ) : null}
      </Box>

      <Box sx={{ textAlign: 'right' }}>
        <Typography
          component="span"
          aria-label={`Score ${oneDecimal(row[scoreField])}`}
          sx={{ display: 'inline-block', px: 0.75, borderRadius: 1, bgcolor: 'action.selected', fontSize: '11px', fontWeight: 700 }}
        >
          {oneDecimal(row[scoreField])}
        </Typography>
        <Typography sx={{ fontSize: '13px', fontWeight: 600, mt: 0.25 }}>
          {formatLocalCurrency(row.current_price, row.currency)}
        </Typography>
        <Typography
          sx={{
            fontSize: '11.5px',
            color: change > 0 ? 'success.main' : change < 0 ? 'error.main' : 'text.secondary',
          }}
        >
          {change == null ? '-' : `${change > 0 ? '+' : ''}${Number(change).toFixed(1)}%`}
        </Typography>
      </Box>
    </Box>
  );
}
