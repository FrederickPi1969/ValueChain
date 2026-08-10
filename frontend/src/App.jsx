import { useEffect, useState } from 'react';
import { Database, LogOut } from 'lucide-react';
import { AccessGate } from './components/AccessGate.jsx';
import { IssuerDirectory } from './components/IssuerDirectory.jsx';
import { SourceCoverage } from './components/SourceCoverage.jsx';
import { Filings } from './views/Filings.jsx';
import { TopologyWorkspace } from './views/TopologyWorkspace.jsx';

const VIEWS = [
  { id: 'filings', label: 'Filing library' },
  { id: 'companies', label: 'Company directory' },
  { id: 'coverage', label: 'Archive coverage' },
  { id: 'topology', label: 'Value-chain topology' },
];

function viewFromLocation() {
  const requested = window.location.hash.replace('#', '');
  return VIEWS.some((view) => view.id === requested) ? requested : 'filings';
}

export function App() {
  const [session, setSession] = useState(null);
  const [view, setView] = useState(viewFromLocation);
  const [requestedIssuer, setRequestedIssuer] = useState(null);

  useEffect(() => {
    const syncView = () => setView(viewFromLocation());
    window.addEventListener('hashchange', syncView);
    return () => window.removeEventListener('hashchange', syncView);
  }, []);

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

  if (!session) return <AccessGate onConnect={enterLibrary} />;

  return (
    <div className="app-shell library-shell">
      <header className="site-header">
        <button className="brand-button" onClick={() => navigate('filings')}>
          <Database size={21} />
          <span>Fin Intelligence</span>
        </button>
        <nav className="site-nav" aria-label="Library navigation">
          {VIEWS.map((item) => (
            <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => navigate(item.id)}>{item.label}</button>
          ))}
        </nav>
        <button className="signout-button" title="Forget this browser's token" onClick={signOut}>
          <LogOut size={16} />
          Sign out
        </button>
      </header>
      <main className="library-main">
        {view === 'filings' && <Filings token={session.token} initialSources={session.sources} requestedIssuer={requestedIssuer} />}
        {view === 'companies' && <IssuerDirectory token={session.token} onOpenIssuer={openIssuer} />}
        {view === 'coverage' && <SourceCoverage sources={session.sources} />}
        {view === 'topology' && <TopologyWorkspace />}
      </main>
    </div>
  );
}
