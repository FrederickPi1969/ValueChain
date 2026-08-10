import { useState } from 'react';
import { ArrowRight, Database, KeyRound } from 'lucide-react';
import { fetchAcquisitionSources } from '../api/data.js';

const TOKEN_STORAGE_KEY = 'valuechain.fileApiToken';

export function AccessGate({ onConnect }) {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const connect = async (event) => {
    event.preventDefault();
    const value = token.trim();
    if (!value) {
      setError('Enter the File API token to access the private archive.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const payload = await fetchAcquisitionSources(value);
      localStorage.setItem(TOKEN_STORAGE_KEY, value);
      onConnect({ token: value, sources: Array.isArray(payload.items) ? payload.items : [] });
    } catch (requestError) {
      setError('The archive could not verify this token. Check the token and try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="access-page">
      <section className="access-card">
        <div className="access-mark"><Database size={24} /></div>
        <span className="eyebrow">Fin Intelligence</span>
        <h1>Global disclosure library</h1>
        <p>Search the collected filings by company, market, period, and report type. Original regulator files remain available for review.</p>
        <form onSubmit={connect}>
          <label>
            <span>File API token</span>
            <div className="access-input">
              <KeyRound size={17} />
              <input
                type="password"
                value={token}
                placeholder="Paste your archive token"
                onChange={(event) => setToken(event.target.value)}
                autoFocus
              />
            </div>
          </label>
          {error && <div className="access-error">{error}</div>}
          <button className="primary-button access-submit" disabled={loading} type="submit">
            {loading ? 'Verifying access...' : 'Enter filing library'}
            <ArrowRight size={17} />
          </button>
        </form>
        <small>The token is saved only in this browser and is sent only to the Fin Intelligence service.</small>
      </section>
    </main>
  );
}
