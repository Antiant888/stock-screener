// Sparkline geometry, reproducing what recharts 2 drew for these charts so
// the plain-SVG sparklines look the same without a chart library.

export const scaleLinear = ([d0, d1], [r0, r1]) => {
  const span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
};

// Where recharts' Area fill and Bar columns start: the domain's floor, or
// zero when the domain spans it (its ceiling if the domain is all negative).
export const baseValue = ([d0, d1]) => (d0 >= 0 ? d0 : d1 <= 0 ? d1 : 0);

const fmt = (n) => Number(n.toFixed(3));
const sign = (x) => (x < 0 ? -1 : 1);

// d3-shape's curveMonotoneX (Steffen 1990), which recharts uses for
// type="monotone": tangent at an interior point from its two neighbours.
function interiorTangent([x0, y0], [x1, y1], [x2, y2]) {
  const h0 = x1 - x0;
  const h1 = x2 - x1;
  const s0 = (y1 - y0) / (h0 || (h1 < 0 && -0));
  const s1 = (y2 - y1) / (h1 || (h0 < 0 && -0));
  const p = (s0 * h1 + s1 * h0) / (h0 + h1);
  return (sign(s0) + sign(s1)) * Math.min(Math.abs(s0), Math.abs(s1), 0.5 * Math.abs(p)) || 0;
}

function endTangent([x0, y0], [x1, y1], t) {
  const h = x1 - x0;
  return h ? (3 * (y1 - y0) / h - t) / 2 : t;
}

export function monotoneXPath(points) {
  if (points.length === 0) return '';
  const [first] = points;
  if (points.length === 1) return `M${fmt(first[0])},${fmt(first[1])}`;
  if (points.length === 2) {
    const [a, b] = points;
    return `M${fmt(a[0])},${fmt(a[1])}L${fmt(b[0])},${fmt(b[1])}`;
  }
  const tangents = points.map((point, i) => (
    i === 0 || i === points.length - 1 ? null : interiorTangent(points[i - 1], point, points[i + 1])
  ));
  tangents[0] = endTangent(points[0], points[1], tangents[1]);
  const last = points.length - 1;
  tangents[last] = endTangent(points[last - 1], points[last], tangents[last - 1]);

  let d = `M${fmt(first[0])},${fmt(first[1])}`;
  for (let i = 1; i < points.length; i += 1) {
    const [x0, y0] = points[i - 1];
    const [x1, y1] = points[i];
    const dx = (x1 - x0) / 3;
    d += `C${fmt(x0 + dx)},${fmt(y0 + dx * tangents[i - 1])},${fmt(x1 - dx)},${fmt(y1 - dx * tangents[i])},${fmt(x1)},${fmt(y1)}`;
  }
  return d;
}

// One bar per value in equal bands, as recharts' BarChart with the default
// 10% category gap and a maxBarSize: widths over 1px are truncated to whole
// pixels, capped, then re-centred in the band. Bars rise from the domain's
// floor (or zero, if the domain spans it).
export function barRects(values, { width, height, margin, domain, maxBarSize }) {
  const y = scaleLinear(domain, [height - margin, margin]);
  const base = y(baseValue(domain));
  const band = width / values.length;
  const gap = band * 0.1;
  let slot = band - 2 * gap;
  if (slot > 1) slot = Math.trunc(slot);
  const barWidth = Math.min(slot, maxBarSize);
  const offset = gap + (slot - barWidth) / 2;
  return values.map((value, i) => {
    const top = y(value);
    return { x: band * i + offset, y: Math.min(top, base), width: barWidth, height: Math.abs(base - top) };
  });
}

export function roundedTopRectPath({ x, y, width, height }, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  return (
    `M${x},${y + r}A ${r},${r},0,0,1,${x + r},${y}L ${x + width - r},${y}`
    + `A ${r},${r},0,0,1,${x + width},${y + r}L ${x + width},${y + height}L ${x},${y + height}Z`
  );
}
