import { useCallback, useEffect, useMemo, useState } from 'react';
import { Database, Download, ExternalLink, FileText, Search, SlidersHorizontal } from 'lucide-react';
import {
  fetchAcquisitionDocumentBlob,
  fetchAcquisitionFilingDetail,
  fetchAcquisitionFilings,
  fetchAcquisitionSources,
} from '../api/data.js';
import { truncate } from '../components/format.js';
import { IssuerSearch } from '../components/IssuerSearch.jsx';
import {
  DEFAULT_FILING_FILTERS,
  FORM_PRESETS,
  filingQueryForIssuer,
  formatFilingType,
  sourceDisplayName,
} from '../lib/filingSearch.js';

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

export function Filings({ token, initialSources = [], requestedIssuer }) {
  const [filters, setFilters] = useState(DEFAULT_FILING_FILTERS);
  const [sources, setSources] = useState(initialSources);
  const [selectedIssuer, setSelectedIssuer] = useState(null);
  const [filings, setFilings] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);

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

  const loadFilings = useCallback(async (queryFilters = filters) => {
    setLoading(true);
    setError('');
    try {
      const payload = await fetchAcquisitionFilings(queryFilters, token);
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
    loadFilings();
  // Load a useful, recent inventory as soon as an existing saved connection is available.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!requestedIssuer) return;
    const nextFilters = filingQueryForIssuer(requestedIssuer, filters);
    setSelectedIssuer(requestedIssuer);
    setFilters(nextFilters);
    loadFilings(nextFilters);
  // A directory selection is an explicit query intent; use its one-time request id as the trigger.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedIssuer?.requestId]);

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
    const nextFilters = filingQueryForIssuer(issuer, filters);
    setFilters(nextFilters);
    loadFilings(nextFilters);
  };

  const clearIssuer = () => {
    setSelectedIssuer(null);
    setFilters((current) => ({ ...current, issuer_id: '' }));
  };

  const useFormPreset = (document_type) => setFilters((current) => ({ ...current, document_type }));

  const resetSearch = () => {
    setSelectedIssuer(null);
    setFilters(DEFAULT_FILING_FILTERS);
    setSelected(null);
    setDetail(null);
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
      <section className="library-hero">
        <div>
          <span className="eyebrow">Global disclosure archive</span>
          <h2>Find a company, then read the original filing.</h2>
          <p>Search by company name, ticker, or local issuer code. The archive keeps each regulator's native report alongside a comparable report type.</p>
        </div>
        <div className="library-hero-actions">
          <button className="quiet-button" onClick={() => setShowAdvanced((current) => !current)}>
            <SlidersHorizontal size={16} />
            More filters
          </button>
        </div>
      </section>

      <section className="company-search-panel">
        <div className="search-heading">
          <span className="eyebrow">Start here</span>
          <h3>Which company are you researching?</h3>
        </div>
        <IssuerSearch token={token} sourceId={filters.source_id} selectedIssuer={selectedIssuer} onSelect={updateIssuer} />
        <button className="primary-button" onClick={() => loadFilings()} disabled={loading}>
          <Search size={16} />
          {selectedIssuer ? `Open ${selectedIssuer.company_name || selectedIssuer.ticker}` : 'Browse latest filings'}
        </button>
        {selectedIssuer && (
          <button className="text-button" onClick={clearIssuer}>Clear company</button>
        )}
      </section>

      <section className="filter-row" aria-label="Filing filters">
        <label>
          <span>Market</span>
          <select value={filters.source_id} onChange={(event) => updateSource(event.target.value)}>
            <option value="">All markets</option>
            {sources.map((source) => (
              <option key={source.source_id} value={source.source_id}>
                {sourceDisplayName(source.source_id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Filing year</span>
          <select value={filters.year} onChange={(event) => updateFilter('year', event.target.value)}>
            <option value="">Any year</option>
            {[2026, 2025, 2024, 2023, 2022, 2021, 2020].map((year) => <option key={year} value={year}>{year}</option>)}
          </select>
        </label>
        <div className="form-presets">
          <span>Report category</span>
          <div>
            {FORM_PRESETS.map((preset) => (
              <button key={preset.label} className={filters.document_type === preset.value ? 'selected' : ''} onClick={() => useFormPreset(preset.value)}>{preset.label}</button>
            ))}
          </div>
        </div>
        <button className="text-button reset-search" onClick={resetSearch}>Reset</button>
      </section>

      {showAdvanced && (
        <section className="advanced-filters">
          <label className="filing-query">
            <span>Filing ID or free text</span>
            <div className="input-with-icon">
              <Search size={16} />
              <input value={filters.q} placeholder="Accession number, form name..." onChange={(event) => updateFilter('q', event.target.value)} />
            </div>
          </label>
          <label>
            <span>Exact native form</span>
            <input value={filters.form} placeholder="10-K, 20-F, annual_report..." onChange={(event) => updateFilter('form', event.target.value)} />
          </label>
          <label>
            <span>Archive state</span>
            <select value={filters.status} onChange={(event) => updateFilter('status', event.target.value)}>
              <option value="complete">Available locally</option>
              <option value="">Any state</option>
              <option value="pending">Scheduled</option>
              <option value="failed">Needs retry</option>
            </select>
          </label>
        </section>
      )}

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
            <span>Loading source coverage...</span>
          </div>
        )}
      </section>

      <section className="filing-layout">
        <div className="filing-results panel">
          <div className="panel-head">
            <div>
              <h2>{selectedIssuer ? `${selectedIssuer.company_name || selectedIssuer.ticker} filings` : 'Recent filing inventory'}</h2>
              <span>{loading ? 'Loading...' : `${formatInteger(filings.length)} filings shown`}</span>
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
                      <span className="pill">{formatFilingType(row)}</span>
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
              <span>{selected ? `${sourceDisplayName(selected.source_id)} / ${selected.source_filing_id}` : 'Choose a filing to inspect its files'}</span>
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
