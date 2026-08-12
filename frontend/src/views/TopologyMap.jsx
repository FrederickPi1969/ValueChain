import { useEffect, useMemo, useRef, useState } from 'react';
import { MultiDirectedGraph } from 'graphology';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import Sigma from 'sigma';
import { Building2, Layers3, Network, Radio, ShieldCheck, X } from 'lucide-react';
import { shortRelation } from '../components/format.js';
import { confirmationStatus } from '../lib/filters.js';
import { challengeRelationship } from '../api/data.js';

const EDGE_LIMITS = [250, 750, 1_500, 3_000, 5_000];
const FORCE_LAYOUT_NODE_LIMIT = 650;
const RELATION_COLORS = {
  supplies_to: '#38bdf8', supplier_dependency: '#38bdf8', manufacturing_dependency: '#a78bfa', foundry_dependency: '#a78bfa',
  customer_dependency: '#fbbf24', purchases_from: '#fbbf24', cloud_or_hosting_dependency: '#34d399',
  data_center_dependency: '#34d399', power_or_utility_dependency: '#fb7185', network_or_interconnection_dependency: '#f97316',
  distribution_or_channel_dependency: '#e879f9', licensing_dependency: '#60a5fa', strategic_partner: '#22d3ee', co_investment: '#f472b6',
};

