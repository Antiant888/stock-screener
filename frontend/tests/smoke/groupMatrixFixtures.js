export const tiers = [
  { id: 'large_mega', label: 'Large/Mega', min_usd: 1e10, max_usd: null },
  { id: 'mid', label: 'Mid', min_usd: 2e9, max_usd: 1e10 },
  { id: 'small', label: 'Small', min_usd: 3e8, max_usd: 2e9 },
  { id: 'micro', label: 'Micro', min_usd: 5e7, max_usd: 3e8 },
  { id: 'nano', label: 'Nano', min_usd: 0, max_usd: 5e7 },
  { id: 'unknown', label: 'Unknown cap', min_usd: null, max_usd: null },
];
export function matrixFixture(market = 'US', count = 120) {
  const stocks = Array.from({length: count}, (_, i) => ({
    symbol: `${market}${String(i).padStart(5, '0')}`, company_name: `Example company ${i}`,
    sector: ['Technology', 'Healthcare', 'Industrials', 'Finance'][Math.floor(i/50)%4],
    ibd_industry_group: `IBD Group ${String(Math.floor(i/50)).padStart(3, '0')}`,
    cap_tier: tiers[i%6].id, market_cap_usd: [1e11, 3e9, 5e8, 1e8, 2e7, null][i%6],
    classification_source: market === 'US' ? 'csv' : 'crosswalk', classification_confidence: market === 'US' ? null : 0.9,
    classification_updated_at: '2026-09-10T00:00:00Z', fundamentals_updated_at: '2026-09-11T00:00:00Z',
    price_change_1d: i%11 === 0 ? null : ((i%13)-6)/2, rs_rating: (i*7)%101,
    price_change_1w: i%11 === 0 ? null : ((i%13)-6),
    price_change_1m: i%11 === 0 ? null : ((i%13)-6)*2,
  }));
  return { schema_version:'group-matrix-v1', available:true, reason:null, market, feature_run_id:1,
    as_of_date:'2026-09-11', generated_at:'2026-09-13T00:00:00Z', metadata_read_at:'2026-09-13T00:00:00Z',
    metadata_basis:'latest_stored', taxonomy:'ibd', rs_formula_version:'legacy-linear-v1', market_rs_run_id:null, rs_universe_size:null,
    tiers, stocks, coverage: {stock_count:count, universe_count:count, ibd_mapped_count:count, missing_feature_count:0,
      unknown_sector_count:0, unknown_cap_count:stocks.filter(s=>s.market_cap_usd===null).length,
      missing_daily_change_count:stocks.filter(s=>s.price_change_1d===null).length,
      missing_weekly_change_count:stocks.filter(s=>s.price_change_1w===null).length,
      missing_monthly_change_count:stocks.filter(s=>s.price_change_1m===null).length, missing_rs_count:0} };
}
export async function installMatrixFixtures(page, { count = 120, mode = 'live' } = {}) {
  const requests = [];
  const json = (route, value, status = 200) => route.fulfill({ status, contentType:'application/json', body:JSON.stringify(value) });
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url()); requests.push(url.pathname);
    if (mode === 'static') return json(route, {detail:'Static mode must not call live API'}, 500);
    if (url.pathname.endsWith('/app-capabilities')) return json(route, {
      features:{tasks:false, themes:false, chatbot:false}, auth:{required:false,authenticated:true},
      bootstrap_required:false, primary_market:'US', enabled_markets:['US','HK'], supported_markets:['US','HK'],
      api_base_path:'/api',
      market_catalog:{version:'test', markets:[['US','United States'],['HK','Hong Kong']].map(([code,label])=>({code,label,capabilities:{group_rankings:true,rrg_scopes:[]}}))},
    });
    if (url.pathname.endsWith('/runtime/activity')) return json(route, {bootstrap:{state:'ready',app_ready:true,primary_market:'US',enabled_markets:['US','HK']}, summary:{status:'idle',active_markets:[]}, markets:[]});
    if (url.pathname.endsWith('/groups/matrix')) return json(route, matrixFixture(url.searchParams.get('market') || 'US', count));
    if (url.pathname.includes('/groups/')) return json(route, {detail:'Fixture rankings unavailable'}, 503);
    return json(route, {});
  });
  await page.route('**/static-data/**', async route => {
    const path = new URL(route.request().url()).pathname.split('/static-data/')[1]; requests.push(path);
    if (path === 'manifest.json') return json(route, { schema_version:'v1', default_market:'US', supported_markets:['US','HK'], markets:
      Object.fromEntries(['US','HK'].map(market=>[market,{market,display_name:market, as_of_date:'2026-09-11', features:{groups:true},
        pages:{groups:{path:`markets/${market.toLowerCase()}/groups.json`}}, assets:{groups_matrix:{path:`markets/${market.toLowerCase()}/groups_matrix.json`}}}]))});
    if (path.endsWith('/groups_matrix.json')) return json(route, matrixFixture(path.includes('/hk/')?'HK':'US', count));
    return json(route, {available:false, message:'Fixture rankings unavailable'});
  });
  return requests;
}
