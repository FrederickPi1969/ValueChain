const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

function acquisitionHeaders(token) {
  const value = String(token || '').trim();
  return value ? { Authorization: `Bearer ${value}` } : {};
}

export function buildAcquisitionFilingQuery(filters = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      params.set(key, String(value).trim());
    }
  }
  return params.toString();
}

async function fetchAcquisitionJson(path, token, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: acquisitionHeaders(token),
    signal: options.signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`Acquisition API ${response.status}: ${detail || response.statusText}`);
  }
  return response.json();
}

export async function fetchAcquisitionSources(token) {
  return fetchAcquisitionJson('/api/acquisition/sources', token);
}

export async function fetchAcquisitionIssuers(filters, token, options = {}) {
  const query = buildAcquisitionFilingQuery({ limit: 50, ...filters });
  return fetchAcquisitionJson(`/api/acquisition/issuers?${query}`, token, options);
}

export async function fetchAcquisitionFilings(filters, token) {
  const query = buildAcquisitionFilingQuery({ limit: 100, ...filters });
  return fetchAcquisitionJson(`/api/acquisition/filings?${query}`, token);
}

export async function fetchAcquisitionFilingDetail(sourceId, filingId, token) {
  const source = encodeURIComponent(sourceId);
  const filing = encodeURIComponent(filingId);
  return fetchAcquisitionJson(`/api/acquisition/filings/${source}/${filing}`, token);
}

export async function fetchAcquisitionDocumentBlob(documentId, token) {
  const response = await fetch(`${API_BASE}/api/acquisition/documents/${documentId}/download`, {
    headers: acquisitionHeaders(token),
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`Download failed ${response.status}: ${detail || response.statusText}`);
  }
  const blob = await response.blob();
  const disposition = response.headers.get('content-disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return {
    blob,
    filename: match?.[1] || `document-${documentId}`,
  };
}

export async function fetchRunRegistry() {
  let apiRuns = [];
  try {
    const response = await fetch(`${API_BASE}/api/runs`, { cache: 'no-store' });
    if (response.ok) {
      const payload = await response.json();
      apiRuns = Array.isArray(payload.runs) ? payload.runs : [];
    }
  } catch {
    // Static artifacts below keep the UI usable when the API is not running.
  }

  let staticRuns = [];
  try {
    const response = await fetch('/data/runs.json', { cache: 'no-store' });
    if (response.ok) {
      const payload = await response.json();
      staticRuns = Array.isArray(payload) ? payload : Array.isArray(payload.runs) ? payload.runs : [];
    } else if (response.status !== 404) {
      throw new Error(`Unable to load run registry: ${response.status}`);
    }
  } catch (error) {
    if (!apiRuns.length) throw error;
  }

  // Keep database and generated runs visible together. Static entries win on
  // duplicate IDs because their data_path points at a complete immutable artifact.
  const merged = new Map(apiRuns.map((run) => [run.run_id, run]));
  staticRuns.forEach((run) => merged.set(run.run_id, run));
  return [...merged.values()].sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')));
}

export async function fetchDashboardData(run) {
  if (run?.data_path?.startsWith('/api/')) {
    try {
      const response = await fetch(`${API_BASE}${run.data_path}`, { cache: 'no-store' });
      if (response.ok) {
        return response.json();
      }
    } catch {
      // Fall through to static artifact path.
    }
  }
  const path = run?.data_path || `/data/runs/${run.run_id}/dashboard-data.json`;
  const staticPath = path.startsWith('/api/') ? `/data/runs/${run.run_id}/dashboard-data.json` : path;
  const response = await fetch(staticPath, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Unable to load dashboard data for ${run.run_id}: ${response.status}`);
  }
  return response.json();
}

export async function fetchResolutionRecords(run) {
  if (!run?.run_id) return [];
  const response = await fetch(`/data/runs/${run.run_id}/entity_resolution_records.jsonl`, { cache: 'no-store' });
  if (response.status === 404) return [];
  if (!response.ok) throw new Error(`Unable to load entity resolution records: ${response.status}`);
  return (await response.text()).split('\n').filter(Boolean).map((line) => JSON.parse(line));
}

export async function fetchCompanyBriefIndex(run) {
  if (!run?.run_id) return [];
  const response = await fetch(`/data/runs/${run.run_id}/briefs/index.json`, { cache: 'no-store' });
  if (response.status === 404) return [];
  if (!response.ok) {
    throw new Error(`Unable to load company brief index for ${run.run_id}: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.briefs) ? payload.briefs : [];
}

export async function fetchCompanyBrief(run, entry) {
  if (!run?.run_id || !entry) return null;
  const ticker = typeof entry === 'string' ? entry : entry.ticker;
  const staticPath =
    typeof entry === 'string'
      ? `/data/runs/${run.run_id}/briefs/${entry.toUpperCase()}_dependency_brief.json`
      : entry.path || `/data/runs/${run.run_id}/briefs/${String(ticker || '').toUpperCase()}_dependency_brief.json`;
  const response = await fetch(staticPath, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Unable to load company brief for ${run.run_id}: ${response.status}`);
  }
  return response.json();
}
