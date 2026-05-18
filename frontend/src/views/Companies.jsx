export function Companies({ companies, filters, onCompany }) {
  const rows = companies
    .filter((row) => !filters.company || row.company === filters.company)
    .filter((row) => !filters.query || Object.values(row).join(' ').toLowerCase().includes(filters.query.toLowerCase()))
    .sort((a, b) => Number(b.evidence_count) - Number(a.evidence_count));

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
