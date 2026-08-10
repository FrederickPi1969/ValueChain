import { useEffect, useMemo, useRef, useState } from 'react';
import { MultiDirectedGraph } from 'graphology';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import Sigma from 'sigma';
import { Layers3, Network, Radio, ShieldCheck } from 'lucide-react';
import { shortRelation } from '../components/format.js';
import { confirmationStatus } from '../lib/filters.js';

const EDGE_LIMITS = [250, 750, 1_500, 3_000, 5_000];
const FORCE_LAYOUT_NODE_LIMIT = 650;
const RELATION_COLORS = {
  supplies_to: '#38bdf8', supplier_dependency: '#38bdf8', manufacturing_dependency: '#a78bfa', foundry_dependency: '#a78bfa',
  customer_dependency: '#fbbf24', purchases_from: '#fbbf24', cloud_or_hosting_dependency: '#34d399',
  data_center_dependency: '#34d399', power_or_utility_dependency: '#fb7185', network_or_interconnection_dependency: '#f97316',
  distribution_or_channel_dependency: '#e879f9', licensing_dependency: '#60a5fa', strategic_partner: '#22d3ee', co_investment: '#f472b6',
};

export function TopologyMap({ edges = [], networkEdges = [], companies = [], industryExpansion = {} }) {
  const expansionEdges = industryExpansion?.edges || [];
  const sourceEdges = expansionEdges.length ? expansionEdges : (networkEdges.length ? networkEdges : edges);
  const expansionNodes = industryExpansion?.nodes || [];
  const seedOptions = useMemo(() => expansionSeedNames(industryExpansion), [industryExpansion]);
  const [selectedSeeds, setSelectedSeeds] = useState([]);
  const [focus, setFocus] = useState('');
  const [scope, setScope] = useState(expansionEdges.length ? 'expansion' : 'global');
  const [depth, setDepth] = useState(Math.min(2, Number(industryExpansion?.config?.max_hops || 2)));
  const [edgeLimit, setEdgeLimit] = useState(1_500);
  const [showExposure, setShowExposure] = useState(false);
  const [showIndustries, setShowIndustries] = useState(true);
  const [includeCandidates, setIncludeCandidates] = useState(true);
  const [enabledTypes, setEnabledTypes] = useState([]);
  const [selectedEdge, setSelectedEdge] = useState(null);
  const options = useMemo(() => focusOptions(sourceEdges, companies, expansionNodes), [sourceEdges, companies, expansionNodes]);
  const availableTypes = useMemo(() => relationTypes(sourceEdges), [sourceEdges]);

  useEffect(() => {
    const defaults = seedOptions.slice(0, Math.min(6, seedOptions.length));
    setSelectedSeeds(defaults);
    setScope((current) => expansionEdges.length ? 'expansion' : current === 'expansion' ? 'global' : current);
  }, [industryExpansion?.schema_version, industryExpansion?.seed_entity_ids?.join('|')]);
  useEffect(() => {
    if (!focus || !options.includes(focus)) setFocus(options.includes('NVIDIA Corporation') ? 'NVIDIA Corporation' : options[0] || '');
  }, [focus, options]);
  useEffect(() => setEnabledTypes(availableTypes.map((row) => row.type)), [availableTypes]);

  const topology = useMemo(() => buildTopology({
    rows: sourceEdges, companies, expansionNodes, focus, selectedSeeds, scope, depth, edgeLimit,
    showExposure, showIndustries, includeCandidates, enabledTypes,
  }), [sourceEdges, companies, expansionNodes, focus, selectedSeeds, scope, depth, edgeLimit, showExposure, showIndustries, includeCandidates, enabledTypes]);

  if (!sourceEdges.length) return <div className="empty topology-empty">No canonical company relationships are available for this run.</div>;
  return (
    <section className="topology-lab force-topology">
      <div className="topology-toolbar topology-toolbar-large">
        <div className="topology-intro">
          <h2><Network size={18} /> Industry relationship expansion</h2>
          <p>10-K/10-Q facts expand outward from seed companies. Arrow direction is canonical source → target; candidates remain visibly distinct from accepted links.</p>
        </div>
        <label><span>Graph scope</span><select value={scope} onChange={(event) => { setScope(event.target.value); setSelectedEdge(null); }}>
          {expansionEdges.length && <option value="expansion">Seed expansion</option>}<option value="global">All extracted links</option><option value="ego">Single-company ego</option>
        </select></label>
        {scope === 'ego' ? <label><span>Focus company</span><select value={focus} onChange={(event) => setFocus(event.target.value)}>{options.map((name) => <option key={name}>{name}</option>)}</select></label> : <label><span>Expansion radius</span><select value={depth} onChange={(event) => setDepth(Number(event.target.value))}><option value={1}>One hop</option><option value={2}>Two hops</option><option value={3}>Three hops</option></select></label>}
        <label><span>Display budget</span><select value={edgeLimit} onChange={(event) => setEdgeLimit(Number(event.target.value))}>{EDGE_LIMITS.map((limit) => <option key={limit} value={limit}>{limit.toLocaleString()} edges</option>)}</select></label>
        <label className="topology-checkbox"><input type="checkbox" checked={includeCandidates} onChange={(event) => setIncludeCandidates(event.target.checked)} /><span>Include candidates</span></label>
        <label className="topology-checkbox"><input type="checkbox" checked={showIndustries} onChange={(event) => setShowIndustries(event.target.checked)} /><span>Industry anchors</span></label>
        <label className="topology-checkbox"><input type="checkbox" checked={showExposure} onChange={(event) => setShowExposure(event.target.checked)} /><span>Anonymous nodes</span></label>
      </div>
      {scope === 'expansion' && seedOptions.length > 0 && <SeedPicker options={seedOptions} selected={selectedSeeds} onChange={setSelectedSeeds} />}
      <div className="topology-stats">
        <span><b>{topology.companyCount}</b> named companies</span><span><b>{topology.industryCount}</b> industry groups</span><span><b>{topology.displayedEdgeCount}</b> of {topology.availableEdgeCount.toLocaleString()} eligible links</span>
        <span><b>{topology.acceptedCount}</b> accepted</span><span><b>{topology.candidateCount}</b> candidates</span>
        {(scope === 'expansion' || scope === 'ego') && <span><b>+{topology.hopDelta}</b> nodes beyond one hop</span>}
        {topology.omittedEdgeCount > 0 && <span><b>{topology.omittedEdgeCount.toLocaleString()}</b> lower-ranked links hidden by display budget</span>}
        <span><Layers3 size={14} /> {topology.layoutMode === 'force' ? 'ForceAtlas2 detail layout' : 'partitioned large-graph layout'}</span>
        <span>{expansionEdges.length ? <><ShieldCheck size={14} /> filing-grounded expansion artifact</> : <><Radio size={14} /> legacy graph projection</>}</span>
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
          {selectedEdge ? <EdgeDetail edge={selectedEdge} /> : <TopologyLegend expansion={industryExpansion} />}
        </aside>
      </div>
    </section>
  );
}

