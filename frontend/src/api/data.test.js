import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildAcquisitionFilingQuery, fetchCompanyBriefIndex, fetchRunRegistry } from './data.js';

afterEach(() => vi.unstubAllGlobals());

describe('buildAcquisitionFilingQuery', () => {
  it('keeps only populated acquisition filters', () => {
    expect(
      buildAcquisitionFilingQuery({
        source_id: 'sec_edgar',
        issuer_id: '0001045810',
        year: 2026,
        q: ' NVIDIA ',
        form: '',
        status: null,
        limit: 100,
      }),
    ).toBe('source_id=sec_edgar&issuer_id=0001045810&year=2026&q=NVIDIA&limit=100');
  });

  it('falls back to static runs when the database has no extracted runs', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ runs: [] }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ runs: [{ run_id: 'semiconductor-pilot' }] }) });
    vi.stubGlobal('fetch', fetch);

    await expect(fetchRunRegistry()).resolves.toEqual([{ run_id: 'semiconductor-pilot' }]);
    expect(fetch).toHaveBeenNthCalledWith(2, '/data/runs.json', { cache: 'no-store' });
  });

  it('merges API and static runs and prefers the complete static duplicate', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ runs: [
          { run_id: 'legacy', created_at: '2026-05-01', data_path: '/api/runs/legacy/dashboard-data' },
          { run_id: 'demo', created_at: '2026-07-01', data_path: '/api/runs/demo/dashboard-data' },
        ] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [
          { run_id: 'demo', created_at: '2026-08-09', data_path: '/data/runs/demo/dashboard-data.json' },
        ] }),
      });
    vi.stubGlobal('fetch', fetch);

    await expect(fetchRunRegistry()).resolves.toEqual([
      { run_id: 'demo', created_at: '2026-08-09', data_path: '/data/runs/demo/dashboard-data.json' },
      { run_id: 'legacy', created_at: '2026-05-01', data_path: '/api/runs/legacy/dashboard-data' },
    ]);
  });

  it('keeps API runs when no static registry is deployed', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ runs: [{ run_id: 'database-run' }] }) })
      .mockResolvedValueOnce({ ok: false, status: 404 });
    vi.stubGlobal('fetch', fetch);

    await expect(fetchRunRegistry()).resolves.toEqual([{ run_id: 'database-run' }]);
  });

  it('loads brief indexes from generated artifacts without probing a missing API route', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ briefs: [{ ticker: 'NVDA' }] }),
    });
    vi.stubGlobal('fetch', fetch);

    await expect(fetchCompanyBriefIndex({ run_id: 'demo' })).resolves.toEqual([{ ticker: 'NVDA' }]);
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledWith('/data/runs/demo/briefs/index.json', { cache: 'no-store' });
  });
});
