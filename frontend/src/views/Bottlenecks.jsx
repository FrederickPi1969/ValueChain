export function Bottlenecks({ rows }) {
  return (
    <section className="panel wide">
      <div className="panel-head">
        <h2>Bottleneck Candidates</h2>
        <span>{rows.length} rows</span>
      </div>
      <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>Dependency Object</th>
              <th>Dependent Companies</th>
              <th>Evidence</th>
              <th>Relation Types</th>
              <th>Subjects</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 500).map((row) => (
              <tr key={`${row.object}-${row.relation_types}`}>
                <td>{row.object}</td>
                <td>{row.dependent_company_count}</td>
                <td>{row.evidence_count}</td>
                <td>{row.relation_types}</td>
                <td>{row.subjects}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