function SeedPicker({ options, selected, onChange }) {
  const shown = options.slice(0, 18);
  return <div className="seed-picker"><span>Seeds</span>{shown.map((name) => <button type="button" key={name} className={selected.includes(name) ? 'active' : ''} onClick={() => onChange(selected.includes(name) && selected.length > 1 ? selected.filter((item) => item !== name) : selected.includes(name) ? selected : [...selected, name])}>{trim(name, 25)}</button>)}{options.length > shown.length && <small>+{options.length - shown.length} additional seeds in artifact</small>}</div>;
}

function ForceGraph({ topology, onSelect }) {
  const containerRef = useRef(null);
  useEffect(() => {
    if (!containerRef.current || !topology.rows.length) return undefined;
    const graph = new MultiDirectedGraph();
    topology.nodes.forEach((node) => graph.addNode(node.id, node));
    topology.rows.forEach((edge, index) => graph.addDirectedEdgeWithKey(edge.id || `edge-${index}`, edge.objectId, edge.subjectId, {
      label: shortRelation(edge.relation_type), color: edge.isIndustryMembership ? '#475569' : edge.review_status === 'accepted' ? relationColor(edge.relation_type) : `${relationColor(edge.relation_type)}99`,
      size: edge.isIndustryMembership ? 0.4 : Math.min(4, 0.7 + Math.log2(Number(edge.evidence_count || 1) + 1)),
      type: edge.isIndustryMembership ? 'line' : 'arrow', edge,
    }));
    if (topology.layoutMode === 'force') {
      forceAtlas2.assign(graph, { iterations: Math.max(35, Math.min(110, Math.round(500 / Math.max(graph.order, 1)))), settings: { ...forceAtlas2.inferSettings(graph), gravity: 1.4, scalingRatio: 8, slowDown: 2, barnesHutOptimize: graph.order > 180, barnesHutTheta: 0.6 } });
    }
    let hoveredNode = null;
    const renderer = new Sigma(graph, containerRef.current, {
      renderEdgeLabels: topology.rows.length <= 70, enableEdgeEvents: true, zIndex: true, defaultEdgeType: 'arrow',
      labelColor: { color: '#e2e8f0' }, edgeLabelColor: { color: '#cbd5e1' }, labelFont: 'Inter, ui-sans-serif, system-ui',
      labelSize: topology.nodes.length > 1_000 ? 10 : 12, labelWeight: '600', labelRenderedSizeThreshold: topology.nodes.length > 500 ? 14 : 10, stagePadding: 38,
      nodeReducer: (node, data) => {
        if (!hoveredNode) return data;
        const connected = node === hoveredNode || graph.areNeighbors(node, hoveredNode);
        return connected ? { ...data, forceLabel: true } : { ...data, color: '#263548', label: '', zIndex: 0 };
      },
      edgeReducer: (edge, data) => {
        if (!hoveredNode) return data;
        const [source, target] = graph.extremities(edge);
        return source === hoveredNode || target === hoveredNode ? { ...data, size: data.size + 1.1, forceLabel: true } : { ...data, color: '#263548', size: 0.25, zIndex: 0, label: '' };
      },
    });
    renderer.on('enterNode', ({ node }) => { hoveredNode = node; renderer.refresh(); });
    renderer.on('leaveNode', () => { hoveredNode = null; renderer.refresh(); });
    renderer.on('enterEdge', ({ edge }) => onSelect(graph.getEdgeAttribute(edge, 'edge')));
    renderer.on('clickEdge', ({ edge }) => onSelect(graph.getEdgeAttribute(edge, 'edge')));
    renderer.on('clickNode', ({ node }) => {
      const nodeEdges = graph.edges(node).map((edge) => graph.getEdgeAttribute(edge, 'edge')).filter((edge) => !edge.isIndustryMembership);
      if (nodeEdges.length) onSelect(nodeEdges.sort(edgeRank)[0]);
    });
    return () => renderer.kill();
  }, [topology, onSelect]);
  return <div className="topology-canvas sigma-canvas" ref={containerRef} />;
}

