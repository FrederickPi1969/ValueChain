import { useEffect, useMemo, useRef, useState } from 'react';
import { MultiDirectedGraph } from 'graphology';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import Sigma from 'sigma';
import { Network, Radio, ShieldCheck } from 'lucide-react';
import { shortRelation } from '../components/format.js';
import { confirmationStatus } from '../lib/filters.js';

const MAX_EGO_EDGES = 600;
const MAX_GLOBAL_EDGES = 3_000;
const RELATION_COLORS = {
  supplies_to: '#38bdf8', supplier_dependency: '#38bdf8', manufacturing_dependency: '#a78bfa', foundry_dependency: '#a78bfa',
  customer_dependency: '#fbbf24', purchases_from: '#fbbf24', cloud_or_hosting_dependency: '#34d399',
  data_center_dependency: '#34d399', power_or_utility_dependency: '#fb7185', network_or_interconnection_dependency: '#f97316',
  distribution_or_channel_dependency: '#e879f9', licensing_dependency: '#60a5fa', strategic_partner: '#22d3ee', co_investment: '#f472b6',
};

export function TopologyMap({ edges = [], networkEdges = [], companies = [] }) {
  const [focus, setFocus] = useState('');
  const [scope, setScope] = useState('global');
  const [depth, setDepth] = useState(1);
  const [showExposure, setShowExposure] = useState(false);
  const [showIndustries, setShowIndustries] = useState(true);
  const [enabledTypes, setEnabledTypes] = useState([]);
  const [selectedEdge, setSelectedEdge] = useState(null);
  const sourceEdges = networkEdges.length ? networkEdges : edges;
  const issuerNames = useMemo(() => companies.map((company) => company.company).filter(Boolean), [companies]);
  const options = useMemo(() => focusOptions(sourceEdges, issuerNames), [sourceEdges, issuerNames]);
  const availableTypes = useMemo(() => relationTypes(sourceEdges), [sourceEdges]);

  useEffect(() => {
    if (!focus || !options.includes(focus)) setFocus(options.includes('NVIDIA Corporation') ? 'NVIDIA Corporation' : options[0] || '');
  }, [focus, options]);
  useEffect(() => setEnabledTypes(availableTypes.map((row) => row.type)), [availableTypes]);

  const topology = useMemo(
    () => buildTopology(sourceEdges, companies, focus, scope, depth, showExposure, showIndustries, enabledTypes),
    [sourceEdges, companies, focus, scope, depth, showExposure, showIndustries, enabledTypes],
  );

  if (!sourceEdges.length) return <div className="empty topology-empty">No relationship edges match the active filters.</div>;
  return (
    <section className="topology-lab force-topology">
      <div className="topology-toolbar">
        <div>
          <h2><Network size={18} /> Supply-chain knowledge graph</h2>
          <p>ForceAtlas2 layout: nearby nodes have denser disclosed relationships. Arrow direction is dependency source → reporting issuer; click or hover an edge to inspect its evidence.</p>
        </div>
        <label><span>Graph scope</span><select value={scope} onChange={(event) => { setScope(event.target.value); setSelectedEdge(null); }}><option value="global">Global loaded graph</option><option value="ego">Company ego graph</option></select></label>
        {scope === 'ego' ? <><label><span>Focus company</span><select value={focus} onChange={(event) => { setFocus(event.target.value); setSelectedEdge(null); }}>{options.map((name) => <option key={name} value={name}>{name}</option>)}</select></label><label><span>Graph radius</span><select value={depth} onChange={(event) => setDepth(Number(event.target.value))}><option value={1}>One hop</option><option value={2}>Two hops</option></select></label></> : <div className="topology-scope-note">Global view: top {MAX_GLOBAL_EDGES.toLocaleString()} relations by evidence</div>}
        <label className="topology-checkbox"><input type="checkbox" checked={showExposure} onChange={(event) => setShowExposure(event.target.checked)} /><span>Include anonymous / low-quality objects</span></label>
        <label className="topology-checkbox"><input type="checkbox" checked={showIndustries} onChange={(event) => setShowIndustries(event.target.checked)} /><span>Show industry anchors</span></label>
      </div>
      <div className="topology-stats">
        <span><b>{topology.companyCount}</b> named entities</span><span><b>{topology.industryCount}</b> industry anchors</span><span><b>{topology.exposureCount}</b> anonymous/class nodes</span><span><b>{topology.rows.length}</b> displayed relations</span>
        {scope === 'ego' && <span><b>+{topology.hopDelta}</b> nodes added by two-hop expansion</span>}
        <span>{networkEdges.length ? <><ShieldCheck size={14} /> canonical candidates; review state applies</> : <><Radio size={14} /> raw extraction; not verified counterparties</>}</span>
      </div>
      <div className="relation-legend" aria-label="Relationship type filters">
        {availableTypes.map(({ type, count }) => <button key={type} type="button" className={enabledTypes.includes(type) ? 'active' : ''} onClick={() => setEnabledTypes((current) => current.includes(type) ? current.filter((item) => item !== type) : [...current, type])}>
          <i style={{ background: relationColor(type) }} />{shortRelation(type)} <b>{count}</b>
        </button>)}
      </div>
      <div className="topology-layout">
        <ForceGraph topology={topology} onSelect={setSelectedEdge} />
        <aside className="topology-detail panel">
          <div className="panel-head"><h2>{selectedEdge ? 'Selected relationship' : 'Reading the graph'}</h2></div>
          {selectedEdge ? <EdgeDetail edge={selectedEdge} /> : <TopologyLegend networkReady={Boolean(networkEdges.length)} />}
        </aside>
      </div>
    </section>
  );
}

