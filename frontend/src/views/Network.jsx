import { useMemo, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight, Network as NetworkIcon } from 'lucide-react';
import { shortRelation } from '../components/format.js';
import { confirmationStatus } from '../lib/filters.js';

const MAX_EDGES = 120;

// Network edges retain canonical typed source -> target direction before display.
export function Network({ edges, allEdges, lineageEvents = [], companies, onFocus }) {
  const [selected, setSelected] = useState('');
  const [selectedEdge, setSelectedEdge] = useState(null);
  const [hoveredEdge, setHoveredEdge] = useState(null);
  const [expandedParents, setExpandedParents] = useState(() => new Set());
  const graph = useMemo(() => buildGraph(edges, allEdges, expandedParents), [edges, allEdges, expandedParents]);
  const selectedNode = graph.nodes.find((node) => node.name === selected);
  const inspectedEdge = hoveredEdge || selectedEdge;

  if (!graph.nodes.length) return <div className="empty network-empty">No graph edges match the current filters.</div>;

  return (
    <div className="network-layout">
      <section className="panel network-panel">
        <div className="panel-head">
          <div><h2><NetworkIcon size={18} /> Relationship Map</h2><span>Edges follow their typed source → target roles. Subsidiaries are collapsed into their parent by default.</span></div>
          <span>{graph.edges.length} shown / {edges.length} matching edges</span>
        </div>
        {edges.length > MAX_EDGES && <div className="network-note">Showing the {MAX_EDGES} most evidenced edges. Narrow filters to inspect the remainder.</div>}
        <svg className="network-canvas" viewBox={`0 0 ${graph.width} ${graph.height}`} role="img" aria-label="Disclosure dependency network">
          <defs><marker id="network-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3.5" orient="auto"><path d="M0,0 L0,7 L7,3.5 z" fill="#668198" /></marker></defs>
          {graph.edges.map((edge) => (
            <line key={edge.key} x1={edge.from.x + 118} y1={edge.from.y} x2={edge.to.x - 118} y2={edge.to.y}
              className={`network-link ${edge.modality} ${confirmationStatus(edge)}`} strokeWidth={Math.min(5, 1 + Math.log2(Number(edge.evidence_count || 1)))} markerEnd="url(#network-arrow)" onMouseEnter={() => setHoveredEdge(edge)} onMouseLeave={() => setHoveredEdge(null)} onClick={() => { setSelected(edge.to.name); setSelectedEdge(edge); }}>
              <title>{`${edge.from.name} → ${edge.to.name}: ${shortRelation(edge.relation_type)}${edge.categories?.length ? ` · ${edge.categories.join(', ')}` : ''}${edge.product_or_service ? ` · ${edge.product_or_service}` : ''} · ${edge.relationship_family || 'supply_chain'} · ${edge.review_status || 'LLM candidate'}`}</title>
            </line>
          ))}
          {graph.nodes.map((node) => (
            <g key={node.name} className={`network-node ${selected === node.name ? 'selected' : ''}`} onClick={() => { setSelected(node.name); if (node.childCount) setExpandedParents((current) => { const next = new Set(current); next.has(node.name) ? next.delete(node.name) : next.add(node.name); return next; }); }}>
              <rect x={node.x - 118} y={node.y - 20} width="236" height="40" rx="8" />
              <text x={node.x} y={node.y - 2} textAnchor="middle">{trim(node.name, 31)}</text>
              <text x={node.x} y={node.y + 13} textAnchor="middle" className="network-node-meta">{node.childCount ? `${expandedParents.has(node.name) ? 'Hide' : 'Expand'} ${node.childCount} subsidiaries · ` : ''}{node.incoming} incoming / {node.outgoing} outgoing</text>
            </g>
          ))}
        </svg>
      </section>
      <aside className="network-sidebar">
        <DataGaps companies={companies} onFocus={onFocus} />
        <section className="panel network-detail">
          <div className="panel-head"><h2>{inspectedEdge ? 'Relationship details' : 'Selected node'}</h2></div>
          {inspectedEdge ? <RelationshipDetail edge={inspectedEdge} lineage={lineageEvents.filter((event) => event.relationship_id === inspectedEdge.relationship_id)} pinned={Boolean(selectedEdge && selectedEdge.key === inspectedEdge.key)} onClear={() => setSelectedEdge(null)} /> : selectedNode ? <><strong>{selectedNode.name}</strong><p>{selectedNode.incoming} disclosed upstream links and {selectedNode.outgoing} downstream references in the current filtered view.{selectedNode.childCount ? ` ${selectedNode.childCount} direct subsidiaries are ${expandedParents.has(selectedNode.name) ? 'expanded' : 'collapsed'}.` : ''}</p><button onClick={() => onFocus(selectedNode.name)}>Filter evidence</button></> : <p className="muted">Hover over or select an edge to inspect it.</p>}
        </section>
      </aside>
    </div>
  );
}

