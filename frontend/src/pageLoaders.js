// One loader per page, shared by the router's lazy() and the prefetchers, so
// a page whose code was fetched early is the module the router renders.
export const loadMarketScanPage = () => import('./pages/MarketScanPage');
export const loadScanPage = () => import('./pages/ScanPage');
export const loadStockDetails = () => import('./components/Stock/StockDetails');
export const loadBreadthPage = () => import('./pages/BreadthPage');
export const loadGroupRankingsPage = () => import('./pages/GroupRankingsPage');
export const loadValidationPage = () => import('./pages/ValidationPage');
export const loadThemesPage = () => import('./pages/ThemesPage');
export const loadChatbotPage = () => import('./pages/ChatbotPage');
export const loadOperationsPage = () => import('./pages/OperationsPage');
export const loadOptionsPage = () => import('./pages/OptionsPage');
export const loadOptionsSymbolPage = () => import('./pages/OptionsSymbolPage');

const PATH_LOADERS = {
  '/': loadMarketScanPage,
  '/scan': loadScanPage,
  '/breadth': loadBreadthPage,
  '/groups': loadGroupRankingsPage,
  '/validation': loadValidationPage,
  '/themes': loadThemesPage,
  '/chatbot': loadChatbotPage,
  '/operations': loadOperationsPage,
  '/options': loadOptionsPage,
};

export function loaderForPath(pathname) {
  if (pathname.startsWith('/stocks/')) return loadStockDetails;
  if (pathname.startsWith('/options/')) return loadOptionsSymbolPage;
  return PATH_LOADERS[pathname] ?? null;
}