function EdgeDetail({ edge }) {
  return <div className="topology-edge-detail"><strong>{edge.object} <span>→</span> {edge.subject}</strong><dl>
    <div><dt>Relation</dt><dd><i className="relation-dot" style={{ background: relationColor(edge.relation_type) }} /> {shortRelation(edge.relation_type)}</dd></div>
    <div><dt>Layer</dt><dd>{edge.review_status === 'accepted' ? 'accepted / verified' : 'candidate — requires review'}</dd></div>
    <div><dt>Hop</dt><dd>{edge.expansion_depth ?? 'not recorded'}</dd></div><div><dt>Modality</dt><dd>{edge.modality || 'not recorded'}</dd></div>
    <div><dt>Evidence</dt><dd>{edge.evidence_count || 1} passage{Number(edge.evidence_count || 1) === 1 ? '' : 's'}; {(edge.source_accession_numbers || []).length} filing accession(s)</dd></div>
    <div><dt>Status</dt><dd>{confirmationStatus(edge)}</dd></div>{edge.product_or_service && <div><dt>Product</dt><dd>{edge.product_or_service}</dd></div>}{edge.risk_flags?.length > 0 && <div><dt>Audit flags</dt><dd>{edge.risk_flags.join(', ')}</dd></div>}
  </dl>{edge.source_urls && <a href={String(edge.source_urls).split(';')[0]} target="_blank" rel="noreferrer">Open supporting filing</a>}</div>;
}

