import { useCallback, useEffect, useMemo, useState } from 'react';
import { Database, Download, ExternalLink, FileText, RefreshCcw, Search, ShieldCheck } from 'lucide-react';
import {
  fetchAcquisitionDocumentBlob,
  fetchAcquisitionFilingDetail,
  fetchAcquisitionFilings,
  fetchAcquisitionSources,
} from '../api/data.js';
import { truncate } from '../components/format.js';
import { IssuerSearch } from '../components/IssuerSearch.jsx';

const TOKEN_STORAGE_KEY = 'valuechain.fileApiToken';
const DEFAULT_FILTERS = {
  source_id: '',
  issuer_id: '',
  year: new Date().getFullYear(),
  q: '',
  form: '',
  status: '',
};

function formatInteger(value) {
  return Number(value || 0).toLocaleString();
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!size) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const exponent = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  return `${(size / 1024 ** exponent).toFixed(exponent ? 1 : 0)} ${units[exponent]}`;
}

function normalizeDate(value) {
  return value ? String(value).slice(0, 10) : '';
}

function sourceLabel(source) {
  if (!source) return 'All sources';
  return String(source.authority || source.source_id || '').replaceAll('_', ' ');
}

export function Filings() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) || '');
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [sources, setSources] = useState([]);
  const [selectedIssuer, setSelectedIssuer] = useState(null);
  const [filings, setFilings] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');

  const completeSources = useMemo(
    () => sources.filter((item) => Number(item.complete_filings || 0) > 0 || Number(item.documents || 0) > 0),
    [sources],
  );

  const loadSources = useCallback(async () => {
    if (!token.trim()) return;
    try {
      const payload = await fetchAcquisitionSources(token);
      setSources(Array.isArray(payload.items) ? payload.items : []);
    } catch (err) {
      setError(err.message);
    }
  }, [token]);

  const loadFilings = useCallback(async () => {
    if (!token.trim()) {
      setError('Enter the file API token before querying the acquisition library.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, token.trim());
      const payload = await fetchAcquisitionFilings(filters, token);
      const rows = Array.isArray(payload.items) ? payload.items : [];
      setFilings(rows);
      setSelected(rows[0] || null);
    } catch (err) {
      setError(err.message);
      setFilings([]);
      setSelected(null);
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, [filters, token]);

  useEffect(() => {
    loadSources();
  }, [loadSources]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setError('');
    fetchAcquisitionFilingDetail(selected.source_id, selected.source_filing_id, token)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch((err) => {
        if (!cancelled) {
          setDetail(null);
          setError(err.message);
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, token]);

  const updateFilter = (key, value) => setFilters((current) => ({ ...current, [key]: value }));
  const updateSource = (value) => {
    setSelectedIssuer(null);
    setFilters((current) => ({ ...current, source_id: value, issuer_id: '' }));
  };
  const updateIssuer = (issuer) => {
    setSelectedIssuer(issuer);
    setFilters((current) => ({
      ...current,
      source_id: issuer?.source_id || current.source_id,
      issuer_id: issuer?.source_issuer_id || '',
    }));
  };

  const openDocument = async (fileRecord) => {
    try {
      const { blob, filename } = await fetchAcquisitionDocumentBlob(fileRecord.document_id, token);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noreferrer';
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="filing-browser">
      <section className="filing-toolbar">
        <div className="filing-token">
          <ShieldCheck size={18} />
          <label>
            <span>File API token</span>
            <input
              type="password"
              value={token}
              placeholder="Required on Cosmos deployment"
              onChange={(event) => setToken(event.target.value)}
            />
          </label>
        </div>
        <label>
          <span>Source</span>
          <select value={filters.source_id} onChange={(event) => updateSource(event.target.value)}>
            <option value="">All sources</option>
            {sources.map((source) => (
              <option key={source.source_id} value={source.source_id}>
                {source.source_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Year</span>
          <input
            type="number"
            min="1990"
            max="2100"
            value={filters.year}
            onChange={(event) => updateFilter('year', event.target.value)}
          />
        </label>
        <label>
          <span>Native form</span>
          <input value={filters.form} placeholder="10-K, annual_report..." onChange={(event) => updateFilter('form', event.target.value)} />
        </label>
        <label>
          <span>Status</span>
          <select value={filters.status} onChange={(event) => updateFilter('status', event.target.value)}>
            <option value="">Any</option>
            <option value="complete">Complete</option>
            <option value="pending">Pending</option>
            <option value="failed">Failed</option>
          </select>
        </label>
        <IssuerSearch token={token} sourceId={filters.source_id} selectedIssuer={selectedIssuer} onSelect={updateIssuer} />
        <label className="filing-query">
          <span>Filing id / free text</span>
          <div className="input-with-icon">
            <Search size={16} />
            <input value={filters.q} placeholder="Accession, source filing id, form..." onChange={(event) => updateFilter('q', event.target.value)} />
          </div>
        </label>
        <button onClick={loadFilings} disabled={loading}>
          <RefreshCcw size={16} />
          Query
        </button>
      </section>

      {error && <div className="inline-alert">{error}</div>}

      <section className="source-strip">
        {completeSources.slice(0, 8).map((source) => (
          <div className="source-tile" key={source.source_id}>
            <span>{source.source_id}</span>
            <strong>{formatInteger(source.complete_filings)}</strong>
            <small>
              {formatInteger(source.documents)} docs / {formatBytes(source.document_bytes)}
            </small>
          </div>
        ))}
        {!completeSources.length && (
          <div className="source-tile empty">
            <Database size={18} />
            <span>Enter token to load source coverage</span>
          </div>
        )}
      </section>

      <section className="filing-layout">
        <div className="filing-results panel">
          <div className="panel-head">
            <div>
              <h2>Filing inventory</h2>
              <span>{loading ? 'Loading...' : `${formatInteger(filings.length)} rows shown`}</span>
            </div>
          </div>
          <div className="table-frame filing-table">
            <table>
              <thead>
                <tr>
                  <th>Filed</th>
                  <th>Company</th>
                  <th>Source</th>
                  <th>Form</th>
                  <th>Documents</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {filings.map((row) => (
                  <tr
                    key={`${row.source_id}:${row.source_filing_id}`}
                    className={selected?.source_id === row.source_id && selected?.source_filing_id === row.source_filing_id ? 'selected-row' : ''}
                    onClick={() => setSelected(row)}
                  >
                    <td>{normalizeDate(row.filing_date)}</td>
                    <td>
                      <strong>{row.company_name || row.ticker || row.source_issuer_id}</strong>
                      <small>{row.ticker || row.source_issuer_id}</small>
                    </td>
                    <td>{row.source_id}</td>
                    <td>
                      <span className="pill">{row.canonical_document_type || row.form_raw}</span>
                      <small>{row.form_raw}</small>
                    </td>
                    <td>
                      {formatInteger(row.document_count)}
                      <small>{formatBytes(row.document_bytes)}</small>
                    </td>
                    <td>{row.status}</td>
                  </tr>
                ))}
                {!filings.length && (
                  <tr>
                    <td colSpan="6" className="muted">
                      Run a query to inspect locally stored filings.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <aside className="filing-detail panel">
          <div className="panel-head">
            <div>
              <h2>Filing detail</h2>
              <span>{selected ? `${sourceLabel(selected)} / ${selected.source_filing_id}` : 'No filing selected'}</span>
            </div>
          </div>
          {selected && (
            <div className="filing-meta">
              <div>
                <span>Company</span>
                <strong>{selected.company_name}</strong>
              </div>
              <div>
                <span>Native form</span>
                <strong>{selected.form_raw}</strong>
              </div>
              <div>
                <span>Canonical type</span>
                <strong>{selected.canonical_document_type}</strong>
              </div>
              <div>
                <span>Filed</span>
                <strong>{normalizeDate(selected.filing_date)}</strong>
              </div>
            </div>
          )}
          {detailLoading && <div className="brief-loading">Loading documents...</div>}
          {detail?.filing?.archive_url && (
            <a className="source-link" href={detail.filing.archive_url} target="_blank" rel="noreferrer">
              <ExternalLink size={14} />
              Source archive
            </a>
          )}
          <div className="document-list">
            {(detail?.documents || []).map((document) => (
              <div className="document-row" key={document.document_id}>
                <FileText size={18} />
                <div>
                  <strong>{document.document_kind || document.content_type || 'document'}</strong>
                  <span>{truncate(document.source_url || document.sha256 || '', 90)}</span>
                  <small>
                    {formatBytes(document.byte_size)} / {document.status}
                  </small>
                </div>
                <button className="icon-button" title="Open or download document" onClick={() => openDocument(document)}>
                  <Download size={16} />
                </button>
              </div>
            ))}
            {selected && !detailLoading && !(detail?.documents || []).length && (
              <div className="muted">No completed document is attached to this filing yet.</div>
            )}
          </div>
        </aside>
      </section>
    </div>
  );
}