function RelationshipDetail({ edge, lineage, pinned, onClear }) {
  return <div className="relationship-detail">
    <strong>{edge.from.name} <span>→</span> {edge.to.name}</strong>
    <dl>
      <div><dt>Relationship</dt><dd>{shortRelation(edge.relation_type)}</dd></div>
      <div><dt>Endpoint roles</dt><dd>{edge.source_role || 'source'} → {edge.target_role || 'target'}</dd></div>
      <div><dt>Category</dt><dd>{edge.categories?.join(', ') || 'Not specified'}</dd></div>
      <div><dt>Product / service</dt><dd>{edge.product_or_service || 'Not specified'}</dd></div>
      <div><dt>Evidence</dt><dd>{edge.evidence_count} passage{Number(edge.evidence_count) === 1 ? '' : 's'}</dd></div>
      <div><dt>Status</dt><dd>{reviewLabel(edge)}</dd></div>
      {edge.risk_flags?.length > 0 && <div><dt>Audit flags</dt><dd>{edge.risk_flags.join(', ')}</dd></div>}
    </dl>
    <details className="audit-details"><summary>Ontology & lineage</summary><div className="audit-popover"><p><b>Ontology:</b> {lineage[0]?.ontology_version || 'Not recorded'}</p>{lineage.length ? lineage.map((event) => <p key={event.event_id}><b>{event.stage}</b> · {event.actor || 'system'} · {event.decision_source} · {event.evidence_ids?.length || 0} passages{event.created_at ? ` · ${new Date(event.created_at).toLocaleString()}` : ''}{event.direction_correction_of ? ` · corrects ${event.direction_correction_of}` : ''}<br/><span className="muted">{event.before_state?.review_status || 'new'} → {event.after_state?.review_status || 'current'}</span></p>) : <p className="muted">No lineage event published for this edge yet.</p>}</div></details>
    {pinned && <button className="compact-button" onClick={onClear}>Clear selected edge</button>}
  </div>;
}

function reviewLabel(edge) {
  return { confirmed: 'Confirmed', rejected: 'Rejected', candidate: 'Unconfirmed candidate' }[confirmationStatus(edge)] || 'Unconfirmed candidate';
}

function DataGaps({ companies, onFocus }) {
  const [open, setOpen] = useState(true);
  const gaps = companies.filter((company) => company.coverage_status !== 'graph_ready');
  return (
    <section className="panel data-gaps">
      <div className="panel-head"><div><h2><AlertTriangle size={18} /> Data gaps</h2><span>Pipeline coverage, not a relationship-quality score</span></div><button className="icon-button" onClick={() => setOpen((value) => !value)} aria-label="Toggle data gaps">{open ? <ChevronDown size={18} /> : <ChevronRight size={18} />}</button></div>
      {open && (gaps.length ? <div className="gap-list">{gaps.slice(0, 12).map((company) => <button key={company.company} className="gap-row" onClick={() => onFocus(company.company)}><span><strong>{company.company}</strong><small>{statusLabel(company.coverage_status)}</small></span><small>{company.filing_count} filings · {company.candidate_passage_count} candidates · {company.evidence_count} evidence</small></button>)}{gaps.length > 12 && <p className="muted">+ {gaps.length - 12} additional companies</p>}</div> : <p className="success-note">All companies with processed filings have at least one graph edge.</p>)}
    </section>
  );
}