function ForceGraph({ topology, onSelect }) {
  const containerRef = useRef(null);
  useEffect(() => {
    if (!containerRef.current || !topology.rows.length) return undefined;
    const graph = new MultiDirectedGraph();
    topology.nodes.forEach((node) => graph.addNode(node.id, node));
    topology.rows.forEach((edge, index) => graph.addDirectedEdgeWithKey(edge.id || `edge-${index}`, edge.objectId, edge.subjectId, {
      color: edge.isIndustryMembership ? '#475569' : relationColor(edge.relation_type), size: edge.isIndustryMembership ? 0.5 : Math.min(4, 0.8 + Math.log2(Number(edge.evidence_count || 1) + 1)), type: edge.isIndustryMembership ? 'line' : 'arrow', edge,
    }));
    forceAtlas2.assign(graph, { iterations: Math.max(45, Math.min(180, Math.round(700 / Math.max(graph.order, 1)))), settings: { ...forceAtlas2.inferSettings(graph), gravity: 1.3, scalingRatio: 7, slowDown: 2, barnesHutOptimize: graph.order > 200, barnesHutTheta: 0.6 } });
    let hoveredNode = null;
    const renderer = new Sigma(graph, containerRef.current, {
      renderEdgeLabels: false, enableEdgeEvents: true, zIndex: true, defaultEdgeType: 'arrow', labelColor: { color: '#e2e8f0' }, labelFont: 'Inter, ui-sans-serif, system-ui', labelSize: 13, labelWeight: '600', labelRenderedSizeThreshold: 10, stagePadding: 38,
      nodeReducer: (node, data) => {
        if (!hoveredNode) return data;
        const connected = node === hoveredNode || graph.areNeighbors(node, hoveredNode);
        return connected ? data : { ...data, color: '#334155', label: '', zIndex: 0 };
      },
      edgeReducer: (edge, data) => {
        if (!hoveredNode) return data;
        const [source, target] = graph.extremities(edge);
        return source === hoveredNode || target === hoveredNode ? { ...data, size: data.size + 1.1 } : { ...data, color: '#334155', size: 0.35, zIndex: 0 };
      },
    });
    renderer.on('enterNode', ({ node }) => { hoveredNode = node; renderer.refresh(); });
    renderer.on('leaveNode', () => { hoveredNode = null; renderer.refresh(); });
    renderer.on('enterEdge', ({ edge }) => onSelect(graph.getEdgeAttribute(edge, 'edge')));
    renderer.on('clickEdge', ({ edge }) => onSelect(graph.getEdgeAttribute(edge, 'edge')));
    renderer.on('clickNode', ({ node }) => {
      const nodeEdges = graph.edges(node).map((edge) => graph.getEdgeAttribute(edge, 'edge'));
      if (nodeEdges.length) onSelect(nodeEdges.sort((a, b) => Number(b.evidence_count || 0) - Number(a.evidence_count || 0))[0]);
    });
    return () => renderer.kill();
  }, [topology, onSelect]);
  return <div className="topology-canvas sigma-canvas" ref={containerRef} />;
}