export function TopologyMap({ runId = '', edges = [], networkEdges = [], companies = [], evidence = [], lineageEvents = [], industryExpansion = {} }) {
  const expansionEdges = industryExpansion?.edges || [];
  const sourceEdges = expansionEdges.length ? expansionEdges : (networkEdges.length ? networkEdges : edges);
  const expansionNodes = industryExpansion?.nodes || [];
  const seedOptions = useMemo(() => expansionSeedNames(industryExpansion), [industryExpansion]);
  const [selectedSeeds, setSelectedSeeds] = useState([]);
  const [scope, setScope] = useState(expansionEdges.length ? 'expansion' : 'global');
  const [depth, setDepth] = useState(Math.min(2, Number(industryExpansion?.config?.max_hops || 2)));
  const [edgeLimit, setEdgeLimit] = useState(1_500);
  const [showExposure, setShowExposure] = useState(false);
  const [showIndustries, setShowIndustries] = useState(true);
  const [showContext, setShowContext] = useState(true);
  const [includeCandidates, setIncludeCandidates] = useState(true);
  const [enabledTypes, setEnabledTypes] = useState([]);
  const [enabledFamilies, setEnabledFamilies] = useState(['supply_chain']);
  const [anchors, setAnchors] = useState([]);
  const [activeNode, setActiveNode] = useState('');
  const [selectedEdge, setSelectedEdge] = useState(null);
  const availableTypes = useMemo(() => relationTypes(sourceEdges), [sourceEdges]);
  const availableFamilies = useMemo(() => relationFamilies(sourceEdges), [sourceEdges]);

  useEffect(() => {
    const defaults = seedOptions.slice(0, Math.min(6, seedOptions.length));
    setSelectedSeeds(defaults);
    setScope((current) => expansionEdges.length ? 'expansion' : current === 'expansion' ? 'global' : current);
  }, [industryExpansion?.schema_version, industryExpansion?.seed_entity_ids?.join('|')]);
  useEffect(() => setEnabledTypes(availableTypes.map((row) => row.type)), [availableTypes]);
  useEffect(() => setEnabledFamilies((current) => current.filter((family) => availableFamilies.includes(family)).length ? current.filter((family) => availableFamilies.includes(family)) : availableFamilies.includes('supply_chain') ? ['supply_chain'] : availableFamilies), [availableFamilies]);

  const topology = useMemo(() => buildTopology({
    rows: sourceEdges, companies, expansionNodes, focus: activeNode, anchors, selectedSeeds, scope, depth, edgeLimit,
    showExposure, showIndustries, showContext, includeCandidates, enabledTypes, enabledFamilies,
  }), [sourceEdges, companies, expansionNodes, activeNode, anchors, selectedSeeds, scope, depth, edgeLimit, showExposure, showIndustries, showContext, includeCandidates, enabledTypes, enabledFamilies]);

  const toggleAnchor = (name) => {
    setAnchors((current) => {
      const next = current.includes(name) ? current.filter((item) => item !== name) : [...current, name];
      setActiveNode((active) => active === name ? (next.at(-1) || '') : name);
      return next;
    });
    setSelectedEdge(null);
  };
  const removeAnchor = (name) => {
    setAnchors((current) => {
      const next = current.filter((item) => item !== name);
      setActiveNode((active) => active === name ? (next.at(-1) || '') : active);
      return next;
    });
  };
  const selectEdge = (edge) => { setSelectedEdge(edge); setActiveNode(''); };
  const inspectCompany = (name) => { setActiveNode(name); setSelectedEdge(null); };

  if (!sourceEdges.length) return <div className="empty topology-empty">No canonical company relationships are available for this run.</div>;
  return (
    <section className="topology-lab force-topology">
      <div className="topology-toolbar topology-toolbar-large">
        <div className="topology-intro">
          <h2><Network size={18} /> Industry relationship expansion</h2>
          <p>10-K/10-Q facts expand outward from seed companies. Arrow direction is canonical source → target; candidates remain visibly distinct from accepted links.</p>
        </div>
        <label><span>Context scope</span><select value={scope} onChange={(event) => { setScope(event.target.value); setSelectedEdge(null); }}>
          {expansionEdges.length && <option value="expansion">Seed expansion</option>}<option value="global">All extracted links</option>
        </select></label>
        {scope === 'expansion' ? <label><span>Expansion radius</span><select value={depth} onChange={(event) => setDepth(Number(event.target.value))}><option value={1}>One hop</option><option value={2}>Two hops</option><option value={3}>Three hops</option></select></label> : <div className="topology-scope-note">Click companies to build a research set</div>}
        <label><span>Display budget</span><select value={edgeLimit} onChange={(event) => setEdgeLimit(Number(event.target.value))}>{EDGE_LIMITS.map((limit) => <option key={limit} value={limit}>{limit.toLocaleString()} edges</option>)}</select></label>
        <label className="topology-checkbox"><input type="checkbox" checked={includeCandidates} onChange={(event) => setIncludeCandidates(event.target.checked)} /><span>Include candidates</span></label>
        <label className="topology-checkbox"><input type="checkbox" checked={showContext} onChange={(event) => setShowContext(event.target.checked)} /><span>Dim global context</span></label>
        <label className="topology-checkbox"><input type="checkbox" checked={showIndustries} onChange={(event) => setShowIndustries(event.target.checked)} /><span>Industry anchors</span></label>
        <label className="topology-checkbox"><input type="checkbox" checked={showExposure} onChange={(event) => setShowExposure(event.target.checked)} /><span>Anonymous nodes</span></label>
      </div>
      {scope === 'expansion' && seedOptions.length > 0 && <SeedPicker options={seedOptions} selected={selectedSeeds} onChange={setSelectedSeeds} />}
      <AnchorBar anchors={anchors} onRemove={removeAnchor} onClear={() => { setAnchors([]); setActiveNode(''); setSelectedEdge(null); }} />
      <div className="topology-layer-picker" aria-label="Relationship layers">
        <span>Layers</span>{availableFamilies.map((family) => <label className="topology-checkbox" key={family}><input type="checkbox" checked={enabledFamilies.includes(family)} onChange={() => setEnabledFamilies((current) => current.includes(family) ? current.filter((item) => item !== family) : [...current, family])} /><span>{familyLabel(family)}</span></label>)}
      </div>
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
      <div className="topology-node-legend" aria-label="Node and edge meaning"><span><i className="node-anchor" />Research-set company</span><span><i className="node-inspected" />Viewing in side panel</span><span><i className="node-seed" />Expansion seed</span><span><i className="node-issuer" />Universe company</span><span><i className="node-counterparty" />External named company</span><span><i className="node-industry" />Industry group</span><span><i className="edge-focus" />One-hop focus link</span><span><i className="edge-context" />Muted context</span></div>
      <div className="topology-layout">
        <ForceGraph topology={topology} onSelect={selectEdge} onToggleAnchor={toggleAnchor} />
        <aside className="topology-detail panel">
          <div className="panel-head"><h2>{activeNode ? 'Company research' : selectedEdge ? 'Selected relationship' : 'Reading the graph'}</h2></div>
          {activeNode ? <CompanyDetail name={activeNode} topology={topology} anchors={anchors} onToggleAnchor={toggleAnchor} onInspectCompany={inspectCompany} onSelectEdge={selectEdge} /> : selectedEdge ? <EdgeDetail runId={runId} edge={selectedEdge} evidence={evidence} lineage={lineageEvents.filter((event) => event.relationship_id === selectedEdge.relationship_id)} /> : <TopologyLegend expansion={industryExpansion} />}
        </aside>
      </div>
    </section>
  );
}

function AnchorBar({ anchors, onRemove, onClear }) {
  if (!anchors.length) return <div className="anchor-bar muted"><Building2 size={15} /> Click a company to start a research set. Its one-hop relationships will become prominent while global context stays muted.</div>;
  return <div className="anchor-bar"><span>Research set</span>{anchors.map((name) => <button type="button" key={name} onClick={() => onRemove(name)}>{trim(name, 28)} <X size={13} /></button>)}<button type="button" className="anchor-clear" onClick={onClear}>Clear all</button></div>;
}

function SeedPicker({ options, selected, onChange }) {
  const shown = options.slice(0, 18);
  return <div className="seed-picker"><span>Seeds</span>{shown.map((name) => <button type="button" key={name} className={selected.includes(name) ? 'active' : ''} onClick={() => onChange(selected.includes(name) && selected.length > 1 ? selected.filter((item) => item !== name) : selected.includes(name) ? selected : [...selected, name])}>{trim(name, 25)}</button>)}{options.length > shown.length && <small>+{options.length - shown.length} additional seeds in artifact</small>}</div>;
}

