/* global d3, SFC_DATA */
const DATA = window.SFC_DATA;
const fmt = v => {
  const a = Math.abs(v);
  if (a >= 1000) return `$${(v / 1000).toFixed(2)}T`;
  if (a >= 1) return `$${v.toFixed(1)}B`;
  return `$${(v * 1000).toFixed(0)}M`;
};
const fmtDays = d => d >= 365 ? `${(d / 365).toFixed(1)} yr` : d >= 1 ? `${d.toFixed(1)} d` : `${(d * 24).toFixed(1)} h`;
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const SECTOR_COLORS = {
  'Federal government': '#d62728',
  'State & local government': '#ff7f0e',
  'Monetary & credit': '#9467bd',
  'Household': '#2ca02c',
  'Financial & real estate': '#17becf',
  'Industrial': '#8c564b',
  'Trade & transport': '#bcbd22',
  'Services': '#1f77b4',
  'Investment': '#e377c2',
  'Pensions': '#8c6d31',
  'Rest of world': '#7f7f7f',
};
const color = s => SECTOR_COLORS[s] || '#444';

// ---- graph data -------------------------------------------------------------------------
const nodes = DATA.nodes.map(n => ({ ...n, V: 0 }));
const byId = new Map(nodes.map(n => [n.id, n]));
const links = DATA.links.map(l => ({ source: byId.get(l.s), target: byId.get(l.t), v: l.v, types: l.types }));
const outL = new Map(nodes.map(n => [n.id, []]));
const inL = new Map(nodes.map(n => [n.id, []]));
links.forEach(l => { outL.get(l.source.id).push(l); inL.get(l.target.id).push(l); });
nodes.forEach(n => {
  n.out = d3.sum(outL.get(n.id), l => l.v);
  n.in = d3.sum(inL.get(n.id), l => l.v);
  n.through = Math.max(n.in, n.out);
});

const state = { selected: null, through: [], trace: null };

// ---- potential (least-squares "HodgeRank" on the transaction graph) ---------------------
// Minimise sum_e w_e (V_s - V_t - 1)^2  =>  L V = b,  b_i = sum_out w - sum_in w
function computePotential() {
  const weighting = document.getElementById('weighting').value;
  const minEdge = +document.getElementById('min-edge').value || 0;
  const n = nodes.length;
  const idx = new Map(nodes.map((d, i) => [d.id, i]));
  const E = links.filter(l => l.v >= minEdge).map(l => {
    const w = weighting === 'unit' ? 1 : weighting === 'log' ? Math.log10(1 + l.v * 1000) : l.v;
    return [idx.get(l.source.id), idx.get(l.target.id), w];
  });
  const diag = new Float64Array(n), b = new Float64Array(n), deg = new Float64Array(n);
  E.forEach(([s, t, w]) => { diag[s] += w; diag[t] += w; b[s] += w; b[t] -= w; deg[s]++; deg[t]++; });
  const eps = 1e-6 * (d3.max(diag) || 1);
  const matvec = (x, y) => {
    for (let i = 0; i < n; i++) y[i] = (diag[i] + eps) * x[i];
    E.forEach(([s, t, w]) => { y[s] -= w * x[t]; y[t] -= w * x[s]; });
  };
  // conjugate gradient
  const x = new Float64Array(n), r = Float64Array.from(b), p = Float64Array.from(b), Ap = new Float64Array(n);
  let rr = r.reduce((a, v) => a + v * v, 0);
  for (let it = 0; it < 5000 && rr > 1e-12; it++) {
    matvec(p, Ap);
    const alpha = rr / p.reduce((a, v, i) => a + v * Ap[i], 0);
    for (let i = 0; i < n; i++) { x[i] += alpha * p[i]; r[i] -= alpha * Ap[i]; }
    const rr2 = r.reduce((a, v) => a + v * v, 0);
    const beta = rr2 / rr;
    for (let i = 0; i < n; i++) p[i] = r[i] + beta * p[i];
    rr = rr2;
  }
  const mean = d3.mean(x);
  nodes.forEach((d, i) => { d.V = x[i] - mean; d.degree = deg[i]; });
}

