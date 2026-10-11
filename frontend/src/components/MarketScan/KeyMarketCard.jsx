import { Box, Paper, Typography } from '@mui/material';
import PriceSparkline from '../Scan/PriceSparkline';
import { formatLocalCurrency } from '../../utils/formatUtils';

const toneOf = (value) => (value > 0 ? 'success.main' : value < 0 ? 'error.main' : 'text.secondary');
const signed = (value) => `${value > 0 ? '+' : ''}${Number(value).toFixed(2)}%`;

// Index/ETF tile on the Daily page. The 1D change and the sparkline's trend are
// labelled separately: a green day inside a falling window must not read as a
// contradiction.
export default function KeyMarketCard({ item, closes, currency }) {
  const first = closes[0];
  const last = closes[closes.length - 1];
  const trend = Math.sign(last - first);
  const periodChange = first ? (last / first - 1) * 100 : null;

  return (
    <Paper
      elevation={0}
      sx={{ p: 1.5, height: '100%', border: 1, borderColor: 'divider', display: 'flex', flexDirection: 'column' }}
    >
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 1 }}>
        <Typography noWrap sx={{ fontWeight: 700, fontSize: '13px', minWidth: 0 }}>
          {item.symbol}
        </Typography>
        <Typography sx={{ color: toneOf(item.change_1d), fontWeight: 600, fontSize: '12px', whiteSpace: 'nowrap' }}>
          <Box component="span" sx={{ color: 'text.secondary', fontWeight: 400 }}>1D </Box>
          {item.change_1d != null ? signed(item.change_1d) : '-'}
        </Typography>
      </Box>
      <Typography variant="caption" noWrap sx={{ color: 'text.secondary' }}>
        {item.display_name}
      </Typography>
      <Typography sx={{ mt: 0.5, fontSize: '17px', fontWeight: 600 }}>
        {formatLocalCurrency(item.latest_close, currency)}
      </Typography>
      <Box sx={{ mt: 0.75, flex: 1, minHeight: 36 }}>
        <PriceSparkline
          data={closes}
          trend={trend}
          change1d={null}
          width="100%"
          height={36}
          showChange={false}
        />
      </Box>
      {periodChange != null && (
        <Typography
          variant="caption"
          sx={{ alignSelf: 'flex-end', mt: 0.25, color: toneOf(periodChange), fontWeight: 600 }}
        >
          {`${closes.length}D ${periodChange >= 0 ? '▲' : '▼'} ${Math.abs(periodChange).toFixed(1)}%`}
        </Typography>
      )}
    </Paper>
  );
}
