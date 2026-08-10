import { describe, expect, it, vi } from 'vitest';

vi.mock('sigma', () => ({ default: class SigmaMock {} }));
import { buildTopology, selectMultiEgo } from './TopologyMap.jsx';

function edge(index, object = `Company ${index}`, subject = `Company ${index + 1}`) {
  return {
    relationship_id: `rel-${index}`, object, subject, relation_type: 'supplies_to',
    review_status: index % 3 === 0 ? 'accepted' : 'unreviewed', evidence_count: (index % 7) + 1,
  };
}

describe('large topology projection', () => {
  it('uses a bounded partitioned layout for a large graph', () => {
    const rows = Array.from({ length: 1_200 }, (_, index) => edge(index));
    const topology = buildTopology({ rows, enabledTypes: ['supplies_to'], edgeLimit: 750 });
    expect(topology.layoutMode).toBe('partitioned');
    expect(topology.rows.filter((row) => !row.isIndustryMembership)).toHaveLength(750);
    expect(topology.omittedEdgeCount).toBe(450);
    expect(topology.nodes.length).toBeGreaterThan(650);
  });

  it('makes one-hop and two-hop expansion materially different', () => {
    const rows = [edge(0, 'Seed', 'A'), edge(1, 'A', 'B'), edge(2, 'B', 'C')];
    expect(selectMultiEgo(rows, ['Seed'], 1)).toHaveLength(1);
    expect(selectMultiEgo(rows, ['Seed'], 2)).toHaveLength(2);
  });

  it('hides anonymous counterparties and candidates by default controls', () => {
    const rows = [edge(0, 'Named Supplier', 'Issuer'), edge(1, 'Customer A', 'Issuer')];
    const topology = buildTopology({
      rows, enabledTypes: ['supplies_to'], showExposure: false, includeCandidates: false,
    });
    const relationRows = topology.rows.filter((row) => !row.isIndustryMembership);
    expect(relationRows).toHaveLength(1);
    expect(relationRows[0].object).toBe('Named Supplier');
  });
});