function EdgeDetail({ edge }) {
  return <div className="topology-edge-detail"><strong>{edge.object} <span>→</span> {edge.subject}</strong><dl>
    <div><dt>Relation</dt><dd><i className="relation-dot" style={{ background: relationColor(edge.relation_type) }} /> {shortRelation(edge.relation_type)}</dd></div>
    <div><dt>Modality</dt><dd>{edge.modality || 'not recorded'}</dd></div><div><dt>Evidence</dt><dd>{edge.evidence_count || 1} supporting passage{Number(edge.evidence_count || 1) === 1 ? '' : 's'}</dd></div>
    <div><dt>Status</dt><dd>{confirmationStatus(edge)}</dd></div>{edge.product_or_service && <div><dt>Product / service</dt><dd>{edge.product_or_service}</dd></div>}{edge.risk_flags?.length > 0 && <div><dt>Audit flags</dt><dd>{edge.risk_flags.join(', ')}</dd></div>}
  </dl>{edge.source_urls && <a href={String(edge.source_urls).split(';')[0]} target="_blank" rel="noreferrer">Open supporting SEC filing</a>}</div>;
}

function TopologyLegend({ networkReady }) {
  return <div className="topology-legend"><p><b>Node color:</b> teal is an issuer in the chosen universe; blue is a named external organization; amber is an anonymous/class object and is hidden by default. Larger nodes have more relationships in the selected subgraph.</p><p><b>Edge color:</b> encodes the relationship type shown in the filter legend—not confidence. Hover or click an edge to see the exact type, direction, status, and supporting evidence.</p><p>{networkReady ? 'The graph is a normalized candidate layer. “accepted” is an explicit review state; all other edges remain candidates.' : 'This raw graph is for exploration only, not a verified supplier/customer dataset.'}</p></div>;
}

