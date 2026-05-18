export function MetricStrip({ data, filteredCompanies, filteredEdges, filteredEvidence, filteredBottlenecks }) {
  const totalEdges = data?.summary?.edge_count || 0;
  const totalCompanies = data?.summary?.company_count || 0;
  const activeCompanies = data?.summary?.active_company_count || new Set((data?.edges || []).map((edge) => edge.subject)).size;
  const companyCount = filteredCompanies?.length ?? new Set(filteredEdges.map((edge) => edge.subject)).size;
  const currentEvidence = filteredEvidence.filter((row) => row.modality === 'current_fact').length;
  const riskEvidence = filteredEvidence.filter((row) => row.modality === 'risk_hypothetical').length;

  return (
    <section className="metrics">
      <Metric label="Companies" value={companyCount} detail={`${activeCompanies} active / ${totalCompanies} total`} />
      <Metric label="Edges" value={filteredEdges.length} detail={`of ${totalEdges} total`} />
      <Metric label="Current Evidence" value={currentEvidence} detail="operating dependency signal" />
      <Metric label="Risk Evidence" value={riskEvidence} detail="hypothetical/risk signal" />
      <Metric label="Shared Bottlenecks" value={filteredBottlenecks.length} detail="repeated dependency objects" />
    </section>
  );
}

function Metric({ label, value, detail }) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}