// ---- layout -----------------------------------------------------------------------------
const svg = d3.select('#graph');
const gRoot = svg.append('g');
const gBg = gRoot.append('g');
const gHl = gRoot.append('g');
const gTrace = gRoot.append('g');
const gNodes = gRoot.append('g');
const gAxis = gRoot.append('g');
svg.append('defs').append('marker').attr('id', 'arrow').attr('viewBox', '0 0 10 10').attr('refX', 9).attr('refY', 5)
  .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
  .append('path').attr('d', 'M0,0L10,5L0,10z').attr('fill', '#d00');
svg.call(d3.zoom().scaleExtent([0.3, 8]).on('zoom', e => gRoot.attr('transform', e.transform)));

const rScale = d3.scaleSqrt().domain([0, d3.max(nodes, d => d.through)]).range([2.5, 16]);

function layout() {
  const W = svg.node().clientWidth, H = svg.node().clientHeight;
  const nCols = Math.max(2, +document.getElementById('ncols').value || 20);
  const sorted = [...nodes].sort((a, b) => b.V - a.V);
  const cols = d3.range(nCols).map(() => []);
  sorted.forEach((d, i) => cols[Math.min(nCols - 1, Math.floor(i * nCols / sorted.length))].push(d));
  const maxRows = d3.max(cols, c => c.length);
  const mx = 60, myTop = 30, myBot = 20;
  const colW = (W - 2 * mx) / (nCols - 1);
  const rowH = (H - myTop - myBot) / Math.max(1, maxRows - 1);
  cols.forEach((c, ci) => {
    c.sort((a, b) => b.degree - a.degree || b.through - a.through);
    c.forEach((d, ri) => {
      d.col = ci;
      d.x = mx + ci * colW + (ri % 2 ? colW * 0.18 : 0);
      d.y = myTop + ri * rowH;
    });
  });
  gAxis.selectAll('*').remove();
  cols.forEach((c, ci) => {
    if (!c.length) return;
    gAxis.append('text').attr('class', 'axis-label').attr('x', mx + ci * colW).attr('y', 14)
      .attr('text-anchor', 'middle').text(d3.mean(c, d => d.V).toFixed(1));
  });
  gAxis.append('text').attr('class', 'axis-label').attr('x', 4).attr('y', 14).text('V:');
  gAxis.append('text').attr('class', 'axis-label').attr('x', W - 4).attr('y', H - 4).attr('text-anchor', 'end')
    .text('← high potential (creation)          rows: most connected at top          low potential (destruction) →');
}

