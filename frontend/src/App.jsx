import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { fetchDashboardData, fetchRunRegistry } from './api/data.js';
import { EvidenceDrawer } from './components/EvidenceDrawer.jsx';
import { FilterBar } from './components/FilterBar.jsx';
import { MetricStrip } from './components/MetricStrip.jsx';
import { RunSelector } from './components/RunSelector.jsx';
import { Tabs } from './components/Tabs.jsx';
import { exportCsv, filterBottlenecks, filterEdges, filterEvidence } from './lib/filters.js';
import { Bottlenecks } from './views/Bottlenecks.jsx';
import { Companies } from './views/Companies.jsx';
import { Edges } from './views/Edges.jsx';
import { Evidence } from './views/Evidence.jsx';
import { Overview } from './views/Overview.jsx';

const EMPTY_FILTERS = { query: '', company: '', relation: '', modality: '' };
const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'companies', label: 'Companies' },
  { id: 'bottlenecks', label: 'Bottlenecks' },
  { id: 'edges', label: 'Edges' },
  { id: 'evidence', label: 'Evidence' },
];

export function App() {
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [data, setData] = useState(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [activeTab, setActiveTab] = useState('overview');
  const [selectedEvidence, setSelectedEvidence] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadRuns = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const nextRuns = await fetchRunRegistry();
      setRuns(nextRuns);
      setSelectedRunId((current) => current || nextRuns[0]?.run_id || '');
      if (!nextRuns.length) setData(null);
    } catch (err) {
      setError(err.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    const run = runs.find((item) => item.run_id === selectedRunId);
    if (!run) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    fetchDashboardData(run)
      .then((payload) => {
        if (!cancelled) {
          setData(payload);
          setFilters(EMPTY_FILTERS);
          setSelectedEvidence(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runs, selectedRunId]);

  const filteredEdges = useMemo(() => filterEdges(data?.edges || [], filters), [data, filters]);
  const filteredEvidence = useMemo(() => filterEvidence(data?.evidence || [], filters), [data, filters]);
  const filteredBottlenecks = useMemo(() => filterBottlenecks(data?.bottlenecks || [], filters), [data, filters]);

  const updateFilters = (patch) => setFilters((current) => ({ ...current, ...patch }));

  if (error) {
    return (
      <div className="state-page">
        <AlertTriangle size={28} />
        <h1>Unable to load Value Chain data</h1>
        <p>{error}</p>
        <code>Run `valuechain run ...` to generate frontend/public/data artifacts.</code>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <RunSelector runs={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} onRefresh={loadRuns} />
      {loading && <div className="loading-bar" />}
      {data ? (
        <>
          <FilterBar
            data={data}
            filters={filters}
            onChange={updateFilters}
            onReset={() => setFilters(EMPTY_FILTERS)}
            onCurrentFacts={() => updateFilters({ modality: 'current_fact' })}
            onExport={() => exportCsv('filtered_edges.csv', filteredEdges)}
          />
          <main>
            <MetricStrip
              data={data}
              filteredEdges={filteredEdges}
              filteredEvidence={filteredEvidence}
              filteredBottlenecks={filteredBottlenecks}
            />
            <section className="workbench">
              <Tabs tabs={TABS} active={activeTab} onChange={setActiveTab} />
              <div className="tab-body">
                {activeTab === 'overview' && <Overview edges={filteredEdges} evidence={filteredEvidence} />}
                {activeTab === 'companies' && (
                  <Companies
                    companies={data.companies || []}
                    filters={filters}
                    onCompany={(company) => updateFilters({ company })}
                  />
                )}
                {activeTab === 'bottlenecks' && <Bottlenecks rows={filteredBottlenecks} />}
                {activeTab === 'edges' && <Edges rows={filteredEdges} />}
                {activeTab === 'evidence' && <Evidence rows={filteredEvidence} onInspect={setSelectedEvidence} />}
              </div>
            </section>
          </main>
          <EvidenceDrawer evidence={selectedEvidence} onClose={() => setSelectedEvidence(null)} />
        </>
      ) : (
        <div className="state-page">
          <h1>No runs available</h1>
          <p>Generate an industry batch first, then refresh this page.</p>
          <code>valuechain run --priority 1 --max-filings-per-company 2 --skip-yahoo</code>
        </div>
      )}
    </div>
  );
}
