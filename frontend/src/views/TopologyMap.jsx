import { useEffect, useMemo, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Network, Radio, ShieldCheck } from 'lucide-react';
import { shortRelation } from '../components/format.js';
import { confirmationStatus } from '../lib/filters.js';

const MAX_TOPOLOGY_EDGES = 140;
const NODE_TYPES = { topology: TopologyNode };

export function TopologyMap({ edges = [], networkEdges = [], companies = [] }) {
  const [focus, setFocus] = useState('');
  const [depth, setDepth] = useState(2);
  const [showExposure, setShowExposure] = useState(true);
  const [selectedEdge, setSelectedEdge] = useState(null);
  const sourceEdges = networkEdges.length ? networkEdges : edges;
  const issuerNames = useMemo(() => companies.map((company) => company.company).filter(Boolean), [companies]);
  const options = useMemo(() => focusOptions(sourceEdges, issuerNames), [sourceEdges, issuerNames]);

  useEffect(() => {
    if (focus && options.includes(focus)) return;
    setFocus(options.includes('NVIDIA Corporation') ? 'NVIDIA Corporation' : options[0] || '');
  }, [focus, options]);

  const topology = useMemo(
    () => buildTopology(sourceEdges, issuerNames, focus, depth, showExposure),
    [sourceEdges, issuerNames, focus, depth, showExposure],
  );
  const detail = selectedEdge ? topology.edgeById.get(selectedEdge.id) : null;

  if (!sourceEdges.length) return <div className="empty topology-empty">No relationship edges match the active filters.</div>;

  return (
    <section className="topology-lab">
      <div className="topology-toolbar">
        <div>
          <h2><Network size={18} /> Supply-chain topology lab</h2>
          <p>Interactive evidence map. Arrow direction is disclosed dependency source → reporting issuer; class exposures are visually distinct from named counterparties.</p>
        </div>
        <label><span>Focus company</span>
          <select value={focus} onChange={(event) => { setFocus(event.target.value); setSelectedEdge(null); }}>
            {options.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        <label><span>Topology radius</span>
          <select value={depth} onChange={(event) => setDepth(Number(event.target.value))}>
            <option value={1}>One hop</option><option value={2}>Two hops</option>
          </select>
        </label>
        <label className="topology-checkbox"><input type="checkbox" checked={showExposure} onChange={(event) => setShowExposure(event.target.checked)} /><span>Include dependency classes</span></label>
      </div>
      <div className="topology-stats">
        <span><b>{topology.companyCount}</b> company / organization nodes</span>
        <span><b>{topology.exposureCount}</b> anonymous dependency-class nodes</span>
        <span><b>{topology.edges.length}</b> displayed relations (of {sourceEdges.length} after global filters)</span>
        <span>{networkEdges.length ? <><ShieldCheck size={14} /> canonical candidates; review status still applies</> : <><Radio size={14} /> raw extraction signals; not all are verified counterparties</>}</span>
      </div>
      <div className="topology-layout">
        <div className="topology-canvas">
          <ReactFlow
            nodes={topology.nodes}
            edges={topology.edges}
            nodeTypes={NODE_TYPES}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            minZoom={0.12}
            onEdgeClick={(_, edge) => setSelectedEdge(edge)}
            onPaneClick={() => setSelectedEdge(null)}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={22} size={1} color="#dce5ed" />
            <MiniMap nodeColor={(node) => miniMapColor(node.data.kind)} maskColor="rgba(241, 245, 249, .62)" />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <aside className="topology-detail panel">
          <div className="panel-head"><h2>{detail ? 'Selected relationship' : 'How to read this map'}</h2></div>
          {detail ? <EdgeDetail edge={detail} /> : <TopologyLegend networkReady={Boolean(networkEdges.length)} />}
        </aside>
      </div>
    </section>
  );
}

function TopologyNode({ data }) {
  return <div className={`topology-node ${data.kind} ${data.focus ? 'focus' : ''}`}>
    <Handle type="target" position={Position.Left} />
    <strong>{data.label}</strong><small>{data.meta}</small>
    <Handle type="source" position={Position.Right} />
  </div>;
}

function EdgeDetail({ edge }) {
  return <div className="topology-edge-detail">
    <strong>{edge.object} <span>→</span> {edge.subject}</strong>
    <dl>
      <div><dt>Relation</dt><dd>{shortRelation(edge.relation_type)}</dd></div>
      <div><dt>Modality</dt><dd>{edge.modality || 'not recorded'}</dd></div>
      <div><dt>Evidence</dt><dd>{edge.evidence_count || 1} supporting passage{Number(edge.evidence_count || 1) === 1 ? '' : 's'}</dd></div>
      <div><dt>Status</dt><dd>{confirmationStatus(edge)}</dd></div>
      {edge.product_or_service && <div><dt>Product / service</dt><dd>{edge.product_or_service}</dd></div>}
      {edge.risk_flags?.length > 0 && <div><dt>Audit flags</dt><dd>{edge.risk_flags.join(', ')}</dd></div>}
    </dl>
    {edge.source_urls && <a href={String(edge.source_urls).split(';')[0]} target="_blank" rel="noreferrer">Open supporting SEC filing</a>}
  </div>;
}

function TopologyLegend({ networkReady }) {
  return <div className="topology-legend">
    <p><b>Solid teal nodes</b> are issuers in the selected company universe. Dark blue nodes are named organizations; dashed amber nodes are disclosed but anonymous classes such as “supplier dependency class.”</p>
    <p><b>Line color</b> denotes modality: green current fact, amber forward-looking, red risk/hypothetical, blue strategic. Width is logarithmic evidence count.</p>
    <p>{networkReady ? 'This run has a canonical network projection, so generic unresolved objects have already been excluded. Canonical is a normalized candidate layer, not a claim that every displayed edge is verified; inspect each edge status before using it as fact.' : 'This older/raw run is useful for exploring disclosure coverage, but a visual edge is not a verified supplier/customer fact.'}</p>
  </div>;
}

function buildTopology(rows, issuerNames, focus, depth, showExposure) {
  const issuers = new Set(issuerNames);
  const normalized = collapseRows(rows, issuers, showExposure);
  const ego = selectEgo(normalized, focus, depth);
  const capped = ego.sort((a, b) => Number(b.evidence_count || 0) - Number(a.evidence_count || 0)).slice(0, MAX_TOPOLOGY_EDGES);
  const visible = new Set(capped.flatMap((edge) => [edge.object, edge.subject]));
  if (focus) visible.add(focus);
  const nodeStats = new Map([...visible].map((name) => [name, { in: 0, out: 0, evidence: 0 }]));
  capped.forEach((edge) => {
    const weight = Number(edge.evidence_count || 1);
    nodeStats.get(edge.object).out += 1; nodeStats.get(edge.object).evidence += weight;
    nodeStats.get(edge.subject).in += 1; nodeStats.get(edge.subject).evidence += weight;
  });
  const positions = topologyPositions([...visible], focus, normalized, nodeStats);
  const nodes = [...visible].map((name) => {
    const stats = nodeStats.get(name) || { in: 0, out: 0, evidence: 0 };
    const kind = nodeKind(name, issuers);
    return { id: nodeId(name), type: 'topology', position: positions.get(name) || { x: 0, y: 0 }, data: { label: trim(name, 34), meta: `${stats.in} in · ${stats.out} out · ${stats.evidence} evidence`, kind, focus: name === focus } };
  });
  const edgeById = new Map();
  const flowEdges = capped.map((edge) => {
    const id = `${nodeId(edge.object)}>${nodeId(edge.subject)}>${edge.relation_type}:${edge.modality}`;
    edgeById.set(id, edge);
    return { id, source: nodeId(edge.object), target: nodeId(edge.subject), label: capped.length <= 32 ? shortRelation(edge.relation_type) : undefined, markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 }, style: edgeStyle(edge), className: `topology-edge ${edge.modality || ''}` };
  });
  return { nodes, edges: flowEdges, edgeById, companyCount: nodes.filter((node) => node.data.kind !== 'exposure').length, exposureCount: nodes.filter((node) => node.data.kind === 'exposure').length };
}

