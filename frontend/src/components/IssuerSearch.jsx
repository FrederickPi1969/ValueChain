import { useEffect, useMemo, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';
import { fetchAcquisitionIssuers } from '../api/data.js';

function issuerDisplayName(issuer) {
  if (!issuer) return '';
  const name = issuer.company_name || issuer.ticker || issuer.source_issuer_id || '';
  const ticker = issuer.ticker ? ` (${issuer.ticker})` : '';
  return `${name}${ticker}`;
}

function issuerSubline(issuer) {
  return [issuer.source_id, issuer.exchange, issuer.source_issuer_id]
    .filter(Boolean)
    .join(' / ');
}

export function IssuerSearch({ token, sourceId, selectedIssuer, onSelect }) {
  const [term, setTerm] = useState('');
  const [options, setOptions] = useState([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [error, setError] = useState('');
  const boxRef = useRef(null);

  const normalizedTerm = useMemo(() => term.trim(), [term]);
  const hasToken = token.trim().length > 0;

  useEffect(() => {
    setTerm(issuerDisplayName(selectedIssuer));
  }, [selectedIssuer]);

  useEffect(() => {
    const onPointerDown = (event) => {
      if (!boxRef.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, []);

  useEffect(() => {
    if (!open || !hasToken || normalizedTerm.length < 1) {
      setOptions([]);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true);
      setError('');
      fetchAcquisitionIssuers(
        {
          source_id: sourceId,
          q: normalizedTerm,
          limit: 50,
        },
        token,
        { signal: controller.signal },
      )
        .then((payload) => {
          setOptions(Array.isArray(payload.items) ? payload.items : []);
          setActiveIndex(0);
        })
        .catch((err) => {
          if (err.name !== 'AbortError') {
            setError(err.message);
            setOptions([]);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 180);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [hasToken, normalizedTerm, open, sourceId, token]);

  const choose = (issuer) => {
    onSelect(issuer);
    setTerm(issuerDisplayName(issuer));
    setOpen(false);
  };

  const clear = () => {
    onSelect(null);
    setTerm('');
    setOptions([]);
    setOpen(false);
  };

  const onKeyDown = (event) => {
    if (!open && ['ArrowDown', 'Enter'].includes(event.key)) {
      setOpen(true);
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((current) => Math.min(current + 1, Math.max(options.length - 1, 0)));
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((current) => Math.max(current - 1, 0));
    }
    if (event.key === 'Enter' && open && options[activeIndex]) {
      event.preventDefault();
      choose(options[activeIndex]);
    }
    if (event.key === 'Escape') setOpen(false);
  };

  return (
    <div className="issuer-search" ref={boxRef}>
      <label>
        <span>Company / issuer</span>
        <div className="issuer-input-wrap">
          <Search size={16} />
          <input
            value={term}
            placeholder="Type 1+ char: NVIDIA, ASML, 603162..."
            onChange={(event) => {
              setTerm(event.target.value);
              if (selectedIssuer) onSelect(null);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onKeyDown}
            autoComplete="off"
          />
          {term && (
            <button type="button" className="issuer-clear" title="Clear issuer" onClick={clear}>
              <X size={14} />
            </button>
          )}
        </div>
      </label>
      {open && normalizedTerm.length >= 1 && (
        <div className="issuer-menu">
          {!hasToken && <div className="issuer-menu-state">Enter file API token first</div>}
          {hasToken && loading && <div className="issuer-menu-state">Searching issuers...</div>}
          {!loading &&
            hasToken &&
            options.map((issuer, index) => (
              <button
                type="button"
                key={`${issuer.source_id}:${issuer.source_issuer_id}`}
                className={index === activeIndex ? 'active' : ''}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(issuer)}
              >
                <strong>{issuer.company_name || issuer.ticker || issuer.source_issuer_id}</strong>
                <span>{issuerSubline(issuer)}</span>
                <small>
                  {Number(issuer.filing_count || 0).toLocaleString()} filings
                  {issuer.latest_filing_date ? ` / latest ${String(issuer.latest_filing_date).slice(0, 10)}` : ''}
                </small>
              </button>
            ))}
          {hasToken && !loading && !options.length && !error && <div className="issuer-menu-state">No issuer match</div>}
          {error && <div className="issuer-menu-state error">{error}</div>}
        </div>
      )}
    </div>
  );
}
