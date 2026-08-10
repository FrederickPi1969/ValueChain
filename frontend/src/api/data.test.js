import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildAcquisitionFilingQuery, fetchRunRegistry } from './data.js';

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
});
