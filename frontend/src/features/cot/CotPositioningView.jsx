import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  FormControl,
  InputLabel,
  Link,
  MenuItem,
  Paper,
  Select,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material';

import CotPositioningChart from './CotPositioningChart';
import CotSummaryStrip from './CotSummaryStrip';
import { latestFocalSummary, priceStateLabel } from './cotPresentation';
import { PageSkeleton } from '../../components/common/LoadingSkeletons';

const CotPositioningView = ({
  catalog = null,
  history = null,
  selectedSlug = 'sp-500',
  range = '1y',
  onSelectInstrument = () => {},
  onRangeChange = () => {},
  isLoading = false,
  error = null,
}) => {
  const [mode, setMode] = useState('net');
  const [participant, setParticipant] = useState(null);
  const [visibleParticipants, setVisibleParticipants] = useState([]);
  const currentParticipants = history?.weeks?.[0]?.positions || [];

  useEffect(() => {
    const nextParticipants = history?.weeks?.[0]?.positions || [];
    if (!nextParticipants.length) return;
    setParticipant((current) => (
      nextParticipants.some((item) => item.participant === current)
        ? current
        : history.focal_participant
    ));
    setVisibleParticipants(nextParticipants.map((item) => item.participant));
  }, [history]); // Reset controls when the selected market changes.

  const grouped = useMemo(() => {
    const groups = new Map();
    (catalog?.instruments || []).forEach((instrument) => {
      const values = groups.get(instrument.category) || [];
      values.push(instrument);
      groups.set(instrument.category, values);
    });
    return [...groups.entries()];
  }, [catalog]);
  const summary = latestFocalSummary(catalog, history);

  if (isLoading) return <PageSkeleton label="Loading COT positioning" />;
  if (error) return <Alert severity="error">Unable to load COT positioning.</Alert>;
  if (!catalog || !history?.weeks?.length) return <Alert severity="info">No COT history is available.</Alert>;

  const toggleParticipant = (value) => setVisibleParticipants((current) => (
    current.includes(value) ? current.filter((item) => item !== value) : [...current, value]
  ));

  return (
    <Paper variant="outlined" sx={{ p: { xs: 1.5, md: 2 }, minWidth: 0 }}>
      <Stack spacing={2}>
        <Box>
          <Typography variant="h5" component="h2">COT Positioning</Typography>
          <Typography variant="body2" color="text.secondary">
            Weekly CFTC futures positioning with long, short, net, and three-year percentile context.
          </Typography>
        </Box>
        {history.publication.stale && <Alert severity="warning">COT publication is stale; the last valid publication remains active.</Alert>}
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }}>
          <FormControl size="small" sx={{ minWidth: 240 }}>
            <InputLabel id="cot-instrument-label">Market</InputLabel>
            <Select
              native
              labelId="cot-instrument-label"
              label="Market"
              value={selectedSlug}
              onChange={(event) => onSelectInstrument(event.target.value)}
              inputProps={{ 'aria-label': 'COT instrument' }}
            >
              {grouped.map(([category, instruments]) => (
                <optgroup key={category} label={category.replaceAll('_', ' ')}>
                  {instruments.map((instrument) => <option key={instrument.slug} value={instrument.slug}>{instrument.display_name}</option>)}
                </optgroup>
              ))}
            </Select>
          </FormControl>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={range}
            onChange={(_event, value) => value && onRangeChange(value)}
            aria-label="COT history range"
          >
            <ToggleButton value="1y">1Y</ToggleButton>
            <ToggleButton value="3y">3Y</ToggleButton>
            <ToggleButton value="5y">5Y</ToggleButton>
          </ToggleButtonGroup>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={mode}
            onChange={(_event, value) => value && setMode(value)}
            aria-label="COT chart mode"
          >
            <ToggleButton value="net">Net</ToggleButton>
            <ToggleButton value="long_short">Long &amp; Short</ToggleButton>
          </ToggleButtonGroup>
          {mode === 'long_short' && (
            <FormControl size="small" sx={{ minWidth: 220 }}>
              <InputLabel id="cot-participant-label">Participant</InputLabel>
              <Select
                labelId="cot-participant-label"
                label="Participant"
                value={participant || ''}
                onChange={(event) => setParticipant(event.target.value)}
                inputProps={{ 'aria-label': 'CFTC participant' }}
              >
                {currentParticipants.map((item) => <MenuItem key={item.participant} value={item.participant}>{item.label}</MenuItem>)}
              </Select>
            </FormControl>
          )}
        </Stack>
        <CotSummaryStrip summary={summary} />
        <Typography variant="body2" color="text.secondary">
          {priceStateLabel(history.price_mapping_kind, history.price_coverage_state)}
        </Typography>
        {history.price_mapping_kind === 'unavailable' && history.tradingview_url && (
          <Link href={history.tradingview_url} target="_blank" rel="noreferrer">View contract on TradingView</Link>
        )}
        <CotPositioningChart
          history={history}
          mode={mode}
          participant={participant || history.focal_participant}
          visibleParticipants={visibleParticipants}
          onToggleParticipant={toggleParticipant}
        />
        <Typography variant="caption" color="text.secondary">
          Source: CFTC {history.source_dataset_id} · Report {history.publication.report_date} · Retrieved {new Date(history.publication.retrieved_at).toLocaleString()}
        </Typography>
      </Stack>
    </Paper>
  );
};

export default CotPositioningView;