function TopologyLegend({ expansion }) {
  const summary = expansion?.summary || {};
  return <div className="topology-legend"><p><b>Level of detail:</b> seed companies, selected nodes, industry anchors and high-degree hubs keep labels. Hovering a node isolates its neighborhood, so a large graph stays readable.</p><p><b>Layout:</b> smaller subgraphs use ForceAtlas2. Above {FORCE_LAYOUT_NODE_LIMIT} nodes, deterministic industry partitions avoid a blocking force simulation and keep repeated renders stable.</p><p><b>Trust:</b> solid/high-opacity links are accepted; translucent links are candidates. A candidate is evidence-backed but not yet verified.</p>{summary.node_cap_reached && <p><b>Expansion cap reached:</b> generate a larger artifact or choose fewer seeds to continue the frontier.</p>}</div>;
}

export function buildTopology({ rows, companies = [], expansionNodes = [], focus = '', selectedSeeds = [], scope = 'global', depth = 2, edgeLimit = 1500, showExposure = false, showIndustries = true, includeCandidates = true, enabledTypes = [] }) {
  const issuers = new Set([...companies.map((row) => row.company), ...expansionNodes.filter((row) => row.is_universe_company).map((row) => row.canonical_name)].filter(Boolean));
  const allowed = new Set(enabledTypes);
  const nodeMetadata = new Map(expansionNodes.map((row) => [row.canonical_name, row]));
  const normalized = collapseRows(rows, issuers, showExposure).filter((edge) => allowed.has(edge.relation_type) && (includeCandidates || edge.review_status === 'accepted'));
  const seeds = scope === 'ego' ? [focus] : selectedSeeds;
  const oneHop = scope === 'global' ? normalized : selectMultiEgo(normalized, seeds, 1);
  const scoped = scope === 'global' ? normalized : selectMultiEgo(normalized, seeds, depth);
  const ranked = [...scoped].sort(edgeRank);
  const ego = ranked.slice(0, edgeLimit);
  const visibleNames = new Set(ego.flatMap((edge) => [edge.object, edge.subject]));
  seeds.forEach((name) => name && visibleNames.add(name));
  const visibleCompanies = [...visibleNames].map((name) => ({ company: name, role: nodeMetadata.get(name)?.role || companies.find((row) => row.company === name)?.role || '' }));
  const industryMemberships = showIndustries ? buildIndustryMemberships(visibleCompanies) : [];
  const names = new Set(visibleNames); industryMemberships.forEach((edge) => { names.add(edge.object); names.add(edge.subject); });
  const stats = new Map([...names].map((name) => [name, { degree: 0, evidence: 0 }]));
  ego.forEach((edge) => { const weight = Number(edge.evidence_count || 1); [edge.object, edge.subject].forEach((name) => { const stat = stats.get(name); stat.degree += 1; stat.evidence += weight; }); });
  const seedSet = new Set(seeds);
  const groupByName = new Map(visibleCompanies.map((row) => [row.company, industryGroup(row.role)]));
  const layoutMode = names.size <= FORCE_LAYOUT_NODE_LIMIT ? 'force' : 'partitioned';
  const labelDegree = names.size > 1_000 ? 15 : names.size > 350 ? 8 : 4;
  const partitionPositions = buildPartitionPositions([...names], groupByName);
  const nodes = [...names].map((name) => {
    const stat = stats.get(name) || { degree: 0, evidence: 0 }; const kind = nodeKind(name, issuers);
    const position = partitionPositions.get(name);
    return { id: nodeId(name), label: trim(name, 34), x: position.x, y: position.y, size: kind === 'industry' ? 17 : 4 + Math.min(14, Math.sqrt(stat.degree) * 2.6), color: nodeColor(kind, seedSet.has(name)), forceLabel: kind === 'industry' || seedSet.has(name) || name === focus || stat.degree >= labelDegree, zIndex: seedSet.has(name) ? 4 : kind === 'industry' ? 3 : 1, fixed: kind === 'industry', kind };
  });
  const nodeByName = new Map(nodes.map((node) => [node.id.slice(5), node.id]));
  const graphRows = [...ego, ...industryMemberships].map((edge, index) => ({ ...edge, id: `rel-${index}-${nodeId(edge.object)}-${nodeId(edge.subject)}-${edge.relation_type}`, objectId: nodeByName.get(encodeURIComponent(edge.object)), subjectId: nodeByName.get(encodeURIComponent(edge.subject)) })).filter((edge) => edge.objectId && edge.subjectId);
  const oneHopNodes = new Set([...seeds, ...oneHop.flatMap((edge) => [edge.object, edge.subject])]);
  return { rows: graphRows, nodes, layoutMode, displayedEdgeCount: ego.length, availableEdgeCount: scoped.length, omittedEdgeCount: Math.max(0, scoped.length - ego.length), companyCount: nodes.filter((node) => node.kind === 'issuer' || node.kind === 'counterparty').length, industryCount: nodes.filter((node) => node.kind === 'industry').length, exposureCount: nodes.filter((node) => node.kind === 'exposure').length, hopDelta: Math.max(0, visibleNames.size - oneHopNodes.size), acceptedCount: ego.filter((row) => row.review_status === 'accepted').length, candidateCount: ego.filter((row) => row.review_status !== 'accepted').length };
}

