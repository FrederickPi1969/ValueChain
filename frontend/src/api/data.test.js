import { describe, expect, it } from 'vitest';
import { buildAcquisitionFilingQuery } from './data.js';

describe('buildAcquisitionFilingQuery', () => {
  it('keeps only populated acquisition filters', () => {
    expect(
      buildAcquisitionFilingQuery({
        source_id: 'sec_edgar',
        year: 2026,
        q: ' NVIDIA ',
        form: '',
        status: null,
        limit: 100,
      }),
    ).toBe('source_id=sec_edgar&year=2026&q=NVIDIA&limit=100');
  });
});
