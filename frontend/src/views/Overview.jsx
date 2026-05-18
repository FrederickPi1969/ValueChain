import { countWeighted, uniqueSorted } from '../lib/filters.js';
import { shortRelation } from '../components/format.js';

export function Overview({ edges, evidence }) {
  return (
    <div className="view-grid">
      <Panel title="Relation Mix" detail={`${uniqueSorted(evidence.map((row) => row.relation_type)).length} types`}>
        <BarList rows={evidence} groupKey="relation_type" limit={12} />
      </Panel>
      <Panel title="Modality Mix" detail={`${uniqueSorted(evidence.map((row) => row.modality)).length} modalities`}>
        <BarList rows={evidence} groupKey="modality" limit={6} />
      </Panel>
      <section className="panel wide">
        <div className="panel-head">
          <h2>Company x Relation Exposure</h2>
          <span>evidence counts</span>
        </div>
        <Heatmap edges={edges} />
      </section>
    </div>
  );
}

function Panel({ title, detail, children }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
        <span>{detail}</span>
      </div>
      {children}
    </section>
  );
}

function BarList({ rows, groupKey, limit }) {
  const counts = countWeighted(rows, groupKey).slice(0, limit);
  const max = Math.max(...counts.map(([, count]) => count), 1);
  if (!counts.length) return <div className="empty">No matching records</div>;
  return (
    <div className="bar-list">
      {counts.map(([label, count]) => (
        <div className="bar-row" key={label}>
          <span title={label}>{label}</span>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${Math.max(4, (count / max) * 100)}%` }} />
          </div>
          <strong>{count}</strong>
        </div>
      ))}
    </div>
  );
}

function Heatmap({ edges }) {
  const companies = uniqueSorted(edges.map((edge) => edge.subject)).slice(0, 18);
  const relations = uniqueSorted(edges.map((edge) => edge.relation_type)).slice(0, 12);
  const matrix = new Map();
  let max = 0;
  edges.forEach((edge) => {
    const key = `${edge.subject}::${edge.relation_type}`;
    const count = (matrix.get(key) || 0) + Number(edge.evidence_count || 0);
    matrix.set(key, count);
    max = Math.max(max, count);
  });
  if (!companies.length || !relations.length) return <div className="empty">No matrix records</div>;
  return (
    <div className="table-frame">
      <table className="heatmap">
        <thead>
          <tr>
            <th>Company</th>
            {relations.map((relation) => <th key={relation}>{shortRelation(relation)}</th>)}
          </tr>
        </thead>
        <tbody>
          {companies.map((company) => (
            <tr key={company}>
              <td>{company}</td>
              {relations.map((relation) => {
                const value = matrix.get(`${company}::${relation}`) || 0;
                return <td key={relation}><span className={`heat h${heatLevel(value, max)}`}>{value || ''}</span></td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function heatLevel(value, max) {
  if (!value) return 0;
  return Math.min(5, Math.max(1, Math.ceil((value / Math.max(max, 1)) * 5)));
}