function buildGraph(edges, allEdges, expandedParents) {
  const parentByChild = parentIndex(allEdges);
  const childCounts = new Map();
  parentByChild.forEach((parent) => childCounts.set(parent, (childCounts.get(parent) || 0) + 1));
  const selectedEdges = collapseAndAggregate(edges, parentByChild, expandedParents)
    .sort((a, b) => Number(b.evidence_count || 0) - Number(a.evidence_count || 0)).slice(0, MAX_EDGES);
  const byName = new Map();
  const graphEdges = selectedEdges.map((edge) => {
    const from = addNode(byName, edge.object); const to = addNode(byName, edge.subject);
    from.outgoing += 1; to.incoming += 1;
    return { ...edge, from, to, key: `${edge.object}:${edge.subject}:${edge.relation_type}:${edge.modality}:${edge.relationship_family}` };
  });
  const nodes = [...byName.values()].sort((a, b) => (b.incoming + b.outgoing) - (a.incoming + a.outgoing) || a.name.localeCompare(b.name));
  const columns = [[], [], []];
  nodes.forEach((node) => columns[node.incoming && node.outgoing ? 1 : node.incoming ? 2 : 0].push(node));
  const height = Math.max(420, Math.max(...columns.map((column) => column.length), 1) * 58 + 52);
  columns.forEach((column, columnIndex) => column.forEach((node, index) => { node.x = 155 + columnIndex * 350; node.y = 46 + index * 58; }));
  nodes.forEach((node) => { node.childCount = childCounts.get(node.name) || 0; });
  return { nodes, edges: graphEdges, width: 1010, height };
}

function parentIndex(allEdges) {
  const index = new Map();
  allEdges.filter((edge) => edge.relationship_family === 'ownership_control').forEach((edge) => {
    // Network records retain the visual object -> subject direction: parent -> child.
    if (edge.object && edge.subject && edge.object !== edge.subject && !index.has(edge.subject)) index.set(edge.subject, edge.object);
  });
  return index;
}

function collapseAndAggregate(edges, parentByChild, expandedParents) {
  const aggregate = new Map();
  edges.forEach((edge) => {
    const originalFrom = String(edge.object || 'Unnamed counterparty');
    const originalTo = String(edge.subject || 'Unnamed counterparty');
    const from = visibleAncestor(originalFrom, parentByChild, expandedParents);
    const to = visibleAncestor(originalTo, parentByChild, expandedParents);
    // Internal ownership links only appear after their parent is explicitly expanded.
    if (edge.relationship_family === 'ownership_control' && (!expandedParents.has(originalFrom) || from === to)) return;
    if (from === to) return;
    const key = `${from}|${to}|${edge.relationship_type}|${edge.modality}|${edge.relationship_family}`;
    const previous = aggregate.get(key);
    aggregate.set(key, previous ? { ...previous, evidence_count: Number(previous.evidence_count || 0) + Number(edge.evidence_count || 0) } : { ...edge, object: from, subject: to });
  });
  return [...aggregate.values()];
}

function visibleAncestor(name, parentByChild, expandedParents) {
  let current = name;
  const seen = new Set();
  while (parentByChild.has(current) && !expandedParents.has(parentByChild.get(current)) && !seen.has(current)) {
    seen.add(current);
    current = parentByChild.get(current);
  }
  return current;
}

function addNode(byName, rawName) {
  const name = String(rawName || 'Unnamed counterparty');
  if (!byName.has(name)) byName.set(name, { name, incoming: 0, outgoing: 0, x: 0, y: 0 });
  return byName.get(name);
}

function trim(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }
function statusLabel(status) { return { evidence_only: 'Evidence has not aggregated to an edge', candidate_only: 'Candidates need extraction/review', filed_no_candidates: 'Filings parsed, no relevant candidates', no_filings: 'No filing coverage yet' }[status] || status; }