function buildTopology(rows, companies, focus, scope, depth, showExposure, showIndustries, enabledTypes) {
  const issuers = new Set(companies.map((company) => company.company).filter(Boolean)); const allowed = new Set(enabledTypes);
  const normalized = collapseRows(rows, issuers, showExposure).filter((edge) => allowed.has(edge.relation_type));
  const oneHop = selectEgo(normalized, focus, 1);
  const scoped = scope === 'ego' ? selectEgo(normalized, focus, depth) : normalized;
  const cap = scope === 'ego' ? MAX_EGO_EDGES : MAX_GLOBAL_EDGES;
  const ego = scoped.sort((a, b) => Number(b.evidence_count || 0) - Number(a.evidence_count || 0)).slice(0, cap);
  const visibleCompanies = scope === 'global' ? companies : companies.filter((company) => company.company === focus || ego.some((edge) => edge.object === company.company || edge.subject === company.company));
  const industryMemberships = showIndustries ? buildIndustryMemberships(visibleCompanies) : [];
  const names = new Set(ego.flatMap((edge) => [edge.object, edge.subject])); industryMemberships.forEach((edge) => { names.add(edge.object); names.add(edge.subject); }); if (focus) names.add(focus);
  const stats = new Map([...names].map((name) => [name, { degree: 0, evidence: 0 }]));
  ego.forEach((edge) => { const weight = Number(edge.evidence_count || 1); [edge.object, edge.subject].forEach((name) => { const stat = stats.get(name); stat.degree += 1; stat.evidence += weight; }); });
  const companyRole = new Map(companies.map((company) => [company.company, industryGroup(company.role)]));
  const nodes = [...names].map((name) => { const stat = stats.get(name) || { degree: 0, evidence: 0 }; const kind = nodeKind(name, issuers); const group = companyRole.get(name); const position = industryPosition(name, group, kind); return { id: nodeId(name), label: trim(name, 34), x: position.x, y: position.y, size: kind === 'industry' ? 16 : 5 + Math.min(13, Math.sqrt(stat.degree) * 3), color: nodeColor(kind), forceLabel: kind === 'industry' || name === focus || stat.degree >= 4, zIndex: name === focus ? 3 : kind === 'industry' ? 2 : 1, fixed: kind === 'industry', kind }; });
  const nodeByName = new Map(nodes.map((node) => [node.id.slice(5), node.id]));
  const graphRows = [...ego, ...industryMemberships].map((edge, index) => ({ ...edge, id: `rel-${index}-${nodeId(edge.object)}-${nodeId(edge.subject)}-${edge.relation_type}`, objectId: nodeByName.get(encodeURIComponent(edge.object)), subjectId: nodeByName.get(encodeURIComponent(edge.subject)) })).filter((edge) => edge.objectId && edge.subjectId);
  const oneHopNodes = new Set(oneHop.flatMap((edge) => [edge.object, edge.subject]));
  return { rows: graphRows, nodes, companyCount: nodes.filter((node) => node.kind === 'issuer' || node.kind === 'counterparty').length, industryCount: nodes.filter((node) => node.kind === 'industry').length, exposureCount: nodes.filter((node) => node.kind === 'exposure').length, hopDelta: Math.max(0, names.size - oneHopNodes.size) };
}

function buildIndustryMemberships(companies) { return companies.filter((company) => company.company).map((company) => ({ object: `Industry · ${industryGroup(company.role)}`, subject: company.company, relation_type: 'industry_membership', modality: 'taxonomy', evidence_count: 0, confirmation_status: 'taxonomy', review_status: 'taxonomy', isIndustryMembership: true })); }
function industryGroup(role) { const value = String(role || '').toLowerCase(); if (/(power|cooling|generation|grid)/.test(value)) return 'Power & thermal'; if (/(data_center|data_centers|colocation|server|networking|optical|edge_cloud)/.test(value)) return 'Data-center infrastructure'; if (/(foundry|semiconductor|accelerator|memory|semicap|chip)/.test(value)) return 'Semiconductors & compute'; if (/(cloud|hyperscaler|database|platform)/.test(value)) return 'Cloud & platforms'; if (/(software|observability|ai_software)/.test(value)) return 'AI software'; return 'AI infrastructure'; }
function industryPosition(name, group, kind) { const groups = ['Semiconductors & compute', 'Cloud & platforms', 'Data-center infrastructure', 'Power & thermal', 'AI software', 'AI infrastructure']; const index = Math.max(0, groups.indexOf(kind === 'industry' ? name.replace('Industry · ', '') : group)); const angle = -Math.PI / 2 + (Math.PI * 2 * index) / groups.length; const radius = kind === 'industry' ? 1.6 : group ? 1.25 : 0.5; return { x: Math.cos(angle) * radius + seededCoordinate(name, 1) * (kind === 'industry' ? .02 : .22), y: Math.sin(angle) * radius + seededCoordinate(name, 2) * (kind === 'industry' ? .02 : .22) }; }

