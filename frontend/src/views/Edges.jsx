import { ExternalLink } from 'lucide-react';
import { modalityClass } from '../components/format.js';

export function Edges({ rows }) {
  return (
    <section className="panel wide">
      <div className="panel-head">
        <h2>Aggregated Edges</h2>
        <span>{rows.length} rows</span>
      </div>
      <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>Subject</th>
              <th>Object</th>
              <th>Relation</th>
              <th>Modality</th>
              <th>Evidence</th>
              <th>Confidence</th>
              <th>Window</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 700).map((edge) => (
              <tr key={`${edge.subject}-${edge.object}-${edge.relation_type}-${edge.modality}`}>
                <td>{edge.subject}</td>
                <td>{edge.object}</td>
                <td>{edge.relation_type}</td>
                <td><span className={`pill ${modalityClass(edge.modality)}`}>{edge.modality}</span></td>
                <td>{edge.evidence_count}</td>
                <td>{edge.avg_confidence}</td>
                <td>{edge.first_seen} to {edge.last_seen}</td>
                <td>{sourceLinks(edge.source_urls)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function sourceLinks(value) {
  return String(value || '').split(';').filter(Boolean).slice(0, 2).map((url, index) => (
    <a key={url} className="source-link" href={url} target="_blank" rel="noreferrer">
      SEC {index + 1} <ExternalLink size={12} />
    </a>
  ));
}
