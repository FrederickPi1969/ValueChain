from valuechain.models import EntityMention, MentionCluster, Passage, RelationEvidence
from valuechain.resolution_records import attach_internal_resolution_candidates, build_resolution_records


def test_relation_linked_named_mention_gets_priority_and_candidate_status():
    passage = Passage("p", "NVDA", "1", "NVIDIA Corporation", "10-K", "a", "2026-01-01", "", "url", "Business", 1, "We purchase memory from SK Hynix Inc.", "test", "1")
    mention = EntityMention("SK Hynix Inc.", "organization", "SK Hynix Inc.", mention_kind="named_entity", cluster_id="c", passage_id="p")
    cluster = MentionCluster("c", "sk hynix", "SK Hynix Inc.", "SK Hynix Inc.", resolution_status="name_resolved", mention_count=1)
    evidence = RelationEvidence("NVIDIA Corporation", "SK Hynix Inc.", "supplier_dependency", "d", "current_fact", "high", "as_disclosed", passage.text, .9, "test", "NVDA", "1", "10-K", "2026-01-01", "", "a", "url", "Business", "p", 1, "test", "1")
    rows = build_resolution_records([mention], [cluster], [passage], [evidence])
    assert rows[0]["resolution_status"] == "candidate"
    assert rows[0]["candidate_relationship_count"] == 1
    assert rows[0]["decision"] == "PENDING"


def test_generic_relation_object_stays_visible_as_unresolved():
    passage = Passage("p", "NVDA", "1", "NVIDIA Corporation", "10-K", "a", "2026-01-01", "", "url", "Business", 1, "We rely on limited suppliers.", "test", "1")
    evidence = RelationEvidence("NVIDIA Corporation", "limited suppliers", "supplier_dependency", "d", "current_fact", "high", "as_disclosed", passage.text, .8, "test", "NVDA", "1", "10-K", "2026-01-01", "", "a", "url", "Business", "p", 1, "test", "1")
    assert build_resolution_records([], [], [passage], [evidence])[0]["decision"] == "KEEP_UNRESOLVED"


def test_geography_mention_is_retained_but_not_sent_to_legal_entity_candidates():
    passage = Passage("p", "AMD", "1", "AMD", "10-K", "a", "2026-01-01", "", "url", "Risk", 1, "China exposure", "test", "1")
    mention = EntityMention("China", "organization", "China", mention_kind="named_entity", cluster_id="c", passage_id="p")
    cluster = MentionCluster("c", "china", "China", "China", resolution_status="unresolved", mention_count=1)
    evidence = RelationEvidence("AMD", "China", "facility_or_geographic_exposure", "d", "current_fact", "high", "as_disclosed", passage.text, .8, "test", "AMD", "1", "10-K", "2026-01-01", "", "a", "url", "Risk", "p", 1, "test", "1")
    row = build_resolution_records([mention], [cluster], [passage], [evidence])[0]
    assert row["resolution_status"] == "unresolved"
    assert row["entity_class"] == "geography"


def test_internal_candidates_are_provenance_not_automatic_canonicalization():
    passage = Passage("p", "NVDA", "1", "NVIDIA Corporation", "10-K", "a", "2026-01-01", "", "url", "Business", 1, "We purchase memory from SK Hynix Inc.", "test", "1")
    mention = EntityMention("SK Hynix Inc.", "organization", "SK Hynix Inc.", mention_kind="named_entity", cluster_id="c", passage_id="p")
    cluster = MentionCluster("c", "sk hynix", "SK Hynix Inc.", "SK Hynix Inc.", resolution_status="name_resolved", mention_count=1)
    evidence = RelationEvidence("NVIDIA Corporation", "SK Hynix Inc.", "supplier_dependency", "d", "current_fact", "high", "as_disclosed", passage.text, .9, "test", "NVDA", "1", "10-K", "2026-01-01", "", "a", "url", "Business", "p", 1, "test", "1")
    records = attach_internal_resolution_candidates(
        build_resolution_records([mention], [cluster], [passage], [evidence]),
        [{"entity_id": "entity:sk-hynix", "canonical_name": "SK Hynix Inc."}],
        [],
    )
    assert records[0]["decision"] == "PENDING"
    assert records[0]["candidate_entities"][0]["source"] == "current_canonical_graph"
    assert records[0]["resolution_evidence"][-1]["source"] == "current_canonical_graph"