function ForceGraph({ topology, onSelect, onToggleAnchor }) {
  const containerRef = useRef(null);
  const [hoveredEdge, setHoveredEdge] = useState(null);
  useEffect(() => {
    if (!containerRef.current || !topology.rows.length) return undefined;
    const graph = new MultiDirectedGraph();
    topology.nodes.forEach((node) => graph.addNode(node.id, node));
    topology.rows.forEach((edge, index) => graph.addDirectedEdgeWithKey(edge.id || `edge-${index}`, edge.objectId, edge.subjectId, {
      label: edge.isFocused ? shortRelation(edge.relation_type) : '', color: edge.isIndustryMembership ? '#475569' : edge.isContext ? '#263548' : edge.review_status === 'accepted' ? relationColor(edge.relation_type) : `${relationColor(edge.relation_type)}99`,
      size: edge.isIndustryMembership ? 0.4 : edge.isAnchorLink ? Math.min(5.5, 1.8 + Math.log2(Number(edge.evidence_count || 1) + 1)) : edge.isContext ? 0.22 : Math.min(4, 0.7 + Math.log2(Number(edge.evidence_count || 1) + 1)),
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
        // Context nodes intentionally omit labels at rest. Reveal the exact
        // company name under the pointer before a user decides to add it to
        // the research set, even when it is disconnected from existing anchors.
        if (node === hoveredNode) return { ...data, label: trim(decodeNodeName(node), 34), forceLabel: true, color: data.contextOnly ? '#94a3b8' : data.color, size: Math.max(data.size, 4), zIndex: 7 };
        const connected = node === hoveredNode || graph.areNeighbors(node, hoveredNode);
        return connected ? { ...data, forceLabel: true } : { ...data, color: '#263548', label: '', zIndex: 0, size: Math.min(data.size, 2) };
      },
      edgeReducer: (edge, data) => {
        if (!hoveredNode) return data;
        const [source, target] = graph.extremities(edge);
        return source === hoveredNode || target === hoveredNode ? { ...data, size: data.size + 1.1, forceLabel: true } : { ...data, color: '#263548', size: 0.25, zIndex: 0, label: '' };
      },
    });
    renderer.on('enterNode', ({ node }) => { hoveredNode = node; renderer.refresh(); });
    renderer.on('leaveNode', () => { hoveredNode = null; renderer.refresh(); });
    // Hover is deliberately visual-only: it previews a short relationship
    // tooltip and never replaces the active company panel.
    renderer.on('enterEdge', ({ edge, event }) => {
      const rect = containerRef.current?.getBoundingClientRect();
      const pointerX = Number(event?.x); const pointerY = Number(event?.y);
      const x = rect ? (pointerX >= 0 && pointerX <= rect.width ? pointerX : pointerX - rect.left) : 0;
      const y = rect ? (pointerY >= 0 && pointerY <= rect.height ? pointerY : pointerY - rect.top) : 0;
      setHoveredEdge({ edge: graph.getEdgeAttribute(edge, 'edge'), x, y });
    });
    renderer.on('leaveEdge', () => setHoveredEdge(null));
    renderer.on('clickEdge', ({ edge }) => onSelect(graph.getEdgeAttribute(edge, 'edge')));
    renderer.on('clickNode', ({ node }) => {
      const name = decodeNodeName(node);
      if (!name.startsWith('Industry · ')) onToggleAnchor(name);
    });
    return () => renderer.kill();
  }, [topology, onSelect, onToggleAnchor]);
  return <div className="topology-canvas sigma-canvas"><div className="sigma-renderer" ref={containerRef} />{hoveredEdge && <EdgeTooltip {...hoveredEdge} />}</div>;
}

function EdgeTooltip({ edge, x, y }) {
  const relation = edge.isIndustryMembership ? 'industry membership' : shortRelation(edge.relation_type);
  return <div className="topology-edge-tooltip" style={{ left: `${Math.max(12, x + 12)}px`, top: `${Math.max(12, y + 12)}px` }} role="tooltip"><b>{trim(edge.object, 32)} → {trim(edge.subject, 32)}</b><span>{relation}</span></div>;
}

