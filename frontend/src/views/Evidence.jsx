import { modalityClass, truncate } from '../components/format.js';

export function Evidence({ rows, onInspect }) {
  return (
    <section className="panel wide">
      <div className="panel-head">
        <h2>Evidence Inspector</h2>
        <span>{rows.length} rows</span>
      </div>
      <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Relation</th>
              <th>Object</th>
              <th>Modality</th>
              <th>Filing</th>
              <th>Section</th>
              <th>Evidence</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 900).map((row, index) => (
              <tr key={`${row.passage_id}-${row.relation_type}-${index}`}>
                <td>{row.subject}</td>
                <td>{row.relation_type}</td>
                <td>{row.object}</td>
                <td><span className={`pill ${modalityClass(row.modality)}`}>{row.modality}</span></td>
                <td>{row.form} {row.filing_date}</td>
                <td>{row.source_section}</td>
                <td className="evidence-preview">{truncate(row.evidence_text)}</td>
                <td><button onClick={() => onInspect(row)}>Inspect</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