function linkPath(l) {
  const { x: x1, y: y1 } = l.source, { x: x2, y: y2 } = l.target;
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
  if (x2 >= x1) return `M${x1},${y1}C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
  // against potential: loop out to the right then come back
  return `M${x1},${y1}C${x1 + dx},${y1 - 60} ${x2 - dx},${y2 - 60} ${x2},${y2}`;
}

function drawNodes() {
  const labelSet = new Set([...nodes].filter(d => d.kind === 'node').sort((a, b) => b.through - a.through).slice(0, 10).map(d => d.id));
  nodes.filter(d => d.kind !== 'node').forEach(d => labelSet.add(d.id));
  if (state.selected) labelSet.add(state.selected);
  const sel = gNodes.selectAll('g.node').data(nodes, d => d.id).join(enter => {
    const g = enter.append('g').attr('class', 'node');
    g.append('circle');
    g.append('text').attr('dx', 6).attr('dy', 3);
    return g;
  });
  sel.attr('transform', d => `translate(${d.x},${d.y})`)
    .on('click', (e, d) => selectNode(d.id))
    .on('mousemove', (e, d) => showTip(e, nodeTip(d)))
    .on('mouseleave', hideTip);
  sel.select('circle').attr('r', d => rScale(d.through)).attr('fill', d => color(d.sector))
    .attr('stroke', d => d.kind === 'source' ? '#000' : d.kind === 'sink' ? '#555' : '#fff')
    .attr('stroke-dasharray', d => d.kind === 'sink' ? '2,2' : null);
  sel.select('text').text(d => labelSet.has(d.id) ? short(d.name) : '');
}
const short = s => s.length > 34 ? s.slice(0, 32) + '…' : s;

function drawBackground() {
  gBg.selectAll('*').remove();
  if (!document.getElementById('bg-edges').checked) return;
  const top = [...links].sort((a, b) => b.v - a.v).slice(0, 400);
  const w = d3.scaleSqrt().domain([0, top[0].v]).range([0.5, 6]);
  gBg.selectAll('path').data(top).join('path').attr('class', 'link-bg').attr('d', linkPath).attr('stroke-width', l => w(l.v));
}

// ---- selection / path highlighting ---------------------------------------------------------
function propagate(startId, depth, dir) {
  const res = new Map();
  const start = byId.get(startId);
  const total = dir === 'down' ? start.out : start.in;
  const cutoff = total * 0.002;
  let frontier = new Map([[startId, total]]);
  for (let d = 0; d < depth; d++) {
    const next = new Map();
    frontier.forEach((amt, id) => {
      const n = byId.get(id);
      const list = dir === 'down' ? outL.get(id) : inL.get(id);
      const denom = dir === 'down' ? n.out : n.in;
      if (!denom) return;
      list.forEach(l => {
        const f = amt * l.v / denom;
        if (f < cutoff) return;
        res.set(l, (res.get(l) || 0) + f);
        const other = dir === 'down' ? l.target : l.source;
        if (other.kind === 'node' && other.id !== startId) next.set(other.id, (next.get(other.id) || 0) + f);
      });
    });
    frontier = next;
  }
  return res;
}

function selectNode(id) {
  state.selected = id;
  const depth = +document.getElementById('depth').value;
  const up = propagate(id, depth, 'up');
  const down = propagate(id, depth, 'down');
  const all = [...[...up].map(([l, f]) => ({ l, f, dir: 'up' })), ...[...down].map(([l, f]) => ({ l, f, dir: 'down' }))];
  const maxF = d3.max(all, d => d.f) || 1;
  const w = d3.scaleSqrt().domain([0, maxF]).range([0.6, 18]);
  gHl.selectAll('path').data(all.sort((a, b) => a.f - b.f), d => d.dir + d.l.source.id + '>' + d.l.target.id)
    .join('path').attr('class', 'link-hl').attr('d', d => linkPath(d.l))
    .attr('stroke', d => d.dir === 'up' ? '#1f77b4' : '#ff7f0e').attr('stroke-width', d => w(d.f))
    .on('mousemove', (e, d) => showTip(e, linkTip(d.l, d.f, d.dir)))
    .on('mouseleave', hideTip);
  const involved = new Set([id]);
  all.forEach(d => { involved.add(d.l.source.id); involved.add(d.l.target.id); });
  gNodes.selectAll('g.node').classed('dim', d => !involved.has(d.id)).classed('selected', d => d.id === id);
  drawNodes();
  renderPanel(byId.get(id));
}

function clearSelection() {
  state.selected = null;
  gHl.selectAll('*').remove();
  gNodes.selectAll('g.node').classed('dim', false).classed('selected', false);
}

function renderPanel(n) {
  const ins = [...inL.get(n.id)].sort((a, b) => b.v - a.v);
  const outs = [...outL.get(n.id)].sort((a, b) => b.v - a.v);
  const typeSum = list => {
    const m = new Map();
    list.forEach(l => Object.entries(l.types).forEach(([k, v]) => m.set(k, (m.get(k) || 0) + v)));
    return [...m].sort((a, b) => b[1] - a[1]);
  };
  const rows = (list, key) => list.slice(0, 25).map(l => {
    const o = l[key];
    return `<tr><td><span class="swatch" style="background:${color(o.sector)}"></span>
      <span class="link" data-id="${esc(o.id)}">${esc(o.name)}</span></td><td class="num">${fmt(l.v)}</td></tr>`;
  }).join('') + (list.length > 25 ? `<tr><td class="muted">+${list.length - 25} more</td><td class="num">${fmt(d3.sum(list.slice(25), l => l.v))}</td></tr>` : '');
  const typesRows = list => list.slice(0, 8).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v)}</td></tr>`).join('');
  document.getElementById('panel').innerHTML = `
    <h3><span class="swatch" style="background:${color(n.sector)}"></span> ${esc(n.name)}</h3>
    <div class="muted">${esc(n.sector)} · ${n.kind}${n.codes.length ? ' · BEA codes: ' + esc(n.codes.slice(0, 12).join(', ')) + (n.codes.length > 12 ? '…' : '') : ''}</div>
    <table>
      <tr><td>Money in / yr</td><td class="num">${fmt(n.in)}</td></tr>
      <tr><td>Money out / yr</td><td class="num">${fmt(n.out)}</td></tr>
      ${n.output ? `<tr><td>Gross output (BEA)</td><td class="num">${fmt(n.output)}</td></tr>` : ''}
      <tr><td>Potential V</td><td class="num">${n.V.toFixed(2)}</td></tr>
      <tr><td>Counterparties (≥ min edge)</td><td class="num">${n.degree}</td></tr>
      <tr><td>Mean holding time</td><td class="num">${n.kind === 'node' ? fmtDays(n.dwell) : n.kind === 'source' ? '0 (created)' : 'terminal'}</td></tr>
    </table>
    <p><button id="add-through">Require trace to pass through this node</button></p>
    ${ins.length ? `<h4>Where its money comes from (by type)</h4><table>${typesRows(typeSum(ins))}</table>
    <h4>From (direct)</h4><table>${rows(ins, 'source')}</table>` : ''}
    ${outs.length ? `<h4>Where its money goes (by type)</h4><table>${typesRows(typeSum(outs))}</table>
    <h4>To (direct)</h4><table>${rows(outs, 'target')}</table>` : ''}`;
  document.querySelectorAll('#panel .link').forEach(el => el.addEventListener('click', () => selectNode(el.dataset.id)));
  document.getElementById('add-through').addEventListener('click', () => addThrough(n.id));
}

