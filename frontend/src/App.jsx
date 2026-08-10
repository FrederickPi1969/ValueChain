import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Database, LogOut } from 'lucide-react';
import {
  fetchCompanyBrief,
  fetchCompanyBriefIndex,
  fetchDashboardData,
  fetchResolutionRecords,
  fetchRunRegistry,
} from './api/data.js';
import { AccessGate } from './components/AccessGate.jsx';
import { EvidenceDrawer } from './components/EvidenceDrawer.jsx';
import { FilterBar } from './components/FilterBar.jsx';
import { IssuerDirectory } from './components/IssuerDirectory.jsx';
import { MetricStrip } from './components/MetricStrip.jsx';
import { RunSelector } from './components/RunSelector.jsx';
import { SourceCoverage } from './components/SourceCoverage.jsx';
import { Tabs } from './components/Tabs.jsx';
import { briefTickerSet, matchBriefForCompany } from './lib/briefs.js';
import { exportCsv, filterBottlenecks, filterCompanies, filterEdges, filterEvidence } from './lib/filters.js';
import { Briefs } from './views/Briefs.jsx';
import { Bottlenecks } from './views/Bottlenecks.jsx';
import { Companies } from './views/Companies.jsx';
import { Edges } from './views/Edges.jsx';
import { Evidence } from './views/Evidence.jsx';
import { Filings } from './views/Filings.jsx';
import { Network } from './views/Network.jsx';
import { Overview } from './views/Overview.jsx';
import { Resolution } from './views/Resolution.jsx';
import { TopologyWorkspace } from './views/TopologyWorkspace.jsx';

const EMPTY_FILTERS = {
  query: '',
  company: '',
  relation: '',
  modality: '',
  relationshipFamilies: ['supply_chain'],
  relationshipStatuses: ['confirmed', 'candidate'],
};

const SITE_VIEWS = [
  { id: 'filings', label: 'Filing library' },
  { id: 'directory', label: 'Company directory' },
  { id: 'coverage', label: 'Archive coverage' },
  { id: 'topology', label: 'Value-chain topology' },
  { id: 'analysis', label: 'Extraction analysis' },
];

const ANALYSIS_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'network', label: 'Network map' },
  { id: 'resolution', label: 'Resolution review' },
  { id: 'companies', label: 'Extracted companies' },
  { id: 'briefs', label: 'Briefs' },
  { id: 'bottlenecks', label: 'Bottlenecks' },
  { id: 'edges', label: 'Edges' },
  { id: 'evidence', label: 'Evidence' },
];

function viewFromLocation() {
  const requested = window.location.hash.replace('#', '');
  return SITE_VIEWS.some((view) => view.id === requested) ? requested : 'filings';
}

