import { useEffect, useState } from 'react';
import { CheckCircle2, CircleAlert, Database, Download } from 'lucide-react';
import { confirmationStatus } from '../lib/filters.js';

export function Resolution({ data, runId, relationshipStatuses = ['confirmed', 'candidate'], resolutionRecords = [] }) {
  const entities = data.canonical_entities || [];
  const relationships = data.canonical_relationships || [];
  const diagnostics = data.canonicalization_diagnostics || [];
  const excluded = diagnostics.filter((row) => row.status !== 'canonicalized');
  const storageKey = `moonbow:canonical-review:${runId}`;
  const [decisions, setDecisions] = useState(() => loadDecisions(storageKey));
  const [notice, setNotice] = useState('');

  useEffect(() => {
    setDecisions(loadDecisions(storageKey));
  }, [storageKey]);
  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify(decisions));
  }, [decisions, storageKey]);

  const updateDecision = (relationshipId, patch) => {
    setDecisions((current) => ({
      ...current,
      [relationshipId]: { status: 'unreviewed', notes: '', ...current[relationshipId], ...patch },
    }));
    if (patch.status) setNotice(`Saved: ${formatDecision(patch.status)}. This choice is stored in this browser.`);
  };
  const exportDecisions = () => downloadReviewCsv(runId, relationships, decisions);
  const visibleRelationships = relationships.filter((row) => relationshipStatuses.includes(confirmationStatus({ ...row, review_status: reviewFor(row, decisions).status })));
  const reviewedCount = relationships.filter((row) => reviewFor(row, decisions).status !== 'unreviewed').length;
  return (
    <div className="view-grid">
      <section className="panel wide resolution-summary">
        <Summary icon={<CheckCircle2 size={20} />} label="Resolved entities" value={entities.length} detail="companies or organizations allowed into the canonical layer" />
        <Summary icon={<Database size={20} />} label="Canonical relationships" value={relationships.length} detail="named company-to-company relationships with evidence" />
        <Summary icon={<CircleAlert size={20} />} label="Evidence not yet in graph" value={excluded.length} detail="retained as a lead, generic class, or unsupported relationship" />
      </section>
      <section className="panel wide">
        <div className="panel-head"><h2>Alias Resolution Review</h2><span>{resolutionRecords.length} relation-linked records · only REVIEW needs human action</span></div>
        <div className="table-frame"><table><thead><tr><th>Mention</th><th>Priority</th><th>SEC evidence</th><th>Candidate sources</th><th>LLM assessment</th><th>Safety validation</th><th>Decision</th></tr></thead>
          <tbody>{resolutionRecords.map((row) => <tr key={row.resolution_id}><td>{row.mention_text}</td><td>{row.priority_score}</td><td><details><summary>{row.evidence_count} evidence · {row.distinct_filing_count} filings</summary><blockquote>{row.sample_evidence}</blockquote></details></td><td>{(row.resolution_evidence || []).map((item) => item.source).join(', ') || 'Pending candidate generation'}</td><td>{(row.llm_assessments || []).map((item) => `${item.candidate_rank}: ${item.assessment}`).join(' · ') || 'Not run'}</td><td>{row.safety_validation?.status || 'Not run'}</td><td><span className={`pill ${row.decision === 'AUTO_ACCEPT' ? 'current' : row.decision === 'REVIEW' ? 'risk' : ''}`}>{row.decision}</span></td></tr>)}</tbody>
        </table></div>
      </section>
      <section className="panel wide">
        <div className="panel-head"><h2>Recognized entities</h2><span>{entities.length} rows</span></div>
        <div className="table-frame"><table><thead><tr><th>Name</th><th>Status</th><th>Kind</th><th>Ticker</th><th>CIK</th><th>Role</th></tr></thead>
          <tbody>{entities.map((row) => <tr key={row.entity_id}><td>{row.canonical_name}</td><td><span className="pill current">{row.resolution_status}</span></td><td>{row.entity_kind}</td><td>{row.ticker || '-'}</td><td>{row.cik || '-'}</td><td>{row.role || '-'}</td></tr>)}</tbody>
        </table></div>
      </section>
      <section className="panel wide">
        <div className="panel-head"><h2>Canonical relationships</h2><span>{visibleRelationships.length} shown · {reviewedCount}/{relationships.length} marked</span><button className="compact-button" onClick={exportDecisions}><Download size={14} /> Export review CSV</button></div>
        <p className="review-help" aria-live="polite">Choose <b>Accept</b> to keep it, <b>Reject</b> to flag it as wrong, or <b>Review</b> to leave it for later. {notice}</p>
        <div className="table-frame"><table><thead><tr><th>From</th><th>To</th><th>Layer</th><th>Type</th><th>Category</th><th>Product / service</th><th>Evidence</th><th>Confidence</th><th>LLM audit</th><th>Source</th><th>Status</th><th>Notes</th></tr></thead>
          <tbody>{visibleRelationships.map((row) => <ReviewRow key={row.relationship_id} row={row} evidence={evidenceForRelationship(row, data.evidence || [])} decision={decisions[row.relationship_id]} onChange={updateDecision} />)}</tbody>
        </table></div>
      </section>
      <section className="panel wide">
        <div className="panel-head"><h2>Not yet in the graph</h2><span>These rows remain inspectable and are the queue for later resolution</span></div>
        <div className="table-frame"><table><thead><tr><th>Issuer</th><th>Raw object</th><th>Normalized object</th><th>Relation</th><th>Reason</th><th>Source</th></tr></thead>
          <tbody>{excluded.slice(0, 500).map((row, index) => <tr key={`${row.passage_id}-${index}`}><td>{row.subject}</td><td>{row.raw_object}</td><td>{row.normalized_object}</td><td>{row.relation_type}</td><td><span className="pill risk">{formatStatus(row.status)}</span></td><td><a href={row.source_document_url} target="_blank" rel="noreferrer">Source</a></td></tr>)}</tbody>
        </table></div>
      </section>
    </div>
  );
}