// ---- tooltips ---------------------------------------------------------------------------
const tip = document.getElementById('tooltip');
function showTip(e, html) {
  tip.innerHTML = html;
  tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 370) + 'px';
  tip.style.top = (e.clientY + 14) + 'px';
}
function hideTip() { tip.style.display = 'none'; }
const nodeTip = d => `<b>${esc(d.name)}</b><br>${esc(d.sector)} (${d.kind})<br>in ${fmt(d.in)} · out ${fmt(d.out)}<br>V = ${d.V.toFixed(2)} · ${d.degree} counterparties`;
function linkTip(l, f, dir) {
  const types = Object.entries(l.types).slice(0, 5).map(([k, v]) => `${esc(k)}: ${fmt(v)}`).join('<br>');
  const attributed = f !== undefined && Math.abs(f - l.v) > 1e-6
    ? `<br><i>${dir === 'up' ? 'Ends up at' : 'Originated at'} selected node: ${fmt(f)}</i>` : '';
  return `<b>${esc(l.source.name)}</b> → <b>${esc(l.target.name)}</b><br>Direct flow: ${fmt(l.v)}/yr${attributed}
    <br>ΔV = ${(l.target.V - l.source.V).toFixed(2)} (${l.target.V <= l.source.V ? 'downhill' : 'uphill'})<br>${types}`;
}

// ---- random dollar ----------------------------------------------------------------------
function weightedPick(list, w) {
  const total = d3.sum(list, w);
  let r = Math.random() * total;
  for (const x of list) { r -= w(x); if (r <= 0) return x; }
  return list[list.length - 1];
}
const MAX_STEPS = 4000;

function walkForward(startId) {
  const path = [startId];
  let cur = byId.get(startId);
  const hops = [];
  while (cur.kind !== 'sink' && path.length < MAX_STEPS) {
    const l = weightedPick(outL.get(cur.id), x => x.v);
    hops.push(l);
    cur = l.target;
    path.push(cur.id);
  }
  return hops;
}
function walkBackward(endId) {
  const hops = [];
  let cur = byId.get(endId);
  while (cur.kind !== 'source' && hops.length < MAX_STEPS) {
    const l = weightedPick(inL.get(cur.id), x => x.v);
    hops.unshift(l);
    cur = l.source;
  }
  return hops;
}

