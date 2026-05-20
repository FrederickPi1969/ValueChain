import { describe, expect, it } from 'vitest';
import { briefTickerSet, formatPercent, matchBriefForCompany, normalizeName } from './briefs.js';

describe('brief helpers', () => {
  it('normalizes legal suffixes for company matching', () => {
    expect(normalizeName('NVIDIA Corporation')).toBe('nvidia');
    expect(normalizeName('ASML Holding N.V.')).toBe('asml holding');
  });

  it('matches brief entries by ticker first', () => {
    const entries = [{ ticker: 'NVDA', company_name: 'NVIDIA Corporation' }];
    expect(matchBriefForCompany({ ticker: 'NVDA', company: 'Other Name' }, entries)).toBe(entries[0]);
  });

  it('falls back to company name matching', () => {
    const entries = [{ ticker: 'ASML', company_name: 'ASML Holding N.V.' }];
    expect(matchBriefForCompany({ company: 'ASML Holding NV' }, entries)).toBe(entries[0]);
  });

  it('builds ticker sets and formats confidence', () => {
    expect(briefTickerSet([{ ticker: 'nvda' }, { ticker: '' }]).has('NVDA')).toBe(true);
    expect(formatPercent(0.928)).toBe('93%');
  });
});