function collapseRows(rows, issuers, showExposure) {
  const index = new Map();
  rows.forEach((row) => {
    const object = String(row.object || '').trim();
    const subject = String(row.subject || '').trim();
    if (!object || !subject || object === subject) return;
    const exposure = nodeKind(object, issuers) === 'exposure';
    if (!showExposure && exposure) return;
    const relation_type = String(row.relation_type || row.relationship_type || 'dependency');
    const modality = String(row.modality || 'not_recorded');
    const key = `${object}|${subject}|${relation_type}|${modality}`;
    const previous = index.get(key);
    index.set(key, previous ? { ...previous, evidence_count: Number(previous.evidence_count || 0) + Number(row.evidence_count || 1), source_urls: joinValues(previous.source_urls, row.source_urls) } : { ...row, object, subject, relation_type, modality, evidence_count: Number(row.evidence_count || 1) });
  });
  return [...index.values()];
}

function selectEgo(rows, focus, depth) {
  if (!focus) return rows.slice(0, MAX_TOPOLOGY_EDGES);
  const distance = new Map([[focus, 0]]);
  let frontier = new Set([focus]);
  for (let hop = 1; hop <= depth; hop += 1) {
    const next = new Set();
    rows.forEach((edge) => { if (frontier.has(edge.object)) next.add(edge.subject); if (frontier.has(edge.subject)) next.add(edge.object); });
    next.forEach((name) => { if (!distance.has(name)) distance.set(name, hop); });
    frontier = next;
  }
  return rows.filter((edge) => distance.has(edge.object) && distance.has(edge.subject));
}