function sampleDollar() {
  const srcFilter = document.getElementById('src-filter').value;
  const sources = nodes.filter(n => n.kind === 'source' && (!srcFilter || n.id === srcFilter));
  const req = state.through;
  let hops = null, tries = 0;
  if (!req.length) {
    hops = walkForward(weightedPick(sources, n => n.out).id);
  } else {
    // exact conditional sampling on the anchor: backward walk ~ inflow shares, forward walk ~ outflow shares
    const anchor = req[0];
    for (; tries < 3000; tries++) {
      const back = byId.get(anchor).kind === 'source' ? [] : walkBackward(anchor);
      const origin = back.length ? back[0].source.id : anchor;
      if (srcFilter && origin !== srcFilter) continue;
      const fwd = byId.get(anchor).kind === 'sink' ? [] : walkForward(anchor);
      const h = [...back, ...fwd];
      const visited = new Set([origin, ...h.map(l => l.target.id)]);
      if (req.every(id => visited.has(id))) { hops = h; break; }
    }
  }
  if (!hops) return null;
  const steps = [];
  let t = 0;
  const first = hops.length ? hops[0].source : byId.get(req[0]);
  let cur = first;
  hops.forEach((l, i) => {
    const dwell = cur.kind === 'node' ? -cur.dwell * Math.log(1 - Math.random()) : 0;
    const type = weightedPick(Object.entries(l.types), ([, v]) => v)[0];
    steps.push({ node: cur, arrive: t, dwell, next: l.target, type, link: l, i });
    t += dwell;
    cur = l.target;
  });
  steps.push({ node: cur, arrive: t, dwell: 0, next: null, type: null, link: null, i: hops.length });
  return { steps, tries, lifespan: t };
}

function runTrace() {
  const tr = sampleDollar();
  if (!tr) {
    document.getElementById('trace-summary').innerHTML = '<span class="up">Could not find a dollar matching those constraints after 3000 tries — try fewer required nodes or a different creation source.</span>';
    return;
  }
  state.trace = tr;
  renderTrace();
}

function renderTrace() {
  const tr = state.trace;
  if (!tr) return;
  const { steps } = tr;
  const moves = steps.filter(s => s.next);
  const down = moves.filter(s => s.next.V <= s.node.V).length;
  const first = steps[0], last = steps[steps.length - 1];
  const sectorTime = d3.rollups(steps, v => d3.sum(v, s => s.dwell), s => s.node.sector).sort((a, b) => b[1] - a[1]);
  document.getElementById('trace-summary').innerHTML = `
    <b>Created</b> by <b>${esc(first.node.name)}</b> (${esc(first.type || '')}) ·
    <b>destroyed / exited</b> at <b>${esc(last.node.name)}</b> via ${esc(steps[steps.length - 2]?.type || '')} ·
    lifespan <b>${fmtDays(tr.lifespan)}</b> · ${moves.length} transactions ·
    <span class="down">${down} downhill</span> / <span class="up">${moves.length - down} uphill</span>
    ${tr.tries ? ` · (conditional sample, ${tr.tries + 1} tries)` : ''}<br>
    Time spent by sector: ${sectorTime.filter(d => d[1] > 0).map(([s, v]) => `<span class="chip"><span class="swatch" style="background:${color(s)}"></span>${esc(s)} ${fmtDays(v)}</span>`).join(' ')}`;
  drawTraceChart();
  drawTraceTable();
  drawTraceOnGraph();
}

