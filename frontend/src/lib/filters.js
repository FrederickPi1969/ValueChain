export function uniqueSorted(values) {
  return Array.from(new Set(values.filter(Boolean))).sort((a, b) => String(a).localeCompare(String(b)));
}

export function normalizeSearch(value) {
  return String(value || '').trim().toLowerCase();
}

export function rowContains(row, query) {
  if (!query) return true;
  return Object.values(row).join(' ').toLowerCase().includes(query);
}

export function filterEdges(edges, filters) {
  const query = normalizeSearch(filters.query);
  return edges.filter((edge) => {
    if (filters.company && edge.subject !== filters.company) return false;
    if (filters.relation && edge.relation_type !== filters.relation) return false;
    if (filters.modality && edge.modality !== filters.modality) return false;
    return rowContains(edge, query);
  });
}

export function filterEvidence(evidence, filters) {
  const query = normalizeSearch(filters.query);
  return evidence.filter((row) => {
    if (filters.company && row.subject !== filters.company) return false;
    if (filters.relation && row.relation_type !== filters.relation) return false;
    if (filters.modality && row.modality !== filters.modality) return false;
    return rowContains(row, query);
  });
}

export function filterBottlenecks(bottlenecks, filters) {
  const query = normalizeSearch(filters.query);
  return bottlenecks.filter((row) => {
    if (filters.company && !String(row.subjects || '').includes(filters.company)) return false;
    if (filters.relation && !String(row.relation_types || '').includes(filters.relation)) return false;
    return rowContains(row, query);
  });
}

export function filterCompanies(companies, filters) {
  const query = normalizeSearch(filters.query);
  return companies.filter((row) => {
    if (filters.company && row.company !== filters.company) return false;
    if (filters.relation && !relationTypes(row).includes(filters.relation)) return false;
    if (filters.modality && !Number(row.modality_counts?.[filters.modality] || 0)) return false;
    return rowContains(row, query);
  });
}

function relationTypes(row) {
  return String(row.relation_types || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

export function countWeighted(rows, key) {
  const counts = new Map();
  rows.forEach((row) => {
    counts.set(row[key], (counts.get(row[key]) || 0) + Number(row.evidence_count || 1));
  });
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
}

export function exportCsv(filename, rows) {
  if (!rows.length) return;
  const columns = Object.keys(rows[0]);
  const body = [columns.join(',')]
    .concat(rows.map((row) => columns.map((col) => `"${String(row[col] ?? '').replaceAll('"', '""')}"`).join(',')))
    .join('\n');
  const blob = new Blob([body], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
