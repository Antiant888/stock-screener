import { Box, Grid, Paper, Typography, useTheme } from '@mui/material';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

function ChartFrame({ title, children }) {
  return (
    <Paper variant="outlined" sx={{ p: 1.5, height: 280 }}>
      <Typography variant="subtitle2" sx={{ mb: 1 }}>{title}</Typography>
      <Box sx={{ height: 225 }}>{children}</Box>
    </Paper>
  );
}

export default function OptionsStrikeCharts({ points, spotPrice }) {
  // Calls in the theme's gain colour, puts in its loss colour, across all three charts.
  const { palette } = useTheme();
  const call = palette.success.main;
  const put = palette.error.main;
  const guide = palette.text.secondary;
  return (
    <Grid container spacing={2}>
      <Grid item xs={12} lg={4}>
        <ChartFrame title="Open interest and volume">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={points}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="strike" />
              <YAxis />
              <Tooltip />
              <ReferenceLine x={spotPrice} stroke={guide} strokeDasharray="3 3" />
              <Bar dataKey="call_open_interest" name="Call OI" fill={call} />
              <Bar dataKey="put_open_interest" name="Put OI" fill={put} />
              <Bar dataKey="call_volume" name="Call volume" fill={call} fillOpacity={0.45} />
              <Bar dataKey="put_volume" name="Put volume" fill={put} fillOpacity={0.45} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      </Grid>
      <Grid item xs={12} lg={4}>
        <ChartFrame title="Estimated GEX by strike">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={points}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="strike" />
              <YAxis />
              <Tooltip />
              <ReferenceLine y={0} stroke={guide} />
              <Bar dataKey="estimated_call_gex" name="Estimated call GEX" fill={call} />
              <Bar dataKey="estimated_put_gex" name="Estimated put GEX" fill={put} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      </Grid>
      <Grid item xs={12} lg={4}>
        <ChartFrame title="Implied volatility smile">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="strike" />
              <YAxis tickFormatter={(value) => `${Math.round(value * 100)}%`} />
              <Tooltip formatter={(value) => `${(value * 100).toFixed(1)}%`} />
              <ReferenceLine x={spotPrice} stroke={guide} strokeDasharray="3 3" />
              <Line dataKey="call_iv" name="Call IV" stroke={call} connectNulls={false} />
              <Line dataKey="put_iv" name="Put IV" stroke={put} connectNulls={false} />
            </LineChart>
          </ResponsiveContainer>
        </ChartFrame>
      </Grid>
    </Grid>
  );
}