function drawTraceChart() {
  const tr = state.trace;
  const chart = d3.select('#trace-chart');
  chart.selectAll('*').remove();
  if (!tr) return;
  const W = chart.node().clientWidth, H = chart.node().clientHeight;
  const m = { l: 50, r: 20, t: 16, b: 34 };
  const byStep = document.getElementById('trace-x').value === 'step';
  const segs = tr.steps.map(s => ({ ...s, x0: byStep ? s.i : s.arrive, x1: byStep ? s.i + 1 : s.arrive + s.dwell }));
  const xMax = byStep ? segs.length : Math.max(tr.lifespan, 1);
  const x = d3.scaleLinear().domain([0, xMax]).range([m.l, W - m.r]);
  const y = d3.scaleLinear().domain(d3.extent(nodes, d => d.V)).nice().range([H - m.b, m.t]);
  chart.append('g').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(10));
  chart.append('g').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(6));
  chart.append('text').attr('x', W / 2).attr('y', H - 4).attr('text-anchor', 'middle').attr('class', 'axis-label')
    .text(byStep ? 'transaction number' : 'days since creation');
  chart.append('text').attr('transform', `translate(12,${H / 2}) rotate(-90)`).attr('text-anchor', 'middle')
    .attr('class', 'axis-label').text('potential V (high = upstream)');
  // vertical jumps
  chart.append('g').selectAll('line').data(segs.filter(s => s.next)).join('line')
    .attr('x1', s => x(s.x1)).attr('x2', s => x(s.x1)).attr('y1', s => y(s.node.V)).attr('y2', s => y(s.next.V))
    .attr('stroke', s => s.next.V <= s.node.V ? '#1a8a1a' : '#c0392b').attr('stroke-dasharray', '2,2').attr('stroke-width', 1);
  // dwell segments coloured by sector
  chart.append('g').selectAll('line').data(segs).join('line')
    .attr('x1', s => x(s.x0)).attr('x2', s => x(Math.max(s.x1, s.x0 + xMax / 800)))
    .attr('y1', s => y(s.node.V)).attr('y2', s => y(s.node.V))
    .attr('stroke', s => color(s.node.sector)).attr('stroke-width', 5).attr('stroke-linecap', 'round')
    .on('mousemove', (e, s) => showTip(e, `<b>#${s.i} ${esc(s.node.name)}</b><br>${esc(s.node.sector)}<br>day ${s.arrive.toFixed(1)} · held ${fmtDays(s.dwell)}<br>V = ${s.node.V.toFixed(2)}${s.next ? `<br>→ ${esc(s.next.name)} (${esc(s.type)})` : ''}`))
    .on('mouseleave', hideTip);
  const leg = chart.append('g').attr('transform', `translate(${m.l + 8},${m.t})`);
  [...new Set(tr.steps.map(s => s.node.sector))].forEach((s, i) => {
    const g = leg.append('g').attr('transform', `translate(${i * 150},0)`);
    g.append('rect').attr('width', 10).attr('height', 10).attr('fill', color(s));
    g.append('text').attr('x', 14).attr('y', 9).attr('font-size', 11).text(s);
  });
}

