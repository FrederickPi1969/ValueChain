import { Download, RotateCcw, Search } from 'lucide-react';
import { uniqueSorted } from '../lib/filters.js';

export function FilterBar({ data, filters, onChange, onReset, onExport, onCurrentFacts }) {
  const companies = uniqueSorted(
    (data?.companies?.length ? data.companies.map((row) => row.company) : (data?.edges || []).map((edge) => edge.subject))
  );
  const relations = uniqueSorted((data?.edges || []).map((edge) => edge.relation_type));
  const modalities = uniqueSorted((data?.edges || []).map((edge) => edge.modality));
  const relationshipFamilies = uniqueSorted((data?.network_edges || []).map((edge) => edge.relationship_family || 'supply_chain'));

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
      <RelationshipFamilies
        values={relationshipFamilies}
        selected={filters.relationshipFamilies || []}
        onChange={(relationshipFamilies) => onChange({ relationshipFamilies })}
      />
      <RelationshipStatuses
        selected={filters.relationshipStatuses || []}
        onChange={(relationshipStatuses) => onChange({ relationshipStatuses })}
      />
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

const STATUS_OPTIONS = [
  ['confirmed', 'Confirmed'],
  ['candidate', 'Candidate / review'],
  ['rejected', 'Rejected'],
];

function RelationshipStatuses({ selected, onChange }) {
  const allSelected = STATUS_OPTIONS.every(([value]) => selected.includes(value));
  const toggle = (value) => onChange(selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]);
  return <fieldset className="relationship-families status-families">
    <legend>Confirmation status</legend>
    <label className="family-all"><input type="checkbox" checked={allSelected} onChange={() => onChange(allSelected ? [] : STATUS_OPTIONS.map(([value]) => value))} /> Show all</label>
    <div className="family-options">
      {STATUS_OPTIONS.map(([value, label]) => <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={() => toggle(value)} /> {label}</label>)}
    </div>
  </fieldset>;
}

const FAMILY_LABELS = {
  supply_chain: 'Supply chain',
  corporate_transaction: 'Corporate transaction',
  ownership_control: 'Ownership / control',
  commercial_relationship: 'Commercial relationship',
  risk_exposure: 'Risk / exposure',
};

function RelationshipFamilies({ values, selected, onChange }) {
  if (!values.length) return null;
  const allSelected = values.every((value) => selected.includes(value));
  const toggle = (value) => onChange(selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]);
  return (
    <fieldset className="relationship-families">
      <legend>Map layers</legend>
      <label className="family-all"><input type="checkbox" checked={allSelected} onChange={() => onChange(allSelected ? [] : values)} /> All relationships</label>
      <div className="family-options">
        {values.map((value) => <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={() => toggle(value)} /> {FAMILY_LABELS[value] || value}</label>)}
      </div>
    </fieldset>
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
