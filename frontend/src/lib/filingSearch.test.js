import { describe, expect, it } from 'vitest';
import { DEFAULT_FILING_FILTERS, filingQueryForIssuer, sourceDisplayName } from './filingSearch.js';

describe('filing search helpers', () => {
  it('uses the selected issuer identity and clears unrelated text', () => {
    expect(
      filingQueryForIssuer(
        { source_id: 'sec_edgar', source_issuer_id: '0001045810' },
        { ...DEFAULT_FILING_FILTERS, q: 'NVIDIA' },
      ),
    ).toMatchObject({ source_id: 'sec_edgar', issuer_id: '0001045810', q: '' });
  });

  it('gives known sources a readable label', () => {
    expect(sourceDisplayName('sec_edgar')).toBe('United States · SEC EDGAR');
  });
});
