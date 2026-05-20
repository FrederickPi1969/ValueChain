const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';

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
