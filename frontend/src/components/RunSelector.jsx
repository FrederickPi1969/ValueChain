import { Database, RefreshCw } from 'lucide-react';

export function RunSelector({ runs, selectedRunId, onSelect, onRefresh }) {
  const selected = runs.find((run) => run.run_id === selectedRunId);
  return (
    <div className="run-selector">
      <div className="brand">
        <Database size={22} />
        <div>
          <h1>AI Value Chain Console</h1>
          <p>Disclosure-derived dependency evidence for industry value-chain review.</p>
        </div>
      </div>
      <div className="run-controls">
        <label>
          Run
          <select value={selectedRunId || ''} onChange={(event) => onSelect(event.target.value)}>
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {run.run_label || run.run_id}
              </option>
            ))}
          </select>
        </label>
        <button className="icon-button" onClick={onRefresh} title="Reload run registry" aria-label="Reload run registry">
          <RefreshCw size={17} />
        </button>
      </div>
      {selected && (
        <div className="run-meta">
          <span>{selected.created_at}</span>
          <span>{selected.counts?.companies || 0} companies</span>
          <span>{selected.counts?.filings || 0} filings</span>
          <span>{selected.counts?.graph_edges || 0} edges</span>
        </div>
      )}
    </div>
  );
}
