import { describe, expect, it, vi } from 'vitest';

vi.mock('sigma', () => ({ default: class SigmaMock {} }));
import { buildNodeProfile, buildTopology, selectMultiEgo } from './TopologyMap.jsx';

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

  it('keeps global context but makes every selected anchor neighborhood prominent', () => {
    const rows = [
      edge(0, 'A', 'B'), edge(1, 'B', 'C'), edge(2, 'Elsewhere', 'Context'),
    ];
    const topology = buildTopology({
      rows, anchors: ['A', 'B'], enabledTypes: ['supplies_to'], enabledFamilies: ['supply_chain'], edgeLimit: 20,
    });
    const graphRows = topology.rows.filter((row) => !row.isIndustryMembership);
    expect(graphRows.find((row) => row.object === 'A' && row.subject === 'B').isAnchorLink).toBe(true);
    expect(graphRows.find((row) => row.object === 'B' && row.subject === 'C').isFocused).toBe(true);
    expect(graphRows.find((row) => row.object === 'Elsewhere').isContext).toBe(true);
    expect(topology.nodes.find((node) => node.id.includes('Elsewhere')).contextOnly).toBe(true);
  });

  it('can remove global context while preserving the combined one-hop research set', () => {
    const rows = [edge(0, 'A', 'B'), edge(1, 'B', 'C'), edge(2, 'Elsewhere', 'Context')];
    const topology = buildTopology({
      rows, anchors: ['A', 'B'], enabledTypes: ['supplies_to'], enabledFamilies: ['supply_chain'], showContext: false, edgeLimit: 20,
    });
    const graphRows = topology.rows.filter((row) => !row.isIndustryMembership);
    expect(graphRows).toHaveLength(2);
    expect(graphRows.some((row) => row.object === 'Elsewhere')).toBe(false);
  });

  it('summarizes directed relationships and ownership for a selected company', () => {
    const rows = [
      { ...edge(0, 'Supplier', 'Company'), product_or_service: 'Memory', source_accession_numbers: ['0001'], verification_status: 'cross_filing_verified' },
      { ...edge(1, 'Company', 'Customer'), source_accession_numbers: ['0002'] },
      { ...edge(2, 'Parent', 'Company'), relation_type: 'controls', relationship_family: 'ownership_control', source_accession_numbers: ['0003'] },
      { ...edge(3, 'Company', 'Subsidiary'), relation_type: 'controls', relationship_family: 'ownership_control', source_accession_numbers: ['0003'] },
    ];
    const profile = buildNodeProfile(rows, 'Company');
    expect(profile.incoming).toHaveLength(2);
    expect(profile.outgoing).toHaveLength(2);
    expect(profile.parent.object).toBe('Parent');
    expect(profile.subsidiaries[0].subject).toBe('Subsidiary');
    expect(profile.products).toEqual(['Memory']);
    expect(profile.crossFiled).toBe(1);
  });
});
