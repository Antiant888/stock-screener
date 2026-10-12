import { Box, Typography } from '@mui/material';
import PriceSparkline from '../Scan/PriceSparkline';
import { formatLocalCurrency } from '../../utils/formatUtils';

const oneDecimal = (value) => (Number.isFinite(Number(value)) && value != null ? Number(value).toFixed(1) : '-');

/**
 * Phone-width stand-in for a stock table row: everything a reader scans for
 * (symbol, score, price, 1D change, trend, group) in ~370px, no sideways scroll.
 *
 * When `onOpen` is set, only the symbol is a real button ("Open FORM chart");
 * its ::after overlay stretches over the card so the whole card is the tap
 * target, while the rest of the card stays readable to screen readers and
 * `badge` (e.g. a clickable action state) sits above the overlay as a sibling,
 * not nested inside the button.
 */
export default function StockRowCard({
  row,
  // Unlabelled value for the pill (Daily lists pass e.g. resilience_score).
  scoreField = 'composite_score',
  // Optional labelled override, already formatted: { label, value, name }
  // (the Scan list passes its active sort's value).
  metric = null,
  badge = null,
  actions = null,
  meta = null,
  onOpen = null,
}) {
  const shown = metric || { label: null, value: oneDecimal(row[scoreField]), name: 'Score' };
  const change = Number(row.price_change_1d);
  const hasChange = row.price_change_1d != null && Number.isFinite(change);
  // Rank first: long industry names truncate, and the rank is the part worth keeping.
  const group = [row.ibd_group_rank != null ? `Grp #${row.ibd_group_rank}` : null, row.ibd_industry_group, meta]
    .filter(Boolean)
    .join(' · ');

  return (
    <Box
      sx={{
        position: 'relative',
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) 88px 72px',
        alignItems: 'center',
        gap: 1.25,
        px: 1.5,
        py: 1,
        cursor: onOpen ? 'pointer' : 'default',
        '&:hover': onOpen ? { backgroundColor: 'action.hover' } : undefined,
      }}
    >
      <Box sx={{ minWidth: 0 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
          {onOpen ? (
            <Box
              component="button"
              type="button"
              onClick={onOpen}
              aria-label={`Open ${row.symbol} chart`}
              sx={{
                all: 'unset',
                // The ticker keeps its width; the badge beside it truncates instead.
                flexShrink: 0,
                maxWidth: '70%',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontWeight: 700,
                fontSize: '14px',
                // Stretch the button's hit area over the whole card.
                '&::after': { content: '""', position: 'absolute', inset: 0 },
                '&:focus-visible::after': { outline: 2, outlineColor: 'primary.main', outlineOffset: -2 },
              }}
            >
              {row.symbol}
            </Box>
          ) : (
            <Typography component="span" noWrap sx={{ fontWeight: 700, fontSize: '14px', flexShrink: 0, maxWidth: '70%' }}>{row.symbol}</Typography>
          )}
          {badge || actions ? (
            // Above the stretched overlay, but only interactive children take the
            // tap; an inert badge (a rating chip) lets it fall through to open.
            <Box
              sx={{
                position: 'relative',
                zIndex: 1,
                minWidth: 0,
                pointerEvents: 'none',
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                '& button, & a, & [role="button"]': { pointerEvents: 'auto' },
              }}
            >
              {badge ? (
                <Box
                  sx={{
                    minWidth: 0,
                    overflow: 'hidden',
                    pointerEvents: 'none',
                    '& .MuiChip-root': { maxWidth: '100%' },
                    '& button, & a, & [role="button"]': { pointerEvents: 'auto' },
                  }}
                >
                  {badge}
                </Box>
              ) : null}
              {actions ? <Box sx={{ flexShrink: 0, display: 'flex' }}>{actions}</Box> : null}
            </Box>
          ) : null}
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
          aria-label={`${shown.name} ${shown.value}`}
          sx={{ display: 'inline-block', px: 0.75, borderRadius: 1, bgcolor: 'action.selected', fontSize: '11px', fontWeight: 700 }}
        >
          {shown.label ? `${shown.label} ` : ''}{shown.value}
        </Typography>
        <Typography sx={{ fontSize: '13px', fontWeight: 600, mt: 0.25 }}>
          {formatLocalCurrency(row.current_price, row.currency)}
        </Typography>
        <Typography
          sx={{
            fontSize: '11.5px',
            color: hasChange && change > 0 ? 'success.main' : hasChange && change < 0 ? 'error.main' : 'text.secondary',
          }}
        >
          {hasChange ? `${change > 0 ? '+' : ''}${change.toFixed(1)}%` : '-'}
        </Typography>
      </Box>
    </Box>
  );
}