function buildIndustryMemberships(companies) { const seen = new Set(); return companies.filter((row) => row.company).map((row) => ({ object: `Industry · ${industryGroup(row.role)}`, subject: row.company, relation_type: 'industry_membership', modality: 'taxonomy', evidence_count: 0, review_status: 'taxonomy', isIndustryMembership: true })).filter((row) => { const key = `${row.object}|${row.subject}`; if (seen.has(key)) return false; seen.add(key); return true; }); }
function industryGroup(role) { const value = String(role || '').toLowerCase(); if (/(power|cooling|generation|grid)/.test(value)) return 'Power & thermal'; if (/(data.?center|colocation|server|network|optical|edge_cloud)/.test(value)) return 'Data-center infrastructure'; if (/(foundry|semiconductor|accelerator|memory|semicap|chip|compute)/.test(value)) return 'Semiconductors & compute'; if (/(cloud|hyperscaler|database|platform)/.test(value)) return 'Cloud & platforms'; if (/(software|observability|ai_software)/.test(value)) return 'AI software'; return 'External / unclassified'; }
function buildPartitionPositions(names, groupByName) {
  const groups = ['Semiconductors & compute', 'Cloud & platforms', 'Data-center infrastructure', 'Power & thermal', 'AI software', 'External / unclassified'];
  const buckets = new Map(groups.map((group) => [group, []]));
  names.filter((name) => !name.startsWith('Industry · ')).forEach((name) => buckets.get(groupByName.get(name) || 'External / unclassified').push(name));
  const positions = new Map(); const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  groups.forEach((group, groupIndex) => {
    const angle = -Math.PI / 2 + (Math.PI * 2 * groupIndex) / groups.length;
    const center = { x: Math.cos(angle) * 1.65, y: Math.sin(angle) * 1.65 };
    positions.set(`Industry · ${group}`, { x: Math.cos(angle) * 2.25, y: Math.sin(angle) * 2.25 });
    const members = buckets.get(group).sort(); const radius = .3 + Math.min(.9, Math.sqrt(members.length) * .028);
    members.forEach((name, index) => {
      const localRadius = radius * Math.sqrt((index + .55) / Math.max(members.length, 1));
      const localAngle = goldenAngle * index + angle + centeredCoordinate(name, 1) * .05;
      positions.set(name, { x: center.x + Math.cos(localAngle) * localRadius, y: center.y + Math.sin(localAngle) * localRadius });
    });
  });
  return positions;
}