function ReviewRow({ row, evidence, decision, onChange }) {
  const review = reviewFor(row, decision ? { [row.relationship_id]: decision } : {});
  const source = row.source_entity_name || row.supplier_name;
  const target = row.target_entity_name || row.customer_name;
  const sourceRole = row.source_role || 'supplier';
  const targetRole = row.target_role || 'customer';
  return <tr>
    <td>{source}<small className="muted">{sourceRole}</small></td><td>{target}<small className="muted">{targetRole}</small></td><td>{formatFamily(row.relationship_family)}</td><td>{row.relationship_type}</td><td>{(row.categories || []).join(', ') || '—'}</td><td>{row.product_or_service || '—'}</td>
    <td><EvidencePreview count={row.evidence_count} evidence={evidence} /></td><td>{row.confidence}</td><td><AuditVerdict audit={row.llm_audit} /></td><td>{(row.source_types || []).join(', ')}</td>
    <td className="review-actions">
      <button aria-pressed={review.status === 'accepted'} className={`compact-button ${review.status === 'accepted' ? 'review-accepted' : ''}`} onClick={() => onChange(row.relationship_id, { status: 'accepted' })}>Accept</button>
      <button aria-pressed={review.status === 'rejected'} className={`compact-button ${review.status === 'rejected' ? 'review-rejected' : ''}`} onClick={() => onChange(row.relationship_id, { status: 'rejected' })}>Reject</button>
      <button aria-pressed={review.status === 'needs_review'} className={`compact-button ${review.status === 'needs_review' ? 'review-pending' : ''}`} onClick={() => onChange(row.relationship_id, { status: 'needs_review' })}>Review</button>
      {row.decision_reason && review.status === 'accepted' && <small className="decision-reason">{row.decision_reason}</small>}
    </td>
    <td><input aria-label={`Notes for ${source} to ${target}`} className="review-note" value={review.notes} onChange={(event) => onChange(row.relationship_id, { notes: event.target.value })} placeholder="Why? Optional" /></td>
  </tr>;
}

