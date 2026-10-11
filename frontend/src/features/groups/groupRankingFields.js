export const GROUP_RS_FIELDS = Object.freeze([
  Object.freeze({
    field: 'avg_rs_rating',
    label: 'RS',
    staticLabel: 'Avg RS',
    description:
      'Blends relative strength over up to 12 months, weighted toward the most recent quarter, so it reflects lasting leadership and lags sharp recent moves. For recent momentum, sort by 1M or 3M RS.',
    align: 'right',
    staticAlign: 'center',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'avg_rs_rating_1d',
    label: '1D RS',
    staticLabel: '1D RS',
    align: 'right',
    staticAlign: 'center',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'avg_rs_rating_1w',
    label: '1W RS',
    staticLabel: '1W RS',
    align: 'right',
    staticAlign: 'center',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'avg_rs_rating_1m',
    label: '1M RS',
    staticLabel: '1M RS',
    align: 'right',
    staticAlign: 'center',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'avg_rs_rating_3m',
    label: '3M RS',
    staticLabel: '3M RS',
    align: 'right',
    staticAlign: 'center',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'avg_rs_rating_6m',
    label: '6M RS',
    staticLabel: '6M RS',
    align: 'right',
    staticAlign: 'center',
    kind: 'rs',
  }),
]);

export const GROUP_RANK_CHANGE_FIELDS = Object.freeze([
  Object.freeze({
    field: 'rank_change_1w',
    label: '1W',
    staticLabel: '1W',
    align: 'right',
    kind: 'rankChange',
  }),
  Object.freeze({
    field: 'rank_change_1m',
    label: '1M Δ',
    staticLabel: '1M',
    align: 'right',
    kind: 'rankChange',
  }),
  Object.freeze({
    field: 'rank_change_3m',
    label: '3M Δ',
    staticLabel: '3M',
    align: 'right',
    kind: 'rankChange',
  }),
  Object.freeze({
    field: 'rank_change_6m',
    label: '6M',
    staticLabel: '6M',
    align: 'right',
    kind: 'rankChange',
  }),
]);

const identityColumns = Object.freeze([
  Object.freeze({
    field: 'rank',
    label: 'Rank',
    staticLabel: 'Rank',
    align: 'left',
    staticAlign: 'center',
    kind: 'rank',
  }),
  Object.freeze({
    field: 'industry_group',
    label: 'Industry Group',
    staticLabel: 'Group',
    align: 'left',
    kind: 'group',
  }),
]);

const stockCountColumn = Object.freeze({
  field: 'num_stocks',
  label: '#',
  staticLabel: 'Stocks',
  align: 'right',
  staticAlign: 'center',
  kind: 'integer',
});

const liveOnlyColumns = Object.freeze([
  Object.freeze({
    field: 'median_rs_rating',
    label: 'Med RS',
    align: 'right',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'weighted_avg_rs_rating',
    label: 'Wtd RS',
    align: 'right',
    kind: 'rs',
  }),
  Object.freeze({
    field: 'rs_std_dev',
    label: 'Disp',
    align: 'right',
    kind: 'rating',
  }),
  stockCountColumn,
  Object.freeze({
    field: 'pct_rs_above_80',
    label: '80+%',
    align: 'right',
    kind: 'pctAbove80',
  }),
]);

const topStockColumn = Object.freeze({
  field: 'top_symbol',
  label: 'Top',
  staticLabel: 'Top Stock',
  align: 'right',
  staticAlign: 'left',
  kind: 'topStock',
});

export const LIVE_GROUP_RANKING_COLUMNS = Object.freeze([
  ...identityColumns,
  ...GROUP_RS_FIELDS,
  ...liveOnlyColumns,
  topStockColumn,
  ...GROUP_RANK_CHANGE_FIELDS,
]);

export const STATIC_GROUP_RANKING_COLUMNS = Object.freeze([
  ...identityColumns,
  ...GROUP_RS_FIELDS,
  stockCountColumn,
  ...GROUP_RANK_CHANGE_FIELDS,
  topStockColumn,
]);

export const isGroupRankChangeField = (field) => (
  GROUP_RANK_CHANGE_FIELDS.some(
    ({ field: rankChangeField }) => rankChangeField === field,
  )
);

export const deriveHistoricalRank = (row, change) => {
  if (change === null || change === undefined || row.rank === null || row.rank === undefined) {
    return null;
  }
  return row.rank + change;
};

export const derivePctRsAbove80 = (row) => {
  if (row.pct_rs_above_80 !== null && row.pct_rs_above_80 !== undefined) {
    return row.pct_rs_above_80;
  }
  if (!row.num_stocks) return null;
  const count = row.num_stocks_rs_above_80 ?? 0;
  return (count / row.num_stocks) * 100;
};

export const getLiveGroupRankingSortValue = (
  row,
  column,
  { showHistoricalRanks = false } = {},
) => {
  if (showHistoricalRanks && isGroupRankChangeField(column)) {
    return deriveHistoricalRank(row, row[column]);
  }

  if (column === 'pct_rs_above_80') {
    return derivePctRsAbove80(row);
  }

  return row[column];
};

export const formatGroupRs = (value) => (
  Number.isFinite(value) ? value.toFixed(1) : '-'
);
