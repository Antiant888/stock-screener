import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Container,
  Paper,
  Typography,
} from '@mui/material';
import { format, parseISO } from 'date-fns';

import {
  getBreadthContributorIndex,
  getBreadthContributors,
  getBreadthSummary,
  getCurrentBreadth,
  getHistoricalBreadth,
} from '../api/breadth';
import { getPriceHistory } from '../api/stocks';
import BreadthContextStrip from '../components/Breadth/BreadthContextStrip';
import BreadthContributorDialog from '../components/Breadth/BreadthContributorDialog';
import BreadthHistoryTable from '../components/Breadth/BreadthHistoryTable';
import { useBreadthContributors } from '../components/Breadth/useBreadthContributors';
import BreadthChart from '../components/Charts/BreadthChart';
import { useMarketForCapability } from '../contexts/MarketContext';
import { useRuntime } from '../contexts/RuntimeContext';
import {
  BREADTH_MARKET_FALLBACKS,
  MARKET_LIVE_BENCHMARK_SYMBOLS,
  breadthBootstrapQuery,
  breadthHistoryWindow,
  getBreadthDateRange,
} from '../pageBootstrapQueries';

function BreadthPage() {
  const { runtimeReady } = useRuntime();
  const queryClient = useQueryClient();
  const { market: selectedMarket } = useMarketForCapability(
    'breadth',
    BREADTH_MARKET_FALLBACKS,
  );
  const [chartTimeRange, setChartTimeRange] = useState('1M');
  const chartDateRange = getBreadthDateRange(chartTimeRange);
  const { startDate, endDate } = breadthHistoryWindow();
  const periodMap = { '1M': '1mo', '3M': '3mo', '6M': '6mo', '1Y': '1y' };
  const benchmarkPeriod = periodMap[chartTimeRange] || '1y';
  const benchmarkSymbol = MARKET_LIVE_BENCHMARK_SYMBOLS[selectedMarket] || null;

  const breadthBootstrap = useQuery({
    ...breadthBootstrapQuery(queryClient, selectedMarket),
  });
  const liveQueriesEnabled = breadthBootstrap.isSuccess || breadthBootstrap.isError;
  const contributorDrilldown = useBreadthContributors({
    market: selectedMarket,
    indexQueryKey: liveQueriesEnabled
      ? ['breadthContributors', 'index', selectedMarket]
      : null,
    loadIndex: liveQueriesEnabled
      ? () => getBreadthContributorIndex(selectedMarket)
      : null,
    loadDate: (date) => getBreadthContributors(selectedMarket, date),
  });

  const {
    data: currentBreadth,
    isLoading: isLoadingCurrent,
    error: errorCurrent,
  } = useQuery({
    queryKey: ['breadth', 'current', selectedMarket],
    queryFn: () => getCurrentBreadth(selectedMarket),
    enabled: liveQueriesEnabled,
    refetchInterval: 60_000,
    staleTime: 60_000,
  });
  const { data: historicalBreadth } = useQuery({
    queryKey: ['breadth', 'historical', selectedMarket, startDate, endDate],
    queryFn: () => getHistoricalBreadth(startDate, endDate, 365, selectedMarket),
    enabled: liveQueriesEnabled,
    staleTime: 60_000,
  });
  useQuery({
    queryKey: ['breadth', 'summary', selectedMarket],
    queryFn: () => getBreadthSummary(selectedMarket),
    enabled: liveQueriesEnabled,
    staleTime: 60_000,
  });
  const {
    data: chartBreadthData,
    isLoading: isLoadingChartBreadth,
    error: errorChartBreadth,
  } = useQuery({
    queryKey: [
      'breadth',
      'chart',
      selectedMarket,
      chartDateRange.startDate,
      chartDateRange.endDate,
    ],
    queryFn: () => getHistoricalBreadth(
      chartDateRange.startDate,
      chartDateRange.endDate,
      730,
      selectedMarket,
    ),
    enabled: liveQueriesEnabled,
    staleTime: 60_000,
  });
  const { data: benchmarkData } = useQuery({
    queryKey: [
      'benchmark',
      'history',
      selectedMarket,
      benchmarkSymbol,
      benchmarkPeriod,
    ],
    queryFn: () => getPriceHistory(benchmarkSymbol, benchmarkPeriod),
    enabled: liveQueriesEnabled && Boolean(benchmarkSymbol),
    staleTime: 60_000,
  });
  const contributorError = contributorDrilldown.dialogQuery.error;
  const contributorErrorStatus = contributorError?.response?.status;
  const contributorErrorDetail = contributorError?.response?.data?.detail;
  const contributorInconsistent = contributorErrorStatus === 409
    ? (typeof contributorErrorDetail === 'string'
      ? contributorErrorDetail
      : 'The contributor snapshot is inconsistent with breadth history.')
    : contributorDrilldown.viewState.inconsistent;
  if (!runtimeReady || isLoadingCurrent) {
    return (
      <Container maxWidth="xl" sx={{ mt: 4, mb: 4 }}>
        <Box display="flex" justifyContent="center" alignItems="center" minHeight="400px">
          <CircularProgress />
        </Box>
      </Container>
    );
  }
  if (errorCurrent) {
    return (
      <Container maxWidth="xl" sx={{ mt: 2, mb: 2 }}>
        <Alert severity="error">
          Error loading {selectedMarket} breadth data: {errorCurrent.message}
        </Alert>
      </Container>
    );
  }

  return (
    <Container maxWidth="xl" sx={{ mt: 2, mb: 2 }}>
      <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
        <Box
          sx={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            mb: 1.25,
          }}
        >
          <Box>
            <Typography variant="h6" sx={{ fontWeight: 700 }}>
              Latest Breadth Data
            </Typography>
            <Typography variant="caption" color="text.secondary">
              One shared calculation layer · revision {currentBreadth?.calculation_revision ?? '—'}
            </Typography>
          </Box>
          {currentBreadth?.date && (
            <Chip label={format(parseISO(currentBreadth.date), 'MMM dd, yyyy')} size="small" />
          )}
        </Box>
        <BreadthContextStrip row={currentBreadth} />
      </Paper>

      <Box sx={{ height: { xs: 390, md: 460 }, mb: 2 }}>
        <BreadthChart
          breadthData={chartBreadthData}
          spyData={benchmarkData || []}
          benchmarkLabel={benchmarkSymbol || 'Benchmark'}
          isLoading={isLoadingChartBreadth}
          error={errorChartBreadth}
          timeRange={chartTimeRange}
          onTimeRangeChange={setChartTimeRange}
          fillContainer
        />
      </Box>

      {historicalBreadth?.length > 0 && (
        <Paper variant="outlined">
          <Box sx={{ p: 1.5, borderBottom: 1, borderColor: 'divider' }}>
            <Typography sx={{ fontSize: 14, fontWeight: 700 }}>
              Recent History
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Primary tracks daily movers and ratios; secondary tracks trend windows; context adds T2108, ATR extension, and universe size.
            </Typography>
          </Box>
          <BreadthHistoryTable
            rows={historicalBreadth}
            contributorDates={contributorDrilldown.availableDates}
            onContributorCellClick={contributorDrilldown.open}
          />
        </Paper>
      )}

      <BreadthContributorDialog
        open={Boolean(contributorDrilldown.selected)}
        metric={contributorDrilldown.selected?.metric}
        row={contributorDrilldown.selected?.row}
        view={contributorDrilldown.viewState.view}
        isLoading={contributorDrilldown.dialogQuery.isLoading}
        error={[404, 409].includes(contributorErrorStatus)
          ? null
          : contributorError}
        unavailable={contributorErrorStatus === 404}
        inconsistent={contributorInconsistent}
        onRetry={() => contributorDrilldown.dialogQuery.refetch()}
        onClose={contributorDrilldown.close}
      />

      {currentBreadth && (
        <Typography
          component="div"
          variant="caption"
          color="text.secondary"
          sx={{ mt: 2, textAlign: 'center' }}
        >
          Broad universe: {currentBreadth.broad_universe_count
            ?? currentBreadth.total_stocks_scanned
            ?? '—'}
          {' · '}
          Calculation time: {currentBreadth.calculation_duration_seconds?.toFixed?.(2) ?? '—'}s
        </Typography>
      )}
    </Container>
  );
}

export default BreadthPage;