function drawTraceTable() {
  const tr = state.trace;
  const rows = tr.steps.map(s => {
    const dV = s.next ? s.next.V - s.node.V : null;
    return `<tr>
      <td class="num">${s.i}</td>
      <td><span class="swatch" style="background:${color(s.node.sector)}"></span> <span class="link" data-id="${esc(s.node.id)}">${esc(s.node.name)}</span></td>
      <td>${esc(s.node.sector)}</td>
      <td class="num">${s.arrive.toFixed(1)}</td>
      <td class="num">${s.node.kind === 'node' ? fmtDays(s.dwell) : s.node.kind === 'source' ? 'created' : 'destroyed'}</td>
      <td>${s.next ? esc(s.next.name) : '—'}</td>
      <td>${s.type ? esc(s.type) : ''}</td>
      <td class="num">${s.link ? (100 * s.link.v / s.node.out).toFixed(1) + '%' : ''}</td>
      <td class="num ${dV === null ? '' : dV <= 0 ? 'down' : 'up'}">${dV === null ? '' : (dV <= 0 ? '↓ ' : '↑ ') + dV.toFixed(2)}</td>
    </tr>`;
  }).join('');
  document.getElementById('trace-table').innerHTML = `<table>
    <thead><tr><th>#</th><th>Node</th><th>Sector</th><th>Arrived (day)</th><th>Held for</th><th>Paid to</th><th>Transaction type</th><th>P(this route)</th><th>ΔV</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  document.querySelectorAll('#trace-table .link').forEach(el => el.addEventListener('click', () => {
    selectNode(el.dataset.id);
    document.getElementById('top').scrollIntoView({ behavior: 'smooth' });
  }));
}

function drawTraceOnGraph() {
  gTrace.selectAll('*').remove();
  const tr = state.trace;
  if (!tr || !document.getElementById('trace-on-graph').checked) return;
  const hops = tr.steps.filter(s => s.link);
  gTrace.selectAll('path').data(hops).join('path').attr('class', 'link-trace').attr('d', s => linkPath(s.link)).attr('stroke-width', 1.8);
  gTrace.selectAll('text').data(hops).join('text').attr('class', 'trace-num')
    .attr('x', s => (s.link.source.x + s.link.target.x) / 2).attr('y', s => (s.link.source.y + s.link.target.y) / 2 - 3)
    .text(s => s.i + 1);
}

// ---- through-node constraint UI -------------------------------------------------------
const nameToId = new Map(nodes.map(n => [n.name, n.id]));
function addThrough(id) {
  if (!id || state.through.includes(id)) return;
  state.through.push(id);
  renderChips();
}
function renderChips() {
  document.getElementById('through-chips').innerHTML = state.through.map(id =>
    `<span class="chip"><span class="swatch" style="background:${color(byId.get(id).sector)}"></span>${esc(byId.get(id).name)} <b data-id="${esc(id)}">×</b></span>`).join('');
  document.querySelectorAll('#through-chips b').forEach(el => el.addEventListener('click', () => {
    state.through = state.through.filter(x => x !== el.dataset.id);
    renderChips();
  }));
  gNodes.selectAll('g.node').classed('through', d => state.through.includes(d.id));
}

// ---- assumptions modal ----------------------------------------------------------------
const UI_ASSUMPTIONS = [
  ['Potential (x-axis)', 'Each node gets a scalar potential V found by least squares: minimise Σ w_e (V_source − V_target − 1)² over all transaction edges (a graph-Laplacian solve, L·V = b with b_i = Σ out-weights − Σ in-weights; a.k.a. HodgeRank / SpringRank). With unit weights every counterparty relationship counts once regardless of size, so sources (pure out-flow) float up and sinks fall down. Only edges ≥ the "min edge" value enter the solve; changing it, or switching to log($)/$ weights, changes the ordering. V is only defined up to a constant (mean set to 0). Money flowing to a lower V is "downhill".'],
  ['Layout', 'Nodes are sorted by V and cut into equal-count columns (left = highest V). Within a column, nodes are sorted top-to-bottom by connectivity = number of counterparties with an edge ≥ min edge (ties broken by throughput). Alternate rows are nudged right for legibility. Column labels show the mean V of that column. Circle area ∝ annual throughput; sources have black outlines, sinks dashed.'],
  ['Path highlighting', 'Clicking a node attributes flows proportionally ("money is fungible inside a node"): e.g. depth 2 downstream shows the node\'s outflows, plus the outflows of each recipient scaled by the share of that recipient\'s spending that came from the selected node. Upstream is the mirror image using inflow shares. Flows below 0.2% of the node\'s total are hidden.'],
  ['Random dollar', 'A dollar is a random walk on the annual flow network: at each node the next payee is chosen with probability = edge flow / node outflow; the time held is exponential with the node\'s mean holding time. Starting points are creation events chosen with probability ∝ their annual outflow. When you require nodes to be visited, the walk is sampled backwards from the first required node using inflow shares (exact because every node balances) and forwards from it, then rejection-sampled for the remaining nodes and creation-source filter. Transaction type is drawn ∝ the amounts of each type on that edge.'],
  ['What "creation" and "destruction" mean here', 'Federal spending creates money and federal taxes destroy it (MMT view; Treasury/Fed consolidated except the Fed\'s own interest income and remittances). Export receipts are dollars entering the domestic economy, import payments are dollars leaving it (they are not destroyed globally). Pension contributions leave circulation into retirement funds and pension benefits re-enter it. Bank credit and net saving are the private ("horizontal") creation/destruction channels that close every node\'s budget.'],
];
function renderAssumptions() {
  const a = DATA.assumptions.map(x => `<h4>${esc(x.title)}</h4><p>${esc(x.text)}</p>`).join('');
  const u = UI_ASSUMPTIONS.map(([t, x]) => `<h4>${esc(t)}</h4><p>${esc(x)}</p>`).join('');
  const s = DATA.sources.map(x => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.name)}</a></li>`).join('');
  const totals = ['FED_SRC', 'ROW_SRC', 'CREDIT', 'PEN_SRC', 'FED_SINK', 'ROW_SINK', 'SAVING', 'PEN_SINK'].map(id => {
    const n = byId.get(id);
    return `<tr><td>${esc(n.name)}</td><td class="num">${fmt(n.kind === 'source' ? n.out : n.in)}</td></tr>`;
  }).join('');
  const nipa = Object.entries(DATA.nipa).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">$${v.toLocaleString()}B</td></tr>`).join('');
  document.getElementById('assumptions').innerHTML = `
    <h3>Data construction (build_flows.py)</h3>${a}
    <h3>Visualisation & tracing (app.js)</h3>${u}
    <h3>Creation / destruction totals in this network</h3><table>${totals}</table>
    <h3>NIPA 2017 control totals used</h3><table>${nipa}</table>
    <h3>Sources</h3><ul>${s}</ul>`;
}

