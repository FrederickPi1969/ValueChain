import { Database } from 'lucide-react';
import { sourceDisplayName } from '../lib/filingSearch.js';

function compact(value) {
  return Number(value || 0).toLocaleString();
}

export function SourceCoverage({ sources }) {
  const rows = sources.slice().sort((left, right) => Number(right.complete_filings || 0) - Number(left.complete_filings || 0));
  return (
    <section className="coverage-page">
      <div className="coverage-heading">
        <Database size={22} />
        <div>
          <span className="eyebrow">Archive coverage</span>
          <h2>Available source inventories</h2>
          <p>Counts below are the current locally tracked acquisition records, not vendor marketing coverage.</p>
        </div>
      </div>
      <div className="coverage-grid">
        {rows.map((source) => (
          <article className="coverage-card" key={source.source_id}>
            <strong>{sourceDisplayName(source.source_id)}</strong>
            <span>{source.authority || source.source_id}</span>
            <div><b>{compact(source.issuers)}</b><small>issuers</small></div>
            <div><b>{compact(source.complete_filings)}</b><small>available filings</small></div>
            <div><b>{compact(source.documents)}</b><small>stored documents</small></div>
          </article>
        ))}
      </div>
    </section>
  );
}
