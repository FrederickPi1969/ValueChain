import { Download, RotateCcw, Search } from 'lucide-react';
import { uniqueSorted } from '../lib/filters.js';

export function FilterBar({ data, filters, onChange, onReset, onExport, onCurrentFacts }) {
  const companies = uniqueSorted((data?.edges || []).map((edge) => edge.subject));
  const relations = uniqueSorted((data?.edges || []).map((edge) => edge.relation_type));
  const modalities = uniqueSorted((data?.edges || []).map((edge) => edge.modality));

  return (
    <section className="filter-bar">
      <label className="search-box">
        <span>Search</span>
        <div className="input-with-icon">
          <Search size={16} />
          <input
            value={filters.query}
            onChange={(event) => onChange({ query: event.target.value })}
            placeholder="Company, object, relation, evidence text"
          />
        </div>
      </label>
      <Select label="Company" value={filters.company} onChange={(company) => onChange({ company })} values={companies} all="All companies" />
      <Select label="Relation" value={filters.relation} onChange={(relation) => onChange({ relation })} values={relations} all="All relations" />
      <Select label="Modality" value={filters.modality} onChange={(modality) => onChange({ modality })} values={modalities} all="All modalities" />
      <div className="filter-actions">
        <button onClick={onCurrentFacts}>Current</button>
        <button onClick={onExport}>
          <Download size={16} />
          CSV
        </button>
        <button className="icon-button" onClick={onReset} title="Reset filters" aria-label="Reset filters">
          <RotateCcw size={16} />
        </button>
      </div>
    </section>
  );
}

function Select({ label, value, onChange, values, all }) {
  return (
    <label>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{all}</option>
        {values.map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </label>
  );
}
