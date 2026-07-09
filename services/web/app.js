// minion canvas: a dependency-free node editor over the platform API.
// Same node/edge model as React Flow, zero external deps (offline-first);
// the API surface is UI-agnostic, so React Flow drops in later unchanged.
'use strict';

const SVGNS = 'http://www.w3.org/2000/svg';
const NODE_W = 140, NODE_H = 58, GAP = 180, X0 = 40, Y0 = 110;

// Pipeline state: an ordered list of nodes (source first, then steps).
const state = {
  nodes: [{ kind: 'source', type: 'folder' }],
  graphId: null,
};

const $ = (id) => document.getElementById(id);
const hdr = () => ({ 'X-Tenant-Id': $('tenant').value.trim() || 't1' });

function nodeId(node, index) {
  return `${node.kind}:${node.type}#${index}`;
}

// The graph spec the API stores and runs (ids are assigned server-side).
function buildSpec() {
  const stages = state.nodes.map((n) => {
    if (n.kind === 'source') {
      return { source: n.type, root: 'bot_dir', exts: ['.jpg'] };
    }
    if (n.kind === 'sink') return { sink: n.type };
    return { step: n.type };
  });
  return { bot: 'ui', stages };
}

function setStatus(text) { $('status').textContent = text; }

function log(line) {
  const li = document.createElement('li');
  li.textContent = line;
  $('log').appendChild(li);
  $('log').scrollTop = $('log').scrollHeight;
}

// ---- rendering -----------------------------------------------------------

function render() {
  const nodes = $('nodes');
  const edges = $('edges');
  nodes.innerHTML = '';
  edges.innerHTML = '';
  state.nodes.forEach((n, i) => {
    if (i > 0) drawEdge(edges, i - 1, i);
    nodes.appendChild(nodeEl(n, i));
  });
}

function nodeEl(n, i) {
  const el = document.createElement('div');
  el.className = `node ${n.kind}`;
  el.id = `n-${nodeId(n, i)}`;
  el.style.left = `${X0 + i * GAP}px`;
  el.style.top = `${Y0}px`;
  el.innerHTML =
    `<div class="kind">${n.kind}</div><div class="name">${n.type}</div>` +
    '<div class="ms"></div>';
  return el;
}

function centerY() { return Y0 + NODE_H / 2; }

function drawEdge(svg, a, b) {
  const x1 = X0 + a * GAP + NODE_W;
  const x2 = X0 + b * GAP;
  const y = centerY();
  const path = document.createElementNS(SVGNS, 'path');
  const mid = (x1 + x2) / 2;
  path.setAttribute('d', `M${x1},${y} C${mid},${y} ${mid},${y} ${x2},${y}`);
  path.setAttribute('class', 'edge');
  path.id = `e-${a}-${b}`;
  svg.appendChild(path);
}

// ---- palette / construction ---------------------------------------------

async function loadCatalog() {
  const res = await fetch('/catalog', { headers: hdr() });
  const cat = await res.json();
  fillPalette('sources', cat.sources, (t) => setSource(t), 'src');
  fillPalette('steps', cat.steps, (t) => addStep(t));
  fillPalette('sinks', cat.sinks, (t) => addSink(t));
}

function fillPalette(id, items, onClick, cls) {
  const box = $(id);
  box.innerHTML = '';
  items.forEach((t) => {
    const chip = document.createElement('span');
    chip.className = `chip ${cls || ''}`;
    chip.textContent = t;
    chip.dataset.type = t;
    chip.onclick = () => onClick(t);
    box.appendChild(chip);
  });
}

function setSource(type) { state.nodes[0] = { kind: 'source', type }; reset(); }
function addStep(type) { state.nodes.push({ kind: 'step', type }); reset(); }
function addSink(type) { state.nodes.push({ kind: 'sink', type }); reset(); }

function reset() {
  state.graphId = null;
  render();
}

// ---- save / run / animate -----------------------------------------------

async function save() {
  const body = { name: 'ui-pipeline', spec: buildSpec() };
  const res = await fetch('/graphs', {
    method: 'POST',
    headers: { ...hdr(), 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const graph = await res.json();
  state.graphId = graph.id;
  setStatus(`saved graph ${graph.id.slice(0, 8)}`);
  return graph.id;
}

async function run() {
  render();
  const graphId = state.graphId || (await save());
  const inputRef = $('inputRef').value.trim();
  if (!inputRef) { setStatus('need an input ref'); return; }
  const res = await fetch('/runs', {
    method: 'POST',
    headers: { ...hdr(), 'content-type': 'application/json' },
    body: JSON.stringify({ graph_id: graphId, input_ref: inputRef }),
  });
  const run = await res.json();
  setStatus(`run ${run.id.slice(0, 8)} ...`);
  await streamEvents(run.id);
  await loadUsage();
  setStatus(`run ${run.id.slice(0, 8)} ${run.status}`);
}

async function streamEvents(runId) {
  const res = await fetch(`/runs/${runId}/events`, { headers: hdr() });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split('\n').find((l) => l.startsWith('data:'));
      if (line) handleEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}

function handleEvent(ev) {
  const el = $(`n-${ev.node}`);
  log(`${ev.node} ${ev.phase} ${ev.disposition}`);
  if (!el) return;
  if (ev.phase === 'entered') {
    el.classList.add('active');
  } else {
    el.classList.remove('active');
    if (ev.disposition) el.classList.add(`done-${ev.disposition}`);
    el.dataset.ts = ev.ts;
  }
}

async function loadUsage() {
  const res = await fetch('/usage', { headers: hdr() });
  const u = await res.json();
  $('usage').textContent =
    `usage: ${u.nodes} nodes, ${u.total_ms.toFixed(2)} ms, ` +
    `${u.compute_ru.toExponential(2)} RU`;
}

// ---- boot ----------------------------------------------------------------

$('save').onclick = () => save();
$('run').onclick = () => run();
loadCatalog().then(render);
