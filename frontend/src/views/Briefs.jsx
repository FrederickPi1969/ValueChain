import { useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, ExternalLink, FileText, Search, ShieldAlert, Target } from 'lucide-react';
import { modalityClass, shortRelation, truncate } from '../components/format.js';
import { formatPercent, getAnalystBullets } from '../lib/briefs.js';

export function Briefs({
  entries,
  brief,
  loading,
  error,
  selectedTicker,
  onSelectTicker,
  onCompanyFilter,
}) {
  const [query, setQuery] = useState('');
  const filteredEntries = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter((entry) =>
      [entry.ticker, entry.company_name, entry.role].some((value) => String(value || '').toLowerCase().includes(needle))
    );
  }, [entries, query]);

  if (!entries.length) {
    return (
      <section className="panel wide">
        <div className="empty">
          No company briefs have been synced for this run yet.
          <br />
          <code>python scripts/sync_company_briefs_to_frontend.py --run-id current-run-id</code>
        </div>
      </section>
    );
  }

  return (
    <section className="brief-layout">
      <aside className="brief-sidebar">
        <div className="brief-search input-with-icon">
          <Search size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search brief" />
        </div>
        <div className="brief-list">
          {filteredEntries.map((entry) => (
            <button
              key={entry.ticker}
              className={`brief-list-row ${selectedTicker === entry.ticker ? 'active' : ''}`}
              onClick={() => onSelectTicker(entry.ticker)}
            >
              <strong>{entry.ticker}</strong>
              <span>{entry.company_name}</span>
              <small>{entry.evidence_count || 0} evidence rows</small>
            </button>
          ))}
        </div>
      </aside>

      <article className="brief-detail">
        {error && (
          <div className="inline-alert">
            <AlertTriangle size={16} /> {error}
          </div>
        )}
        {loading && <div className="brief-loading">Loading brief...</div>}
        {!loading && brief && (
          <>
            <BriefHeader brief={brief} onCompanyFilter={onCompanyFilter} />
            <AnalystInterpretation brief={brief} />
            <div className="brief-section-grid">
              <ClaimSection
                title="Top Operating Dependencies"
                icon={<Target size={17} />}
                claims={brief.top_operating_dependencies}
              />
              <ClaimSection
                title="Top Risk Exposures"
                icon={<ShieldAlert size={17} />}
                claims={brief.top_risk_exposures}
              />
              <ClaimSection title="Current-Fact Edges" icon={<CheckCircle2 size={17} />} claims={brief.current_fact_edges} />
              <ClaimSection title="Strategic Relations" icon={<FileText size={17} />} claims={brief.strategic_relations} />
            </div>
            <EvidenceTable rows={brief.evidence_table || []} />
          </>
        )}
      </article>
    </section>
  );
}

function BriefHeader({ brief, onCompanyFilter }) {
  const company = brief.company || {};
  const role = brief.company_role || {};
  const dominantRelations = Object.entries(role.dominant_relation_types || {})
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 5);

  return (
    <header className="brief-header">
      <div>
        <span className="eyebrow">Company Dependency Brief</span>
        <h2>{company.company_name || company.ticker}</h2>
        <div className="brief-subtitle">
          <span>{company.ticker}</span>
          <span>{role.brief_role_label || role.declared_role || company.role}</span>
          <span>{brief.model_version || brief.analyst_interpretation?.model_version || 'deterministic'}</span>
        </div>
      </div>
      <button onClick={() => onCompanyFilter(company.company_name || company.ticker)}>Filter Dashboard</button>
      {company.notes && <p>{company.notes}</p>}
      {!!dominantRelations.length && (
        <div className="relation-chips">
          {dominantRelations.map(([relation, count]) => (
            <span key={relation} className="pill">
              {shortRelation(relation)}: {count}
            </span>
          ))}
        </div>
      )}
    </header>
  );
}

function AnalystInterpretation({ brief }) {
  const interpretation = brief.analyst_interpretation || {};
  return (
    <section className="brief-analyst">
      <div>
        <span className="eyebrow">Analyst Interpretation</span>
        <p>{interpretation.one_paragraph_summary || 'No generated analyst summary is available for this brief.'}</p>
      </div>
      <AnalystList title="What This Implies" rows={getAnalystBullets(brief, 'what_this_implies')} />
      <AnalystList title="What To Monitor" rows={getAnalystBullets(brief, 'what_to_monitor')} />
      <AnalystList title="Weak Evidence" rows={getAnalystBullets(brief, 'weak_or_missing_evidence')} />
    </section>
  );
}

function AnalystList({ title, rows }) {
  return (
    <div className="analyst-list">
      <h3>{title}</h3>
      {rows.length ? (
        <ul>
          {rows.map((row, index) => (
            <li key={`${title}-${index}`}>{row}</li>
          ))}
        </ul>
      ) : (
        <p className="muted">No items.</p>
      )}
    </div>
  );
}

function ClaimSection({ title, icon, claims }) {
  const rows = claims || [];
  return (
    <section className="brief-claim-section">
      <div className="section-title">
        {icon}
        <h3>{title}</h3>
        <span>{rows.length}</span>
      </div>
      {rows.length ? (
        <div className="claim-list">
          {rows.slice(0, 8).map((claim) => (
            <ClaimCard key={claim.claim_id || `${claim.relation_type}-${claim.object}`} claim={claim} />
          ))}
        </div>
      ) : (
        <p className="muted">No supported claims in this section.</p>
      )}
    </section>
  );
}

function ClaimCard({ claim }) {
  const modalities = String(claim.modality_mix || '')
    .split(';')
    .map((value) => value.trim())
    .filter(Boolean);
  return (
    <div className="claim-card">
      <div className="claim-topline">
        <strong>{claim.canonical_object || claim.object || 'Unknown object'}</strong>
        <span>{formatPercent(claim.avg_confidence)}</span>
      </div>
      <div className="claim-meta">
        <span>{shortRelation(claim.relation_type)}</span>
        <span>{claim.evidence_count || 0} evidence</span>
        <span>{claim.first_seen} to {claim.last_seen}</span>
      </div>
      {!!modalities.length && (
        <div className="claim-modalities">
          {modalities.map((modality) => (
            <span key={modality} className={`pill ${modalityClass(modality)}`}>
              {modality}
            </span>
          ))}
        </div>
      )}
      <small>{claim.forms || claim.accessions || ''}</small>
    </div>
  );
}

function EvidenceTable({ rows }) {
  return (
    <section className="brief-evidence">
      <div className="panel-head">
        <h2>Evidence Table</h2>
        <span>{rows.length} rows</span>
      </div>
      <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Relation</th>
              <th>Object</th>
              <th>Modality</th>
              <th>Filing</th>
              <th>Section</th>
              <th>Evidence</th>
              <th>SEC</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 120).map((row) => (
              <tr key={row.evidence_id}>
                <td>{row.evidence_id}</td>
                <td>{shortRelation(row.relation_type)}</td>
                <td>{row.canonical_object || row.object}</td>
                <td>
                  <span className={`pill ${modalityClass(row.modality)}`}>{row.modality}</span>
                </td>
                <td>
                  {row.form} {row.filing_date}
                  <br />
                  <small>{row.accession_number}</small>
                </td>
                <td>
                  {row.section}
                  <br />
                  <small>paragraph {row.paragraph_offset}</small>
                </td>
                <td className="evidence-preview">{truncate(row.evidence_text, 320)}</td>
                <td>
                  {row.source_document_url && (
                    <a className="source-link" href={row.source_document_url} target="_blank" rel="noreferrer">
                      Open <ExternalLink size={12} />
                    </a>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
