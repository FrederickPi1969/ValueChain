import { describe, expect, it } from 'vitest';
import { filterBottlenecks, filterEdges, filterEvidence, uniqueSorted } from './filters.js';

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

  it('returns sorted unique values', () => {
    expect(uniqueSorted(['b', 'a', 'b', ''])).toEqual(['a', 'b']);
  });
});