// ---- wiring ---------------------------------------------------------------------------
function rebuild() {
  computePotential();
  layout();
  drawBackground();
  drawNodes();
  if (state.selected) selectNode(state.selected);
  drawTraceOnGraph();
  if (state.trace) drawTraceChart(), drawTraceTable();
}

function init() {
  document.getElementById('meta').textContent =
    `${DATA.meta.nodeCount} nodes (${DATA.meta.industryNodes} industry nodes) · ${DATA.meta.linkCount.toLocaleString()} payment links · ${DATA.meta.units} · BEA data`;
  document.getElementById('legend').innerHTML = Object.entries(SECTOR_COLORS)
    .map(([s, c]) => `<span class="chip"><span class="swatch" style="background:${c}"></span>${esc(s)}</span>`).join('');
  document.getElementById('node-list').innerHTML = [...nodes].sort((a, b) => a.name.localeCompare(b.name))
    .map(n => `<option value="${esc(n.name)}">`).join('');
  const sf = document.getElementById('src-filter');
  nodes.filter(n => n.kind === 'source').forEach(n => sf.insertAdjacentHTML('beforeend', `<option value="${esc(n.id)}">${esc(n.name)}</option>`));

  ['weighting', 'min-edge', 'ncols', 'bg-edges'].forEach(id => document.getElementById(id).addEventListener('change', rebuild));
  document.getElementById('depth').addEventListener('input', e => {
    document.getElementById('depth-val').textContent = e.target.value;
    if (state.selected) selectNode(state.selected);
  });
  document.getElementById('search').addEventListener('change', e => {
    const id = nameToId.get(e.target.value);
    if (id) selectNode(id);
  });
  document.getElementById('through-add').addEventListener('click', () => {
    const inp = document.getElementById('through-input');
    addThrough(nameToId.get(inp.value));
    inp.value = '';
  });
  document.getElementById('btn-trace').addEventListener('click', runTrace);
  document.getElementById('trace-x').addEventListener('change', drawTraceChart);
  document.getElementById('trace-on-graph').addEventListener('change', drawTraceOnGraph);
  svg.on('click', e => { if (e.target === svg.node()) clearSelection(); });
  document.getElementById('btn-assumptions').addEventListener('click', () => document.getElementById('modal').classList.remove('hidden'));
  document.getElementById('modal-close').addEventListener('click', () => document.getElementById('modal').classList.add('hidden'));
  document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') e.target.classList.add('hidden'); });
  window.addEventListener('resize', rebuild);
  renderAssumptions();
  rebuild();
}
init();
