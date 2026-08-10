export const DEFAULT_FILING_FILTERS = {
  source_id: '',
  issuer_id: '',
  year: '',
  q: '',
  form: '',
  document_type: '',
  status: 'complete',
};

export const SOURCE_NAMES = {
  sec_edgar: 'United States · SEC EDGAR',
  cninfo: 'China · CNINFO',
  opendart: 'South Korea · OpenDART',
  edinet: 'Japan · EDINET',
  esef: 'Europe · ESEF',
  twse: 'Taiwan · TWSE',
  tpex: 'Taiwan · TPEx',
  cvm_brazil: 'Brazil · CVM',
  companies_house: 'United Kingdom · Companies House',
};

export const FORM_PRESETS = [
  { label: 'All reports', value: '' },
  { label: 'Annual reports', value: 'annual_report' },
  { label: 'Quarterly reports', value: 'quarterly_report' },
  { label: 'Current events', value: 'current_report' },
];

export function sourceDisplayName(sourceId) {
  return SOURCE_NAMES[sourceId] || String(sourceId || '').replaceAll('_', ' ');
}

export function filingQueryForIssuer(issuer, filters) {
  return {
    ...filters,
    source_id: issuer?.source_id || filters.source_id,
    issuer_id: issuer?.source_issuer_id || '',
    q: issuer ? '' : filters.q,
  };
}

export function formatFilingType(row) {
  const type = String(row?.canonical_document_type || row?.form_raw || '').replaceAll('_', ' ');
  return type || 'Regulatory filing';
}
