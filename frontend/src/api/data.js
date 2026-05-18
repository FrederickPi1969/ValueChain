export async function fetchRunRegistry() {
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
  const path = run?.data_path || `/data/runs/${run.run_id}/dashboard-data.json`;
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Unable to load dashboard data for ${run.run_id}: ${response.status}`);
  }
  return response.json();
}
