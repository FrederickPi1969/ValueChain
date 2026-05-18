import { describe, expect, it } from 'vitest';
import { filterBottlenecks, filterCompanies, filterEdges, filterEvidence, uniqueSorted } from './filters.js';

describe('filters', () => {
  it('filters edges by company, relation, modality, and search text', () => {
    const edges = [
      { subject: 'NVIDIA', object: 'TSMC', relation_type: 'foundry_dependency', modality: 'current_fact' },
      { subject: 'Microsoft', object: 'power', relation_type: 'power_or_utility_dependency', modality: 'risk_hypothetical' },
    ];
    const result = filterEdges(edges, {
      company: 'NVIDIA',
      relation: 'foundry_dependency',
      modality: 'current_fact',
      query: 'tsmc',
    });
    expect(result).toHaveLength(1);
    expect(result[0].object).toBe('TSMC');
  });

  it('filters evidence with free text', () => {
    const evidence = [
      { subject: 'AMD', relation_type: 'licensing_dependency', modality: 'current_fact', evidence_text: 'Microsoft support matters' },
      { subject: 'AMD', relation_type: 'supplier_dependency', modality: 'current_fact', evidence_text: 'wafer supply' },
    ];
    expect(filterEvidence(evidence, { query: 'microsoft' })).toHaveLength(1);
  });

  it('filters bottlenecks by dependent company', () => {
    const rows = [{ object: 'TSMC', subjects: 'NVIDIA;AMD', relation_types: 'foundry_dependency' }];
    expect(filterBottlenecks(rows, { company: 'AMD' })).toHaveLength(1);
    expect(filterBottlenecks(rows, { company: 'Microsoft' })).toHaveLength(0);
  });

  it('filters company universe rows without requiring graph edges', () => {
    const rows = [
      {
        company: 'NVIDIA',
        relation_types: 'foundry_dependency, supplier_dependency',
        modality_counts: { current_fact: 2 },
      },
      { company: 'Microsoft', relation_types: '', modality_counts: {} },
    ];
    expect(filterCompanies(rows, { query: '', company: '', relation: '', modality: '' })).toHaveLength(2);
    expect(filterCompanies(rows, { relation: 'foundry_dependency' })).toEqual([rows[0]]);
    expect(filterCompanies(rows, { modality: 'current_fact' })).toEqual([rows[0]]);
  });

  it('returns sorted unique values', () => {
    expect(uniqueSorted(['b', 'a', 'b', ''])).toEqual(['a', 'b']);
  });
});
