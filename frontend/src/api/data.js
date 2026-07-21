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

async function fetchAcquisitionJson(path, token) {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: acquisitionHeaders(token),
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
  try {
    const response = await fetch(`${API_BASE}/api/runs`, { cache: 'no-store' });
    if (response.ok) {
      const payload = await response.json();
      return Array.isArray(payload.runs) ? payload.runs : [];
    }
  } catch {
    // Fall back to generated static artifacts when the API is not running.
  }
  const response = await fetch('/data/runs.json', { cache: 'no-store' });
  if (response.status === 404) {
    return [];
  }
  if (!response.ok) {
    throw new Error(`Unable to load run registry: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.runs) ? payload.runs : [];
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

export async function fetchCompanyBriefIndex(run) {
  if (!run?.run_id) return [];
  try {
    const response = await fetch(`${API_BASE}/api/runs/${run.run_id}/briefs`, { cache: 'no-store' });
    if (response.ok) {
      const payload = await response.json();
      return Array.isArray(payload.briefs) ? payload.briefs : [];
    }
  } catch {
    // Fall back to generated static artifacts when the API is not running.
  }

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