function topologyPositions(names, focus, rows, stats) {
  const positions = new Map();
  if (!names.length) return positions;
  const root = focus && names.includes(focus) ? focus : names[0];
  positions.set(root, { x: 0, y: 0 });
  const distances = distancesFrom(root, rows);
  const rings = new Map();
  names.filter((name) => name !== root).forEach((name) => { const ring = Math.min(distances.get(name) || 2, 2); rings.set(ring, [...(rings.get(ring) || []), name]); });
  [1, 2].forEach((ring) => {
    const radius = ring === 1 ? 350 : 690;
    const items = (rings.get(ring) || []).sort((a, b) => (stats.get(b)?.evidence || 0) - (stats.get(a)?.evidence || 0) || a.localeCompare(b));
    items.forEach((name, index) => { const angle = -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(items.length, 1); positions.set(name, { x: Math.round(Math.cos(angle) * radius), y: Math.round(Math.sin(angle) * radius) }); });
  });
  return positions;
}

function distancesFrom(root, rows) {
  const result = new Map([[root, 0]]); let frontier = new Set([root]);
  for (let hop = 1; hop <= 2; hop += 1) {
    const next = new Set();
    rows.forEach((edge) => { if (frontier.has(edge.object) && !result.has(edge.subject)) next.add(edge.subject); if (frontier.has(edge.subject) && !result.has(edge.object)) next.add(edge.object); });
    next.forEach((name) => result.set(name, hop)); frontier = next;
  }
  return result;
}

function focusOptions(rows, issuerNames) {
  const issuerSet = new Set(issuerNames); const candidates = new Set(issuerNames);
  rows.forEach((edge) => { candidates.add(edge.subject); if (nodeKind(edge.object, issuerSet) !== 'exposure') candidates.add(edge.object); });
  return [...candidates].filter(Boolean).sort((a, b) => a.localeCompare(b));
}

function nodeKind(name, issuers) {
  if (issuers.has(name)) return 'issuer';
  if (/\b(class|dependency|capacity|supplier|vendor|customer|provider|contents)\b/i.test(name)) return 'exposure';
  return 'counterparty';
}

function edgeStyle(edge) {
  const color = { current_fact: '#15803d', forward_looking: '#b45309', risk_hypothetical: '#b42318', strategic: '#1d4ed8' }[edge.modality] || '#5b6b7b';
  return { stroke: color, strokeWidth: Math.min(6, 1.3 + Math.log2(Number(edge.evidence_count || 1))), strokeDasharray: edge.modality === 'risk_hypothetical' ? '7 5' : edge.modality === 'forward_looking' ? '3 5' : undefined };
}

function miniMapColor(kind) { return { issuer: '#0f766e', counterparty: '#1d4ed8', exposure: '#b45309' }[kind] || '#5b6b7b'; }
function nodeId(value) { return `node:${encodeURIComponent(value)}`; }
function trim(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }
function joinValues(first, second) { return [...new Set(`${first || ''};${second || ''}`.split(';').filter(Boolean))].join(';'); }
