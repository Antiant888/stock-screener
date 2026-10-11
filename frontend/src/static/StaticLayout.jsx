import { useEffect, useState } from 'react';
import {
  Alert,
  AppBar,
  Box,
  Button,
  Chip,
  Container,
  FormControl,
  MenuItem,
  Select,
  Toolbar,
} from '@mui/material';
import CandlestickChartIcon from '@mui/icons-material/CandlestickChart';
import {
  BrandMark,
  ColorModeButton,
  ColorModeListItem,
  DesktopNav,
  MobileBottomNav,
} from '../components/Layout/AppNav';
import { CORE_NAV_ITEMS, mobileNavOffset, useIsMobileShell } from '../components/Layout/shellNav';
import { useStaticMarket } from './StaticMarketContext';
import {
  getStaticSupportedMarkets,
  getStaticUnavailableMarkets,
  resolveStaticMarketEntry,
  useStaticManifest,
} from './dataClient';
import { useStaticGenerationLifecycle } from './useStaticGenerationLifecycle';
import { marketFlag } from '../utils/marketFlags';
import { isStaticOptionsAvailable } from '../features/options/optionsAvailability';

// Late once the publish says so, or once its deadline passes with nothing new
// published: a stalled pipeline must not leave a market "current" (#503).
const publicationStatus = (publication, now) => {
  if (!publication) return null;
  const session = publication.session_date;
  if (publication.state === 'unknown' || !session) return { label: 'Status unknown', color: 'default' };
  const deadline = publication.late_after ? Date.parse(publication.late_after) : NaN;
  if (publication.state === 'stale') return { label: `Late · ${session}`, color: 'warning' };
  // No deadline means nothing could turn this late: unknown, never Current forever.
  if (!Number.isFinite(deadline)) return { label: 'Status unknown', color: 'default' };
  if (now > deadline) return { label: `Late · ${session}`, color: 'warning' };
  return { label: `Current · ${session}`, color: 'success' };
};

const useMinuteClock = () => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
};

function StaticLayout({ children }) {
  const isMobile = useIsMobileShell();
  const manifestQuery = useStaticManifest();
  const { expired } = useStaticGenerationLifecycle();
  const supportedMarkets = getStaticSupportedMarkets(manifestQuery.data);
  const { selectedMarket, setSelectedMarket } = useStaticMarket();
  const marketEntry = resolveStaticMarketEntry(manifestQuery.data, selectedMarket);
  const selectedUnavailable = getStaticUnavailableMarkets(manifestQuery.data).includes(selectedMarket);
  const now = useMinuteClock();
  const status = selectedUnavailable ? null : publicationStatus(marketEntry.publication, now);
  const navItems = isStaticOptionsAvailable(marketEntry)
    ? [...CORE_NAV_ITEMS, { path: '/options', label: 'Options', matchPrefix: true, icon: <CandlestickChartIcon /> }]
    : CORE_NAV_ITEMS;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar position="static">
        <Toolbar variant="dense" sx={{ minHeight: 52, gap: { xs: 1, md: 1.5 } }}>
          <BrandMark label={isMobile ? null : 'Stock Scanner Daily'} />
          {!isMobile && <Chip label="Read-only" size="small" variant="outlined" />}
          <FormControl size="small" sx={{ minWidth: { xs: 0, md: 140 } }}>
            <Select
              value={selectedUnavailable ? selectedMarket : marketEntry.market}
              onChange={(event) => setSelectedMarket(event.target.value)}
              displayEmpty
              sx={{ height: 32, fontSize: '13px', fontWeight: 600, borderRadius: 2 }}
              inputProps={{ 'aria-label': 'Static market selector' }}
            >
              {supportedMarkets.map((market) => {
                const label = manifestQuery.data?.markets?.[market]?.display_name || market;
                const flag = marketFlag(market);
                return (
                  <MenuItem key={market} value={market}>
                    {flag ? `${flag}  ${label}` : label}
                  </MenuItem>
                );
              })}
              {getStaticUnavailableMarkets(manifestQuery.data).map((market) => {
                const flag = marketFlag(market);
                const label = `${market} — unavailable`;
                return (
                  <MenuItem key={market} value={market} disabled>
                    {flag ? `${flag}  ${label}` : label}
                  </MenuItem>
                );
              })}
            </Select>
          </FormControl>
          {status && (
            <Chip
              label={status.label}
              color={status.color}
              variant="outlined"
              size="small"
              role="status"
              aria-label={`${marketEntry.market} data status: ${status.label}`}
            />
          )}
          {!isMobile && (
            <>
              <Box sx={{ flexGrow: 1 }} />
              <DesktopNav items={navItems} />
              <ColorModeButton />
            </>
          )}
        </Toolbar>
      </AppBar>

      <Container maxWidth="xl" sx={{ mt: 1.5, mb: 1.5, flex: 1, pb: { xs: mobileNavOffset(8), md: 0 } }}>
        {expired && (
          <Alert
            severity="info"
            sx={{ mb: 1.5 }}
            action={(
              <Button color="inherit" size="small" onClick={() => window.location.reload()}>
                Reload
              </Button>
            )}
          >
            Some data could not be loaded, most likely because newer data has been published. Reload to get the latest.
          </Alert>
        )}
        {selectedUnavailable ? (
          <Alert
            severity="warning"
            action={(
              <Button color="inherit" size="small" onClick={() => manifestQuery.refetch()}>
                Retry
              </Button>
            )}
          >
            {selectedMarket} data is not available in this publish. Retry, or choose another market above.
          </Alert>
        ) : children}
      </Container>

      {isMobile && <MobileBottomNav items={navItems} extras={(close) => <ColorModeListItem onDone={close} />} />}
    </Box>
  );
}

export default StaticLayout;