function reviewFor(row, decisions) {
  return decisions[row.relationship_id] || row.human_review || { status: row.review_status || 'unreviewed', notes: '' };
}

function AuditVerdict({ audit }) {
  if (!audit) return <span className="muted">Not run</span>;
  const history = [...(audit.decision_history || [])].reverse();
  return <details className="audit-details"><summary><span className={`pill audit-${audit.decision}`}>{audit.decision}</span></summary><div className="audit-popover"><b>Current conclusion</b><p>{audit.reason}</p>{audit.current_reviewed_at && <p className="muted">Reviewed {new Date(audit.current_reviewed_at).toLocaleString()}</p>}{history.length > 0 && <><b>Audit history</b>{history.map((event, index) => <p key={`${event.reviewed_at}-${index}`} className="audit-history"><span className={`pill audit-${event.decision}`}>{event.decision}</span> {event.valid ? 'valid' : 'invalid response'} · {event.reason || event.follow_up_reason || 'No reason returned'}</p>)}</>}{audit.supported_relation_type && <p>Suggested type: {audit.supported_relation_type}</p>}{audit.evidence_quote && <blockquote>“{audit.evidence_quote}”</blockquote>}</div></details>;
}

function EvidencePreview({ count, evidence }) {
  return <details className="evidence-details">
    <summary>{count} evidence{count === 1 ? '' : 's'} · View</summary>
    <div className="evidence-popover">
      {evidence.length ? evidence.map((item, index) => <article key={`${item.passage_id}-${index}`}>
        <div><b>{item.form}</b> · {item.source_section || 'filing text'} · paragraph {Number(item.paragraph_offset) + 1}</div>
        <blockquote>{item.evidence_text}</blockquote>
        <a href={item.source_document_url} target="_blank" rel="noreferrer">Open SEC source ↗</a>
      </article>) : <p>The evidence text is not available in this saved dashboard dataset. Re-run this filing to refresh it.</p>}
    </div>
  </details>;
}

function evidenceForRelationship(relationship, evidence) {
  const ids = new Set(relationship.evidence_ids || []);
  return evidence.filter((item) => ids.has(item.passage_id));
}

function Summary({ icon, label, value, detail }) {
  return <div><span>{icon}{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function formatStatus(value) {
  return {
    unresolved_or_generic_counterparty: 'generic or unresolved counterparty',
    not_a_directed_supply_relation: 'not a directed relationship',
  }[value] || value;
}

function formatFamily(value) {
  return { supply_chain: 'Supply chain', corporate_transaction: 'Corporate transaction', ownership_control: 'Ownership / control', commercial_relationship: 'Commercial relationship', risk_exposure: 'Risk / exposure' }[value] || 'Supply chain';
}

function formatDecision(value) {
  return { accepted: 'Accepted', rejected: 'Rejected', needs_review: 'Needs review' }[value] || value;
}

function loadDecisions(storageKey) {
  try { return JSON.parse(window.localStorage.getItem(storageKey) || '{}'); } catch { return {}; }
}

function downloadReviewCsv(runId, relationships, decisions) {
  const columns = ['relationship_id', 'supplier', 'customer', 'relationship_type', 'evidence_count', 'confidence', 'review_status', 'review_notes'];
  const rows = relationships.map((row) => {
    const decision = reviewFor(row, decisions);
    return [row.relationship_id, row.supplier_name, row.customer_name, row.relationship_type, row.evidence_count, row.confidence, decision.status, decision.notes];
  });
  const csv = [columns, ...rows].map((row) => row.map((value) => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a'); link.href = url; link.download = `${runId}-canonical-review.csv`; link.click(); URL.revokeObjectURL(url);
}