export function App() {
  const [session, setSession] = useState(null);
  const [view, setView] = useState(viewFromLocation);
  const [requestedIssuer, setRequestedIssuer] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [data, setData] = useState(null);
  const [resolutionRecords, setResolutionRecords] = useState([]);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [activeAnalysisTab, setActiveAnalysisTab] = useState('overview');
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
    } catch (requestError) {
      setError(requestError.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const syncView = () => setView(viewFromLocation());
    window.addEventListener('hashchange', syncView);
    return () => window.removeEventListener('hashchange', syncView);
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
      .catch((requestError) => {
        if (!cancelled) setError(requestError.message);
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
    fetchResolutionRecords(run).then(setResolutionRecords).catch(() => setResolutionRecords([]));
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
      .catch((requestError) => {
        if (!cancelled) setBriefError(requestError.message);
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
      .catch((requestError) => {
        if (!cancelled) {
          setSelectedBrief(null);
          setBriefError(requestError.message);
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

  const navigate = (nextView) => {
    if (nextView === view) return;
    window.location.hash = nextView;
  };

  const enterLibrary = (nextSession) => {
    setSession(nextSession);
    window.history.replaceState(null, '', `${window.location.pathname}#filings`);
    setView('filings');
  };

  const signOut = () => {
    localStorage.removeItem('valuechain.fileApiToken');
    setSession(null);
    setRequestedIssuer(null);
    window.history.replaceState(null, '', window.location.pathname);
  };

  const openIssuer = (issuer) => {
    setRequestedIssuer({ ...issuer, requestId: Date.now() });
    navigate('filings');
  };

  const updateFilters = (patch) => setFilters((current) => ({ ...current, ...patch }));
  const openCompanyBrief = (company) => {
    const entry = matchBriefForCompany(company, briefIndex);
    if (entry) setSelectedBriefTicker(entry.ticker);
    setActiveAnalysisTab('briefs');
  };
  const focusNetworkNode = (company) => {
    updateFilters({ query: company, company: '' });
    setActiveAnalysisTab('edges');
  };

  if (!session) return <AccessGate onConnect={enterLibrary} />;

  return (
    <div className="app-shell library-shell">
      <header className="site-header">
        <button className="brand-button" onClick={() => navigate('filings')}>
          <Database size={21} />
          <span>Fin Intelligence</span>
        </button>
        <nav className="site-nav" aria-label="Fin Intelligence navigation">
          {SITE_VIEWS.map((item) => (
            <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => navigate(item.id)}>
              {item.label}
            </button>
          ))}
        </nav>
        <button className="signout-button" title="Forget this browser's token" onClick={signOut}>
          <LogOut size={16} />
          Sign out
        </button>
      </header>

      <main className={view === 'analysis' ? 'analysis-main' : 'library-main'}>
        {view === 'filings' && (
          <Filings token={session.token} initialSources={session.sources} requestedIssuer={requestedIssuer} />
        )}
        {view === 'directory' && <IssuerDirectory token={session.token} onOpenIssuer={openIssuer} />}
        {view === 'coverage' && <SourceCoverage sources={session.sources} />}
        {view === 'topology' && <TopologyWorkspace />}

        {view === 'analysis' && (
          <section className="analysis-workspace">
            <RunSelector runs={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} onRefresh={loadRuns} />
            {loading && <div className="loading-bar" />}
            {data && (
              <FilterBar
                data={data}
                filters={filters}
                onChange={updateFilters}
                onReset={() => setFilters(EMPTY_FILTERS)}
                onCurrentFacts={() => updateFilters({ modality: 'current_fact' })}
                onExport={() => exportCsv('filtered_edges.csv', filteredEdges)}
              />
            )}
            <div className="analysis-content">
              {data && (
                <MetricStrip
                  data={data}
                  filteredCompanies={filteredCompanies}
                  filteredEdges={filteredEdges}
                  filteredEvidence={filteredEvidence}
                  filteredBottlenecks={filteredBottlenecks}
                />
              )}
              <section className="workbench">
                <Tabs tabs={ANALYSIS_TABS} active={activeAnalysisTab} onChange={setActiveAnalysisTab} />
                <div className="tab-body">
                  {data && activeAnalysisTab === 'overview' && <Overview edges={filteredEdges} evidence={filteredEvidence} />}
                  {data && activeAnalysisTab === 'network' && (
                    <Network
                      edges={filterEdges(data.network_edges || [], filters)}
                      allEdges={data.network_edges || []}
                      lineageEvents={data.relationship_lineage_events || []}
                      companies={data.companies || []}
                      onFocus={focusNetworkNode}
                    />
                  )}
                  {data && activeAnalysisTab === 'resolution' && (
                    <Resolution
                      data={data}
                      runId={selectedRunId}
                      relationshipStatuses={filters.relationshipStatuses}
                      resolutionRecords={resolutionRecords}
                    />
                  )}
                  {data && activeAnalysisTab === 'companies' && (
                    <Companies
                      companies={filteredCompanies}
                      onCompany={(company) => updateFilters({ company })}
                      onBrief={openCompanyBrief}
                      briefTickers={availableBriefTickers}
                    />
                  )}
                  {activeAnalysisTab === 'briefs' && (
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
                  {data && activeAnalysisTab === 'bottlenecks' && <Bottlenecks rows={filteredBottlenecks} />}
                  {data && activeAnalysisTab === 'edges' && <Edges rows={filteredEdges} />}
                  {data && activeAnalysisTab === 'evidence' && (
                    <Evidence rows={filteredEvidence} onInspect={setSelectedEvidence} />
                  )}
                  {!data && (
                    <div className="state-page embedded">
                      <AlertTriangle size={24} />
                      <h1>{error ? 'Unable to load extraction data' : 'No runs available'}</h1>
                      <p>{error || 'Generate an industry batch, then refresh this page.'}</p>
                      <code>valuechain run --priority 1 --max-filings-per-company 2 --skip-yahoo</code>
                    </div>
                  )}
                </div>
              </section>
            </div>
          </section>
        )}
      </main>

      <EvidenceDrawer evidence={selectedEvidence} onClose={() => setSelectedEvidence(null)} />
    </div>
  );
}
