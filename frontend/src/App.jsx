import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { fetchCompanyBrief, fetchCompanyBriefIndex, fetchDashboardData, fetchRunRegistry } from './api/data.js';
import { EvidenceDrawer } from './components/EvidenceDrawer.jsx';
import { FilterBar } from './components/FilterBar.jsx';
import { MetricStrip } from './components/MetricStrip.jsx';
import { RunSelector } from './components/RunSelector.jsx';
import { Tabs } from './components/Tabs.jsx';
import { briefTickerSet, matchBriefForCompany } from './lib/briefs.js';
import { exportCsv, filterBottlenecks, filterCompanies, filterEdges, filterEvidence } from './lib/filters.js';
import { Briefs } from './views/Briefs.jsx';
import { Bottlenecks } from './views/Bottlenecks.jsx';
import { Companies } from './views/Companies.jsx';
import { Edges } from './views/Edges.jsx';
import { Evidence } from './views/Evidence.jsx';
import { Filings } from './views/Filings.jsx';
import { Overview } from './views/Overview.jsx';

const EMPTY_FILTERS = { query: '', company: '', relation: '', modality: '' };
const TABS = [
  { id: 'filings', label: 'Filing Library' },
  { id: 'overview', label: 'Overview' },
  { id: 'companies', label: 'Companies' },
  { id: 'briefs', label: 'Briefs' },
  { id: 'bottlenecks', label: 'Bottlenecks' },
  { id: 'edges', label: 'Edges' },
  { id: 'evidence', label: 'Evidence' },
];

export function App() {
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [data, setData] = useState(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [activeTab, setActiveTab] = useState('filings');
  const [selectedEvidence, setSelectedEvidence] = useState(null);
  const [briefIndex, setBriefIndex] = useState([]);
  const [selectedBriefTicker, setSelectedBriefTicker] = useState('');
  const [selectedBrief, setSelectedBrief] = useState(null);
  const [briefLoading, setBriefLoading] = useState(false);
  const [briefError, setBriefError] = useState('');
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

  useEffect(() => {
    const run = runs.find((item) => item.run_id === selectedRunId);
    if (!run) return;
    let cancelled = false;
    setBriefIndex([]);
    setSelectedBriefTicker('');
    setSelectedBrief(null);
    setBriefError('');
    fetchCompanyBriefIndex(run)
      .then((rows) => {
        if (!cancelled) {
          setBriefIndex(rows);
          setSelectedBriefTicker(rows[0]?.ticker || '');
        }
      })
      .catch((err) => {
        if (!cancelled) setBriefError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [runs, selectedRunId]);

  useEffect(() => {
    const run = runs.find((item) => item.run_id === selectedRunId);
    const entry = briefIndex.find((item) => item.ticker === selectedBriefTicker);
    if (!run || !entry) {
      setSelectedBrief(null);
      setBriefLoading(false);
      return;
    }
    let cancelled = false;
    setBriefLoading(true);
    setBriefError('');
    fetchCompanyBrief(run, entry)
      .then((payload) => {
        if (!cancelled) setSelectedBrief(payload);
      })
      .catch((err) => {
        if (!cancelled) {
          setSelectedBrief(null);
          setBriefError(err.message);
        }
      })
      .finally(() => {
        if (!cancelled) setBriefLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runs, selectedRunId, briefIndex, selectedBriefTicker]);

  const filteredEdges = useMemo(() => filterEdges(data?.edges || [], filters), [data, filters]);
  const filteredEvidence = useMemo(() => filterEvidence(data?.evidence || [], filters), [data, filters]);
  const filteredBottlenecks = useMemo(() => filterBottlenecks(data?.bottlenecks || [], filters), [data, filters]);
  const filteredCompanies = useMemo(() => filterCompanies(data?.companies || [], filters), [data, filters]);
  const availableBriefTickers = useMemo(() => briefTickerSet(briefIndex), [briefIndex]);

  const updateFilters = (patch) => setFilters((current) => ({ ...current, ...patch }));
  const openCompanyBrief = (company) => {
    const entry = matchBriefForCompany(company, briefIndex);
    if (entry) setSelectedBriefTicker(entry.ticker);
    setActiveTab('briefs');
  };

  if (error && activeTab !== 'filings') {
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
      {data || activeTab === 'filings' ? (
        <>
          {data && activeTab !== 'filings' && (
            <FilterBar
              data={data}
              filters={filters}
              onChange={updateFilters}
              onReset={() => setFilters(EMPTY_FILTERS)}
              onCurrentFacts={() => updateFilters({ modality: 'current_fact' })}
              onExport={() => exportCsv('filtered_edges.csv', filteredEdges)}
            />
          )}
          <main>
            {data && activeTab !== 'filings' && (
              <MetricStrip
                data={data}
                filteredCompanies={filteredCompanies}
                filteredEdges={filteredEdges}
                filteredEvidence={filteredEvidence}
                filteredBottlenecks={filteredBottlenecks}
              />
            )}
            <section className="workbench">
              <Tabs tabs={TABS} active={activeTab} onChange={setActiveTab} />
              <div className="tab-body">
                {activeTab === 'filings' && <Filings />}
                {activeTab === 'overview' && data && <Overview edges={filteredEdges} evidence={filteredEvidence} />}
                {activeTab === 'companies' && (
                  <Companies
                    companies={filteredCompanies}
                    onCompany={(company) => updateFilters({ company })}
                    onBrief={openCompanyBrief}
                    briefTickers={availableBriefTickers}
                  />
                )}
                {activeTab === 'briefs' && (
                  <Briefs
                    entries={briefIndex}
                    brief={selectedBrief}
                    loading={briefLoading}
                    error={briefError}
                    selectedTicker={selectedBriefTicker}
                    onSelectTicker={setSelectedBriefTicker}
                    onCompanyFilter={(company) => updateFilters({ company })}
                  />
                )}
                {activeTab === 'bottlenecks' && data && <Bottlenecks rows={filteredBottlenecks} />}
                {activeTab === 'edges' && data && <Edges rows={filteredEdges} />}
                {activeTab === 'evidence' && data && <Evidence rows={filteredEvidence} onInspect={setSelectedEvidence} />}
                {activeTab !== 'filings' && !data && (
                  <div className="state-page embedded">
                    <AlertTriangle size={24} />
                    <h1>Dashboard data is not loaded</h1>
                    <p>{error || 'Generate an extraction run or switch back to Filing Library.'}</p>
                  </div>
                )}
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
