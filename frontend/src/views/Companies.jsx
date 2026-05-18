export function Companies({ companies, onCompany }) {
  const rows = companies
    .slice()
    .sort(
      (a, b) =>
        Number(a.priority || 999) - Number(b.priority || 999) ||
        Number(b.evidence_count) - Number(a.evidence_count) ||
        String(a.company).localeCompare(String(b.company))
    );

  return (
    <section className="panel wide">
      <div className="panel-head">
        <h2>Portfolio Exposure</h2>
        <span>{rows.length} rows</span>
      </div>
      <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Ticker</th>
              <th>Role</th>
              <th>Edges</th>
              <th>Evidence</th>
              <th>Current</th>
              <th>Risk</th>
              <th>Relation Breadth</th>
              <th>Avg Confidence</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.company}>
                <td><button className="link-button" onClick={() => onCompany(row.company)}>{row.company}</button></td>
                <td>{row.ticker}</td>
                <td>{row.role}</td>
                <td>{row.edge_count}</td>
                <td>{row.evidence_count}</td>
                <td>{row.current_evidence_count}</td>
                <td>{row.risk_evidence_count}</td>
                <td>{row.relation_type_count}</td>
                <td>{row.avg_confidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