function collapseRows(rows, issuers, showExposure) { const index = new Map(); rows.forEach((row) => { const object = String(row.object || row.source || '').trim(); const subject = String(row.subject || row.target || '').trim(); if (!object || !subject || object === subject || (!showExposure && (nodeKind(object, issuers) === 'exposure' || nodeKind(subject, issuers) === 'exposure'))) return; const relation_type = String(row.relation_type || row.relationship_type || 'dependency'); const modality = String(row.modality || 'not_recorded'); const key = `${object}|${subject}|${relation_type}|${modality}`; const prior = index.get(key); const current = { ...row, object, subject, relation_type, modality, evidence_count: Number(row.evidence_count || 1), review_status: row.review_status || (row.confirmation_status === 'confirmed' ? 'accepted' : 'unreviewed') }; index.set(key, prior ? { ...prior, evidence_count: Number(prior.evidence_count || 0) + current.evidence_count, review_status: prior.review_status === 'accepted' || current.review_status === 'accepted' ? 'accepted' : current.review_status, source_urls: joinValues(prior.source_urls, current.source_urls), source_accession_numbers: [...new Set([...(prior.source_accession_numbers || []), ...(current.source_accession_numbers || [])])] } : current); }); return [...index.values()]; }
export function selectMultiEgo(rows, seeds, depth) { const activeSeeds = seeds.filter(Boolean); if (!activeSeeds.length) return []; const distance = new Map(activeSeeds.map((name) => [name, 0])); let frontier = new Set(activeSeeds); for (let hop = 1; hop <= depth; hop += 1) { const next = new Set(); rows.forEach((edge) => { if (frontier.has(edge.object) && !distance.has(edge.subject)) next.add(edge.subject); if (frontier.has(edge.subject) && !distance.has(edge.object)) next.add(edge.object); }); next.forEach((name) => distance.set(name, hop)); frontier = next; if (!frontier.size) break; } return rows.filter((edge) => distance.has(edge.object) && distance.has(edge.subject)); }
function relationTypes(rows) { const counts = new Map(); rows.forEach((row) => { const type = String(row.relation_type || row.relationship_type || 'dependency'); counts.set(type, (counts.get(type) || 0) + 1); }); return [...counts].map(([type, count]) => ({ type, count })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type)); }
function focusOptions(rows, companies, expansionNodes) { const names = new Set([...companies.map((row) => row.company), ...expansionNodes.map((row) => row.canonical_name)]); rows.forEach((edge) => { names.add(edge.subject || edge.target); names.add(edge.object || edge.source); }); return [...names].filter((name) => name && !isAnonymousOrLowQuality(name)).sort((a, b) => a.localeCompare(b)); }
function expansionSeedNames(expansion) { const ids = new Set(expansion?.seed_entity_ids || []); return (expansion?.nodes || []).filter((row) => row.is_seed || ids.has(row.entity_id)).map((row) => row.canonical_name).filter(Boolean); }
function nodeKind(name, issuers) { if (String(name).startsWith('Industry · ')) return 'industry'; if (issuers.has(name)) return 'issuer'; return isAnonymousOrLowQuality(name) ? 'exposure' : 'counterparty'; }
function isAnonymousOrLowQuality(name) { const value = String(name || '').trim(); return /^(?:(?:direct|major|large|significant)\s+)?(?:customer|customers|supplier|suppliers|vendor|vendors|distributor|distributors|partner|partners)\s+(?:[a-z](?:\s+(?:and|or)\s+[a-z])?|[0-9]+)$/i.test(value) || /\b(class|dependency class|capacity class)\b/i.test(value) || /^(contents?|table of contents)\b/i.test(value); }
function edgeRank(a, b) { return (a.review_status === 'accepted' ? 0 : 1) - (b.review_status === 'accepted' ? 0 : 1) || Number(b.evidence_count || 0) - Number(a.evidence_count || 0) || Number(b.confidence || b.avg_confidence || 0) - Number(a.confidence || a.avg_confidence || 0) || String(a.relationship_id || '').localeCompare(String(b.relationship_id || '')); }
function relationColor(type) { return RELATION_COLORS[type] || '#94a3b8'; }
function nodeColor(kind, seed) { if (seed) return '#f472b6'; return { issuer: '#2dd4bf', counterparty: '#60a5fa', exposure: '#fbbf24', industry: '#c084fc' }[kind] || '#94a3b8'; }
function nodeId(value) { return `node:${encodeURIComponent(value)}`; }
function centeredCoordinate(value, salt) { let hash = 2166136261 + salt; for (let i = 0; i < value.length; i += 1) { hash ^= value.charCodeAt(i); hash = Math.imul(hash, 16777619); } return (((hash >>> 0) % 2001) - 1000) / 1000; }
function trim(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }
function joinValues(first, second) { return [...new Set(`${first || ''};${second || ''}`.split(';').filter(Boolean))].join(';'); }