function collapseRows(rows, issuers, showExposure) { const index = new Map(); rows.forEach((row) => { const object = String(row.object || '').trim(); const subject = String(row.subject || '').trim(); if (!object || !subject || object === subject || (!showExposure && nodeKind(object, issuers) === 'exposure')) return; const relation_type = String(row.relation_type || row.relationship_type || 'dependency'); const modality = String(row.modality || 'not_recorded'); const key = `${object}|${subject}|${relation_type}|${modality}`; const prior = index.get(key); index.set(key, prior ? { ...prior, evidence_count: Number(prior.evidence_count || 0) + Number(row.evidence_count || 1), source_urls: joinValues(prior.source_urls, row.source_urls) } : { ...row, object, subject, relation_type, modality, evidence_count: Number(row.evidence_count || 1) }); }); return [...index.values()]; }
function selectEgo(rows, focus, depth) { if (!focus) return rows; const distance = new Map([[focus, 0]]); let frontier = new Set([focus]); for (let hop = 1; hop <= depth; hop += 1) { const next = new Set(); rows.forEach((edge) => { if (frontier.has(edge.object)) next.add(edge.subject); if (frontier.has(edge.subject)) next.add(edge.object); }); next.forEach((name) => { if (!distance.has(name)) distance.set(name, hop); }); frontier = next; } return rows.filter((edge) => distance.has(edge.object) && distance.has(edge.subject)); }
function relationTypes(rows) { const counts = new Map(); rows.forEach((row) => { const type = String(row.relation_type || row.relationship_type || 'dependency'); counts.set(type, (counts.get(type) || 0) + 1); }); return [...counts].map(([type, count]) => ({ type, count })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type)); }
function focusOptions(rows, issuers) { const issuerSet = new Set(issuers); const names = new Set(issuers); rows.forEach((edge) => { names.add(edge.subject); if (nodeKind(edge.object, issuerSet) !== 'exposure') names.add(edge.object); }); return [...names].filter(Boolean).sort((a, b) => a.localeCompare(b)); }
function nodeKind(name, issuers) { if (String(name).startsWith('Industry · ')) return 'industry'; if (issuers.has(name)) return 'issuer'; return isAnonymousOrLowQuality(name) ? 'exposure' : 'counterparty'; }
function isAnonymousOrLowQuality(name) { const value = String(name || '').trim(); return /^(?:(?:direct|major|large|significant)\s+)?(?:customer|customers|supplier|suppliers|vendor|vendors|distributor|distributors|partner|partners)\s+(?:[a-z](?:\s+(?:and|or)\s+[a-z])?|[0-9]+)$/i.test(value) || /^(?:ai|cloud|software|service|electric|investment|specialty)\s+(?:and\s+)?(?:ai\s+)?(?:providers|companies|titans|neoclouds)$/i.test(value) || /^(a small number of customers|supplier dependency class|data center or compute capacity class|power, utility, or cooling supply class)$/i.test(value) || /^(contents?|table of contents)\b/i.test(value) || /^(pte\.?|pty\.?|ltd\.?|inc\.?)\s*(ltd\.?|limited)?$/i.test(value) || /\b(class|dependency class|capacity class)\b/i.test(value); }
function relationColor(type) { return RELATION_COLORS[type] || '#94a3b8'; }
function nodeColor(kind) { return { issuer: '#2dd4bf', counterparty: '#60a5fa', exposure: '#fbbf24', industry: '#f472b6' }[kind] || '#94a3b8'; }
function nodeId(value) { return `node:${encodeURIComponent(value)}`; }
function seededCoordinate(value, salt) { let hash = 2166136261 + salt; for (let i = 0; i < value.length; i += 1) { hash ^= value.charCodeAt(i); hash = Math.imul(hash, 16777619); } return ((hash >>> 0) % 1000) / 1000; }
function trim(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }
function joinValues(first, second) { return [...new Set(`${first || ''};${second || ''}`.split(';').filter(Boolean))].join(';'); }
