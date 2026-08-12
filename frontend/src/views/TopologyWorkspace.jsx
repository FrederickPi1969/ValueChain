import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { fetchDashboardData, fetchRunRegistry } from '../api/data.js';
import { TopologyMap } from './TopologyMap.jsx';

export function TopologyWorkspace() {
  const [runs, setRuns] = useState([]);
  const [runId, setRunId] = useState('');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadRuns = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const nextRuns = await fetchRunRegistry();
      setRuns(nextRuns);
      setRunId((current) => nextRuns.some((row) => row.run_id === current) ? current : nextRuns[0]?.run_id || '');
      if (!nextRuns.length) setData(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadRuns(); }, [loadRuns]);
  useEffect(() => {
    const run = runs.find((row) => row.run_id === runId);
    if (!run) return;
    let cancelled = false;
    setLoading(true);
    setData(null);
    setError('');
    fetchDashboardData(run)
      .then((payload) => { if (!cancelled) { setData(payload); setError(''); } })
      .catch((err) => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runs, runId]);

  return <section className="topology-workspace">
    <div className="topology-runbar panel">
      <div><h1>Value-chain topology</h1><p>Expand filing-grounded company relationships from multiple industry seeds.</p></div>
      {runs.length > 1 && <label><span>Extraction run</span><select value={runId} onChange={(event) => setRunId(event.target.value)}>{runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_label || run.run_id}</option>)}</select></label>}
      <button type="button" onClick={loadRuns}><RefreshCw size={15} /> Refresh</button>
    </div>
    {loading && !data && <div className="topology-state panel">Loading relationship graph…</div>}
    {error && <div className="topology-state topology-error panel"><AlertTriangle size={18} /> {error}</div>}
    {data && <TopologyMap runId={runId} edges={data.edges || []} networkEdges={data.network_edges || []} companies={data.companies || []} evidence={data.evidence || []} lineageEvents={data.relationship_lineage_events || []} industryExpansion={data.industry_expansion || {}} />}
    {!loading && !error && !data && <div className="topology-state panel">No extracted relationship run is available.</div>}
  </section>;
}