function EdgeDetail({ runId, edge, evidence, lineage }) {
  const supportingEvidence = evidenceForRelationship(edge, evidence);
  const audit = edge.llm_audit || {};
  return <div className="topology-edge-detail"><strong>{edge.object} <span>→</span> {edge.subject}</strong><dl>
    <div><dt>Relation</dt><dd><i className="relation-dot" style={{ background: relationColor(edge.relation_type) }} /> {shortRelation(edge.relation_type)}</dd></div>
    <div><dt>Layer</dt><dd>{edge.review_status === 'accepted' ? 'accepted / verified' : 'candidate — requires review'}</dd></div>
    <div><dt>Hop</dt><dd>{edge.expansion_depth ?? 'not recorded'}</dd></div><div><dt>Modality</dt><dd>{edge.modality || 'not recorded'}</dd></div>
    <div><dt>Evidence</dt><dd>{edge.evidence_count || 1} passage{Number(edge.evidence_count || 1) === 1 ? '' : 's'}; {listValue(edge.source_accession_numbers || edge.accessions).length} filing accession(s)</dd></div>
    <div><dt>Status</dt><dd>{confirmationStatus(edge)}</dd></div>{edge.product_or_service && <div><dt>Product</dt><dd>{edge.product_or_service}</dd></div>}{edge.risk_flags?.length > 0 && <div><dt>Audit flags</dt><dd>{edge.risk_flags.join(', ')}</dd></div>}
  </dl>
  <EvidenceDrilldown evidence={supportingEvidence} expectedCount={edge.evidence_count} />
  <RelationshipAssistant runId={runId} edge={edge} evidenceAvailable={supportingEvidence.length > 0} />
  <AuditDrilldown audit={audit} lineage={lineage} decisionReason={edge.decision_reason} decisionSource={edge.decision_source} />
  {!supportingEvidence.length && edge.source_urls && <a href={String(edge.source_urls).split(';')[0]} target="_blank" rel="noreferrer">Open supporting filing</a>}</div>;
}

