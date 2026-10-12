import { useCallback, useMemo, useRef, useState } from 'react';
import { Alert, Box, Button, Chip, MenuItem, Paper, Stack, TextField, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { buildMatrixModel, groupLabel, sectorLabel } from './groupMatrixModel';
import { MATRIX_METRICS, matrixColor, matrixLegend } from './groupMatrixColors';
import GroupMatrixLayouts from './GroupMatrixLayouts';
import GroupMatrixDetails from './GroupMatrixDetails';
import GroupMatrixTileInspector from './GroupMatrixTileInspector';
import { TableSkeleton } from '../../../components/common/LoadingSkeletons';

const EMPTY_STOCKS = [];
const UNAVAILABLE = {
  missing_ibd_mappings: 'IBD classifications are not available for this market yet.',
  no_published_run: 'No published daily stock snapshot is available yet.',
  no_feature_rows: 'The published snapshot has no stock features.',
  publication_identity_mismatch: 'The stock snapshot could not be matched to a consistent publication.',
};
function MatrixLegend({ metric }) {
  const theme = useTheme();
  const {values, labels} = matrixLegend(metric);
  return <Box aria-label={`${MATRIX_METRICS[metric].label} color legend`} sx={{ display: 'flex', flexWrap: 'wrap', gap: '2px', my: 1 }}>
    {values.map((value, i) => <Box key={value} sx={{ px: 1, py: 0.5, borderRadius: '2px', fontSize: 10,
      bgcolor: matrixColor(value, metric, theme).backgroundColor, color: '#fff' }}>{labels[i]}</Box>)}
    <Box sx={{ fontSize: 11, px: 1, py: 0.5 }}>— Missing</Box>
  </Box>;
}
export default function GroupMatrixPanel({ data, isLoading, error, onRetry, preferences, onPreferencesChange }) {
  const [sector, setSector] = useState('');
  const [group, setGroup] = useState('');
  const [search, setSearch] = useState('');
  const [selection, setSelection] = useState(null);
  const [coverageOpen, setCoverageOpen] = useState(false);
  const triggerRef = useRef(null);
  const listButtonRef = useRef(null);
  const stocks = data?.stocks || EMPTY_STOCKS;
  const visibleTiers = useMemo(() => (data?.tiers || []).filter(t => preferences.tiers.includes(t.id)), [data?.tiers, preferences.tiers]);
  const model = useMemo(() => buildMatrixModel(stocks, {sector, group, search, tiers: preferences.tiers}), [stocks, sector, group, search, preferences.tiers]);
  const sectors = useMemo(() => [...new Set(stocks.map(s => sectorLabel(s.sector)))].sort(), [stocks]);
  const groups = useMemo(() => [...new Set(stocks.filter(s => !sector || sectorLabel(s.sector) === sector).map(s => groupLabel(s.ibd_industry_group)))].sort(), [stocks, sector]);
  const update = next => onPreferencesChange({ ...preferences, ...next });
  const open = useCallback(value => {
    if (!selection) triggerRef.current = document.activeElement;
    setSelection(value);
  }, [selection]);
  const selectStock = useCallback(stock => open({stock}), [open]);
  const close = () => {
    setSelection(null);
    const target = triggerRef.current?.isConnected ? triggerRef.current : listButtonRef.current;
    setTimeout(() => target?.focus(), 0);
  };
  if (isLoading) return <TableSkeleton label="Loading group matrix" />;
  if (error) return <Alert severity="error" action={<Button onClick={onRetry}>Retry</Button>}>Failed to load the stock matrix.</Alert>;
  if (!data?.available) return <Alert severity="info">{UNAVAILABLE[data?.reason] || 'Matrix data is not available in this snapshot.'}</Alert>;

  const coverage = data.coverage;

  return <Paper variant="outlined" sx={{ p: { xs: 1, sm: 2 }, minWidth: 0 }}>
    <Stack direction="row" alignItems="center" justifyContent="space-between" flexWrap="wrap" gap={1}>
      <Box><Typography variant="h6" fontWeight={700}>{data.market} Stock Matrix</Typography>
        <Typography variant="caption" color="text.secondary">Sector · IBD industry · market cap in USD</Typography></Box>
      <Stack direction="row" gap={1} flexWrap="wrap">
        <ToggleButtonGroup size="small" exclusive value={preferences.layout} onChange={(_, v) => v && update({layout:v})} aria-label="Matrix layout">
          <ToggleButton value="grid">Grid</ToggleButton><ToggleButton value="clusters">Clusters</ToggleButton>
        </ToggleButtonGroup>
        <TextField select size="small" label="Color" value={preferences.metric} onChange={e => update({metric:e.target.value})} sx={{ minWidth:160 }}>
          {Object.entries(MATRIX_METRICS).map(([key, value]) => <MenuItem key={key} value={key}>{value.label}</MenuItem>)}
        </TextField>
      </Stack>
    </Stack>
    <Stack direction="row" flexWrap="wrap" gap={1} sx={{ mt: 2 }}>
      <TextField select size="small" label="Sector" value={sector} sx={{ minWidth: 180 }} onChange={e => {
        const next = e.target.value; setSector(next);
        if (group && !stocks.some(s => (!next || sectorLabel(s.sector) === next) && groupLabel(s.ibd_industry_group) === group)) setGroup('');
      }}><MenuItem value="">All sectors</MenuItem>{sectors.map(v => <MenuItem key={v} value={v}>{v}</MenuItem>)}</TextField>
      <TextField select size="small" label="IBD industry" value={group} onChange={e => setGroup(e.target.value)} sx={{ minWidth: 200, maxWidth: '100%' }}>
        <MenuItem value="">All IBD groups</MenuItem>{groups.map(v => <MenuItem key={v} value={v}>{v}</MenuItem>)}</TextField>
      <TextField size="small" label="Search symbol or company" value={search} onChange={e => setSearch(e.target.value)} sx={{ flex: 1, minWidth: 210 }} />
      <Button onClick={() => {setSector(''); setGroup(''); setSearch(''); update({tiers:data.tiers.map(t=>t.id)});}}>Reset filters</Button>
    </Stack>
    <Stack direction="row" flexWrap="wrap" gap={0.75} sx={{ my: 1.5 }} aria-label="Market cap tiers">
      {data.tiers.map(tier => <Chip key={tier.id} label={tier.label} clickable sx={{ minHeight: { xs: 44, sm: 32 } }} variant={preferences.tiers.includes(tier.id) ? 'filled' : 'outlined'}
        color={preferences.tiers.includes(tier.id) ? 'primary' : 'default'} aria-pressed={preferences.tiers.includes(tier.id)}
        onClick={() => update({tiers:preferences.tiers.includes(tier.id) ? preferences.tiers.filter(t=>t!==tier.id) : [...preferences.tiers,tier.id]})} />)}
    </Stack>
    <Typography variant="caption" color="text.secondary">Daily data: {data.as_of_date} · Metadata read: {data.metadata_read_at?.slice(0,10)} · Latest stored metadata, not intraday quotes</Typography>
    <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap">
      <Typography variant="body2" aria-live="polite">{model.stockCount.toLocaleString()} shown / {coverage.stock_count.toLocaleString()} stocks · IBD coverage {coverage.stock_count ? Math.round(coverage.ibd_mapped_count / coverage.stock_count * 100) : 0}%</Typography>
      <Button size="small" onClick={() => setCoverageOpen(v=>!v)} aria-expanded={coverageOpen}>Data coverage</Button>
      <Button size="small" ref={listButtonRef} onClick={() => open({title:'Matching stocks · current filters', stocks:model.stocks})}>View matching stocks</Button>
    </Stack>
    {coverageOpen && <Alert severity="info" sx={{ my: 1 }}>Universe: {coverage.universe_count}; missing features: {coverage.missing_feature_count || 0}; unknown sector: {coverage.unknown_sector_count || 0}; unknown cap: {coverage.unknown_cap_count || 0}; missing daily change: {coverage.missing_daily_change_count || 0}; missing weekly change: {coverage.missing_weekly_change_count ?? stocks.filter(stock=>!Number.isFinite(stock.price_change_1w)).length}; missing monthly change: {coverage.missing_monthly_change_count ?? stocks.filter(stock=>!Number.isFinite(stock.price_change_1m)).length}; missing RS: {coverage.missing_rs_count || 0}. Counts can overlap. Automated IBD mappings are estimates; inspect stock details for source.</Alert>}
    <MatrixLegend metric={preferences.metric} />
    {model.stockCount ? <GroupMatrixTileInspector stocks={model.stocks} data={data}><GroupMatrixLayouts layout={preferences.layout} model={model} tiers={visibleTiers} metric={preferences.metric} onSelectStock={selectStock} onSelectStocks={open} /></GroupMatrixTileInspector>
      : <Alert severity="info">No stocks match these filters. Reset filters to see the full matrix.</Alert>}
    <GroupMatrixDetails selection={selection} onClose={close} onSelectStock={stock=>setSelection({stock})} onSelectStocks={setSelection} matchingStocks={model.stocks} data={data} />
  </Paper>;
}
