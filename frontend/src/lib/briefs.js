export function normalizeTicker(value) {
  return String(value || '').trim().toUpperCase();
}

export function normalizeName(value) {
  const text = String(value || '')
    .toLowerCase()
    .replace(/\bn\s*\.\s*v\s*\.?/g, '')
    .replace(/\bs\s*\.\s*a\s*\.?/g, '');
  return text
    .replace(/\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|nv|sa)\b/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

export function briefTickerSet(entries) {
  return new Set((entries || []).map((entry) => normalizeTicker(entry.ticker)).filter(Boolean));
}

export function matchBriefForCompany(company, entries) {
  if (!company || !entries?.length) return null;
  const ticker = normalizeTicker(company.ticker);
  if (ticker) {
    const byTicker = entries.find((entry) => normalizeTicker(entry.ticker) === ticker);
    if (byTicker) return byTicker;
  }
  const companyName = normalizeName(company.company || company.company_name || company.subject);
  if (!companyName) return null;
  return entries.find((entry) => normalizeName(entry.company_name) === companyName) || null;
}

export function getAnalystBullets(brief, key) {
  const values = brief?.analyst_interpretation?.[key];
  return Array.isArray(values) ? values.filter(Boolean) : [];
}

export function getBriefEvidenceById(brief) {
  return new Map((brief?.evidence_table || []).map((row) => [row.evidence_id, row]));
}

export function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '';
  return `${Math.round(number * 100)}%`;
}