function RelationshipAssistant({ runId, edge, evidenceAvailable }) {
  const [question, setQuestion] = useState('Does this connection match the evidence?');
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  if (!edge.relationship_id) return null;
  const ask = async (value = question) => {
    setLoading(true); setError('');
    try { const response = await challengeRelationship(runId, edge.relationship_id, value); setResult(response.response); }
    catch (err) { setError('The connection assistant needs the local API and the saved evidence corpus.'); }
    finally { setLoading(false); }
  };
  return <details className="topology-drilldown relationship-assistant"><summary>Ask about this connection <small>evidence-only AI</small></summary><div className="drilldown-body">{!evidenceAvailable ? <p className="muted">Unavailable for this run: its saved graph contains relationship references but not the underlying SEC passages. Select a run with evidence drill-down to ask the assistant.</p> : <><div className="challenge-prompts"><button type="button" onClick={() => { setQuestion('Does this connection match the evidence?'); ask('Does this connection match the evidence?'); }}>Confirm connection</button><button type="button" onClick={() => { setQuestion('Could this be a competitor or market reference instead?'); ask('Could this be a competitor or market reference instead?'); }}>Could it be competition?</button><button type="button" onClick={() => { setQuestion('Is the displayed direction supported by the evidence?'); ask('Is the displayed direction supported by the evidence?'); }}>Check direction</button></div><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask a short question about this connection" rows={2} /><button type="button" className="compact-button" disabled={loading} onClick={() => ask()}>{loading ? 'Checking evidence…' : 'Ask AI'}</button>{error && <p className="challenge-error">{error}</p>}{result && <article className={`challenge-answer ${result.assessment}`}><b>{({ supported: 'Evidence supports this connection', concern: 'Possible issue — marked for re-audit', inconclusive: 'Evidence is not conclusive' })[result.assessment]}</b><p>{result.answer}</p>{result.evidence_quote && <blockquote>“{result.evidence_quote}”</blockquote>}{result.needs_reaudit && <small>This did not change the graph. It was recorded for a later re-audit{result.re_audit_reason ? `: ${result.re_audit_reason}` : '.'}</small>}</article>}</>}</div></details>;
}

function EvidenceDrilldown({ evidence, expectedCount }) {
  return <details className="topology-drilldown" open={evidence.length === 1}><summary>Evidence drill-down <small>{evidence.length ? `${evidence.length} loaded` : `${expectedCount || 0} linked`}</small></summary><div className="drilldown-body">
    {evidence.length ? evidence.slice(0, 6).map((item, index) => <article key={item.evidence_id || item.passage_id || index} className="evidence-card"><div><b>{item.form || item.source_document_type || 'SEC filing'}</b>{item.filing_date && ` · ${item.filing_date}`}{item.source_section && ` · ${item.source_section}`}</div>{item.evidence_quote && <blockquote>“{item.evidence_quote}”</blockquote>}<p>{item.evidence_text || 'No passage text was retained for this evidence record.'}</p><footer><span>{item.accession_number || 'Accession not recorded'}{Number.isFinite(Number(item.paragraph_offset)) ? ` · paragraph ${Number(item.paragraph_offset) + 1}` : ''}</span>{item.source_document_url && <a href={item.source_document_url} target="_blank" rel="noreferrer">Open SEC filing ↗</a>}</footer></article>) : <p className="muted">This saved graph has relationship-level provenance, but not the underlying passage text. Rebuild or serve the run through the API to load its evidence corpus.</p>}</div></details>;
}

function AuditDrilldown({ audit, lineage, decisionReason, decisionSource }) {
  const history = [...(audit.decision_history || [])].reverse();
  const reason = audit.reason || decisionReason;
  if (!reason && !history.length && !lineage.length) return null;
  return <details className="topology-drilldown"><summary>Audit & lineage <small>{audit.decision || decisionSource || 'recorded'}</small></summary><div className="drilldown-body audit-body">{reason && <p><b>Current conclusion:</b> {reason}</p>}{audit.evidence_quote && <blockquote>“{audit.evidence_quote}”</blockquote>}{history.map((event, index) => <p key={`${event.reviewed_at || index}`}><b>{event.decision}</b> · {event.reason || event.follow_up_reason || 'No reason recorded'}</p>)}{lineage.map((event) => <p key={event.event_id}><b>{event.stage}</b> · {event.decision_source || event.actor || 'system'}{event.created_at ? ` · ${new Date(event.created_at).toLocaleString()}` : ''}<br /><span className="muted">{event.before_state?.review_status || 'new'} → {event.after_state?.review_status || 'current'}</span></p>)}</div></details>;
}

function CompanyDetail({ name, topology, anchors, onToggleAnchor, onInspectCompany, onSelectEdge }) {
  const profile = buildNodeProfile(topology.relationshipRows, name);
  const isAnchor = anchors.includes(name);
  return <div className="topology-company-detail">
    <div className="company-detail-title"><div><strong>{name}</strong><small>{isAnchor ? 'Pinned research anchor' : 'Context company'}</small></div><button type="button" onClick={() => onToggleAnchor(name)}>{isAnchor ? 'Remove from set' : 'Add to set'}</button></div>
    <div className="company-detail-stats"><span><b>{profile.accepted}</b> confirmed</span><span><b>{profile.candidate}</b> candidate</span><span><b>{profile.filingCount}</b> source filings</span></div>
    <RelationshipList title="Upstream / incoming" rows={profile.incoming} counterpart={(edge) => edge.object} empty="No visible incoming relationships." onInspectCompany={onInspectCompany} onSelect={onSelectEdge} />
    <RelationshipList title="Downstream / outgoing" rows={profile.outgoing} counterpart={(edge) => edge.subject} empty="No visible outgoing relationships." onInspectCompany={onInspectCompany} onSelect={onSelectEdge} />
    {profile.products.length > 0 && <section className="company-detail-section"><h3>Products / services</h3><div className="product-list">{profile.products.map((value) => <span key={value}>{value}</span>)}</div></section>}
    <section className="company-detail-section"><h3>Evidence status</h3><p>{profile.crossFiled} cross-file verified relationship{profile.crossFiled === 1 ? '' : 's'}; {profile.evidenceCount} supporting passage{profile.evidenceCount === 1 ? '' : 's'} across the visible relationships.</p></section>
    {(profile.parent || profile.subsidiaries.length > 0) && <section className="company-detail-section ownership"><h3>Ownership / control</h3>{profile.parent && <p><b>Parent:</b> <button type="button" className="text-button" onClick={() => onInspectCompany(profile.parent.object)}>{profile.parent.object}</button></p>}{profile.subsidiaries.length > 0 && <details><summary>{profile.subsidiaries.length} direct subsidiary relationship{profile.subsidiaries.length === 1 ? '' : 's'}</summary><ul>{profile.subsidiaries.slice(0, 12).map((edge) => <li key={relationshipRowKey(edge)}><button type="button" className="text-button" onClick={() => onInspectCompany(edge.subject)}>{edge.subject}</button></li>)}</ul></details>}</section>}
  </div>;
}

function RelationshipList({ title, rows, counterpart, empty, onInspectCompany, onSelect }) {
  return <section className="company-detail-section"><h3>{title}</h3>{rows.length ? <ul className="company-relationship-list">{rows.slice(0, 6).map((edge) => { const company = counterpart(edge); return <li key={relationshipRowKey(edge)}><span className={edge.review_status === 'accepted' ? 'status-accepted' : 'status-candidate'}>{edge.review_status === 'accepted' ? '✓' : '○'}</span><button type="button" className="relationship-company" onClick={() => onInspectCompany(company)} title={`Open ${company} company research`}><b>{company}</b><small>{edge.product_or_service || 'Product not specified'}</small></button><button type="button" className="relationship-detail" onClick={() => onSelect(edge)} title={`Open ${shortRelation(edge.relation_type)} relationship details`}><span>→</span>{shortRelation(edge.relation_type)}</button></li>; })}</ul> : <p>{empty}</p>}</section>;
}

function TopologyLegend({ expansion }) {
  const summary = expansion?.summary || {};
  return <div className="topology-legend"><p><b>Level of detail:</b> seed companies, selected nodes, industry anchors and high-degree hubs keep labels. Hovering a node isolates its neighborhood, so a large graph stays readable.</p><p><b>Layout:</b> smaller subgraphs use ForceAtlas2. Above {FORCE_LAYOUT_NODE_LIMIT} nodes, deterministic industry partitions avoid a blocking force simulation and keep repeated renders stable.</p><p><b>Trust:</b> solid/high-opacity links are accepted; translucent links are candidates. A candidate is evidence-backed but not yet verified.</p>{summary.node_cap_reached && <p><b>Expansion cap reached:</b> generate a larger artifact or choose fewer seeds to continue the frontier.</p>}</div>;
}

export function buildTopology({ rows, companies = [], expansionNodes = [], focus = '', anchors = [], selectedSeeds = [], scope = 'global', depth = 2, edgeLimit = 1500, showExposure = false, showIndustries = true, showContext = true, includeCandidates = true, enabledTypes = [], enabledFamilies = [] }) {
  const issuers = new Set([...companies.map((row) => row.company), ...expansionNodes.filter((row) => row.is_universe_company).map((row) => row.canonical_name)].filter(Boolean));
  const allowed = new Set(enabledTypes);
  const allowedFamilies = new Set(enabledFamilies);
  const nodeMetadata = new Map(expansionNodes.map((row) => [row.canonical_name, row]));
  const normalized = collapseRows(rows, issuers, showExposure).filter((edge) => allowed.has(edge.relation_type) && (!allowedFamilies.size || allowedFamilies.has(edge.relationship_family)) && (includeCandidates || edge.review_status === 'accepted'));
  const seeds = scope === 'ego' ? [focus] : selectedSeeds;
  const activeAnchors = anchors.filter((name) => normalized.some((edge) => edge.object === name || edge.subject === name));
  const oneHop = scope === 'global' ? normalized : selectMultiEgo(normalized, seeds, 1);
  const scoped = scope === 'global' ? normalized : selectMultiEgo(normalized, seeds, depth);
  const anchorRows = activeAnchors.length ? selectMultiEgo(normalized, activeAnchors, 1) : [];
  const anchorKeys = new Set(anchorRows.map(relationshipRowKey));
  const ranked = [...scoped].sort(edgeRank);
  const prioritized = activeAnchors.length ? [...anchorRows.sort(edgeRank), ...ranked.filter((edge) => !anchorKeys.has(relationshipRowKey(edge)))] : ranked;
  const ego = (activeAnchors.length && !showContext ? anchorRows.sort(edgeRank) : prioritized).slice(0, edgeLimit);
  const visibleNames = new Set(ego.flatMap((edge) => [edge.object, edge.subject]));
  [...seeds, ...activeAnchors, focus].forEach((name) => name && visibleNames.add(name));
  const visibleCompanies = [...visibleNames].map((name) => ({ company: name, role: nodeMetadata.get(name)?.role || companies.find((row) => row.company === name)?.role || '' }));
  const industryMemberships = showIndustries ? buildIndustryMemberships(visibleCompanies) : [];
  const names = new Set(visibleNames); industryMemberships.forEach((edge) => { names.add(edge.object); names.add(edge.subject); });
  const stats = new Map([...names].map((name) => [name, { degree: 0, evidence: 0 }]));
  ego.forEach((edge) => { const weight = Number(edge.evidence_count || 1); [edge.object, edge.subject].forEach((name) => { const stat = stats.get(name); stat.degree += 1; stat.evidence += weight; }); });
  const seedSet = new Set(seeds);
  const anchorSet = new Set(activeAnchors);
  const neighborSet = new Set(anchorRows.flatMap((edge) => [edge.object, edge.subject]));
  const groupByName = new Map(visibleCompanies.map((row) => [row.company, industryGroup(row.role)]));
  const layoutMode = names.size <= FORCE_LAYOUT_NODE_LIMIT ? 'force' : 'partitioned';
  const labelDegree = names.size > 1_000 ? 15 : names.size > 350 ? 8 : 4;
  const partitionPositions = buildPartitionPositions([...names], groupByName);
  const nodes = [...names].map((name) => {
    const stat = stats.get(name) || { degree: 0, evidence: 0 }; const kind = nodeKind(name, issuers);
    const position = partitionPositions.get(name);
    const isAnchor = anchorSet.has(name); const isNeighbor = neighborSet.has(name); const isInspected = name === focus;
    const contextOnly = activeAnchors.length > 0 && !isAnchor && !isNeighbor && kind !== 'industry';
    return { id: nodeId(name), label: contextOnly && !isInspected ? '' : trim(name, 34), x: position.x, y: position.y, size: kind === 'industry' ? 17 : contextOnly && !isInspected ? Math.min(3.2, 1.8 + Math.sqrt(stat.degree)) : 4 + Math.min(14, Math.sqrt(stat.degree) * 2.6) + (isAnchor ? 3 : 0) + (isInspected ? 2.5 : 0), color: isInspected ? '#f8fafc' : contextOnly ? '#334155' : nodeColor(kind, { isAnchor, isSeed: seedSet.has(name) }), forceLabel: !contextOnly || isInspected ? (kind === 'industry' || seedSet.has(name) || isAnchor || isNeighbor || isInspected || stat.degree >= labelDegree) : false, zIndex: isInspected ? 7 : isAnchor ? 6 : isNeighbor ? 4 : seedSet.has(name) ? 4 : kind === 'industry' ? 3 : 1, fixed: kind === 'industry', kind, isAnchor, isNeighbor, isInspected, contextOnly };
  });
  const nodeByName = new Map(nodes.map((node) => [node.id.slice(5), node.id]));
  const graphRows = [...ego, ...industryMemberships].map((edge, index) => ({ ...edge, id: `rel-${index}-${nodeId(edge.object)}-${nodeId(edge.subject)}-${edge.relation_type}`, objectId: nodeByName.get(encodeURIComponent(edge.object)), subjectId: nodeByName.get(encodeURIComponent(edge.subject)), isFocused: anchorKeys.has(relationshipRowKey(edge)), isAnchorLink: anchorSet.has(edge.object) && anchorSet.has(edge.subject), isContext: activeAnchors.length > 0 && !anchorKeys.has(relationshipRowKey(edge)) && !edge.isIndustryMembership })).filter((edge) => edge.objectId && edge.subjectId);
  const oneHopNodes = new Set([...seeds, ...oneHop.flatMap((edge) => [edge.object, edge.subject])]);
  return { rows: graphRows, nodes, relationshipRows: normalized, layoutMode, displayedEdgeCount: ego.length, availableEdgeCount: scoped.length, omittedEdgeCount: Math.max(0, scoped.length - ego.length), companyCount: nodes.filter((node) => node.kind === 'issuer' || node.kind === 'counterparty').length, industryCount: nodes.filter((node) => node.kind === 'industry').length, exposureCount: nodes.filter((node) => node.kind === 'exposure').length, hopDelta: Math.max(0, visibleNames.size - oneHopNodes.size), acceptedCount: ego.filter((row) => row.review_status === 'accepted').length, candidateCount: ego.filter((row) => row.review_status !== 'accepted').length };
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

function collapseRows(rows, issuers, showExposure) { const index = new Map(); rows.forEach((row) => { const object = String(row.object || row.source || '').trim(); const subject = String(row.subject || row.target || '').trim(); if (!object || !subject || object === subject || (!showExposure && (nodeKind(object, issuers) === 'exposure' || nodeKind(subject, issuers) === 'exposure'))) return; const relation_type = String(row.relation_type || row.relationship_type || 'dependency'); const modality = String(row.modality || 'not_recorded'); const key = `${object}|${subject}|${relation_type}|${modality}`; const prior = index.get(key); const current = { ...row, object, subject, relation_type, modality, relationship_family: relationshipFamily(row), evidence_count: Number(row.evidence_count || 1), review_status: row.review_status || (row.confirmation_status === 'confirmed' ? 'accepted' : 'unreviewed') }; index.set(key, prior ? { ...prior, evidence_count: Number(prior.evidence_count || 0) + current.evidence_count, review_status: prior.review_status === 'accepted' || current.review_status === 'accepted' ? 'accepted' : current.review_status, source_urls: joinValues(prior.source_urls, current.source_urls), source_accession_numbers: [...new Set([...(prior.source_accession_numbers || []), ...(current.source_accession_numbers || [])])] } : current); }); return [...index.values()]; }
export function selectMultiEgo(rows, seeds, depth) { const activeSeeds = seeds.filter(Boolean); if (!activeSeeds.length) return []; const distance = new Map(activeSeeds.map((name) => [name, 0])); let frontier = new Set(activeSeeds); for (let hop = 1; hop <= depth; hop += 1) { const next = new Set(); rows.forEach((edge) => { if (frontier.has(edge.object) && !distance.has(edge.subject)) next.add(edge.subject); if (frontier.has(edge.subject) && !distance.has(edge.object)) next.add(edge.object); }); next.forEach((name) => distance.set(name, hop)); frontier = next; if (!frontier.size) break; } return rows.filter((edge) => distance.has(edge.object) && distance.has(edge.subject)); }
function relationTypes(rows) { const counts = new Map(); rows.forEach((row) => { const type = String(row.relation_type || row.relationship_type || 'dependency'); counts.set(type, (counts.get(type) || 0) + 1); }); return [...counts].map(([type, count]) => ({ type, count })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type)); }
function relationFamilies(rows) { return [...new Set(rows.map(relationshipFamily))].sort((a, b) => familyOrder(a) - familyOrder(b) || a.localeCompare(b)); }
function familyOrder(family) { return ['supply_chain', 'corporate_transaction', 'ownership_control', 'commercial_relationship'].indexOf(family); }
function familyLabel(family) { return ({ supply_chain: 'Supply chain', corporate_transaction: 'Corporate transactions', ownership_control: 'Ownership / control', commercial_relationship: 'Other commercial' })[family] || family.replaceAll('_', ' '); }
function relationshipFamily(row) {
  const existing = String(row.relationship_family || row.relation_family || '').trim();
  if (existing) return existing;
  const type = String(row.relation_type || row.relationship_type || '').toLowerCase();
  if (/(control|subsidiar|owns|ownership|parent)/.test(type)) return 'ownership_control';
  if (/(acquir|merger|divest|dispos|asset_sale|invests_in)/.test(type)) return 'corporate_transaction';
  if (/(license|partner|alliance|joint_venture|distribution)/.test(type)) return 'commercial_relationship';
  return 'supply_chain';
}
function relationshipRowKey(edge) { return `${edge.object}|${edge.subject}|${edge.relation_type}|${edge.modality || 'not_recorded'}`; }
function listValue(value) { return Array.isArray(value) ? value.filter(Boolean) : String(value || '').split(/[;,]/).map((item) => item.trim()).filter(Boolean); }
export function evidenceForRelationship(relationship, evidence) { const ids = new Set(listValue(relationship.evidence_ids)); return evidence.filter((item) => ids.has(item.evidence_id) || ids.has(item.passage_id)); }
export function buildNodeProfile(rows, name) {
  const connected = rows.filter((edge) => edge.object === name || edge.subject === name);
  const incoming = connected.filter((edge) => edge.subject === name).sort(edgeRank);
  const outgoing = connected.filter((edge) => edge.object === name).sort(edgeRank);
  const ownership = connected.filter((edge) => edge.relationship_family === 'ownership_control');
  const parent = ownership.find((edge) => edge.subject === name);
  const subsidiaries = ownership.filter((edge) => edge.object === name);
  return {
    incoming, outgoing, parent, subsidiaries,
    accepted: connected.filter((edge) => edge.review_status === 'accepted').length,
    candidate: connected.filter((edge) => edge.review_status !== 'accepted').length,
    filingCount: new Set(connected.flatMap((edge) => listValue(edge.source_accession_numbers))).size,
    evidenceCount: connected.reduce((sum, edge) => sum + Number(edge.evidence_count || 1), 0),
    crossFiled: connected.filter((edge) => edge.cross_filing_verified || edge.verification_status === 'cross_filing_verified').length,
    products: [...new Set(connected.flatMap((edge) => listValue(edge.product_or_service)))].slice(0, 12),
  };
}
function expansionSeedNames(expansion) { const ids = new Set(expansion?.seed_entity_ids || []); return (expansion?.nodes || []).filter((row) => row.is_seed || ids.has(row.entity_id)).map((row) => row.canonical_name).filter(Boolean); }
function nodeKind(name, issuers) { if (String(name).startsWith('Industry · ')) return 'industry'; if (issuers.has(name)) return 'issuer'; return isAnonymousOrLowQuality(name) ? 'exposure' : 'counterparty'; }
function isAnonymousOrLowQuality(name) { const value = String(name || '').trim(); return /^(?:(?:direct|major|large|significant)\s+)?(?:customer|customers|supplier|suppliers|vendor|vendors|distributor|distributors|partner|partners)\s+(?:[a-z](?:\s+(?:and|or)\s+[a-z])?|[0-9]+)$/i.test(value) || /\b(class|dependency class|capacity class)\b/i.test(value) || /^(contents?|table of contents)\b/i.test(value); }
function edgeRank(a, b) { return (a.review_status === 'accepted' ? 0 : 1) - (b.review_status === 'accepted' ? 0 : 1) || Number(b.evidence_count || 0) - Number(a.evidence_count || 0) || Number(b.confidence || b.avg_confidence || 0) - Number(a.confidence || a.avg_confidence || 0) || String(a.relationship_id || '').localeCompare(String(b.relationship_id || '')); }
function relationColor(type) { return RELATION_COLORS[type] || '#94a3b8'; }
function nodeColor(kind, { isAnchor = false, isSeed = false } = {}) { if (isAnchor) return '#f472b6'; if (isSeed) return '#fbbf24'; return { issuer: '#2dd4bf', counterparty: '#60a5fa', exposure: '#fb7185', industry: '#c084fc' }[kind] || '#94a3b8'; }
function nodeId(value) { return `node:${encodeURIComponent(value)}`; }
function decodeNodeName(node) { return decodeURIComponent(String(node).replace(/^node:/, '')); }
function centeredCoordinate(value, salt) { let hash = 2166136261 + salt; for (let i = 0; i < value.length; i += 1) { hash ^= value.charCodeAt(i); hash = Math.imul(hash, 16777619); } return (((hash >>> 0) % 2001) - 1000) / 1000; }
function trim(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }
function joinValues(first, second) { return [...new Set(`${first || ''};${second || ''}`.split(';').filter(Boolean))].join(';'); }
