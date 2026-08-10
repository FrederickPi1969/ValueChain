from valuechain.mention_layer import build_alias_review_queue, build_entity_triage
from valuechain.models import EntityMention, MentionCluster, Passage


def test_triage_routes_products_and_geographies_out_of_company_review():
    passage = Passage("p", "AMD", "1", "AMD", "10-K", "a", "2026-01-01", "", "", "Business", 1, "GPUs in China", "test", "1")
    clusters = [
        MentionCluster("c-gpu", "gpus", "GPUs", "GPUs", resolution_status="unresolved", mention_count=1),
        MentionCluster("c-china", "china", "China", "China", resolution_status="unresolved", mention_count=1),
        MentionCluster("c-acme", "acme", "Acme", "Acme", resolution_status="unresolved", mention_count=1),
    ]
    mentions = [
        EntityMention("GPUs", "organization", "GPUs", cluster_id="c-gpu", passage_id="p"),
        EntityMention("China", "organization", "China", cluster_id="c-china", passage_id="p"),
        EntityMention("Acme", "organization", "Acme", cluster_id="c-acme", passage_id="p"),
    ]
    triage = build_entity_triage(mentions, clusters, {"p": passage})
    assert {row["proposed_name"]: row["disposition"] for row in triage}["GPUs"] == "retain_non_company"
    assert {row["proposed_name"]: row["entity_class"] for row in triage}["China"] == "geography"
    assert [row["proposed_name"] for row in build_alias_review_queue(mentions, clusters, {"p": passage})] == ["Acme"]


def test_legal_name_pattern_is_still_a_candidate_until_formally_resolved():
    cluster = MentionCluster("c", "sk hynix", "SK Hynix Inc.", "SK Hynix Inc.", resolution_status="name_resolved", mention_count=1)
    mention = EntityMention("SK Hynix Inc.", "organization", "SK Hynix Inc.", cluster_id="c", passage_id="p")
    assert build_alias_review_queue([mention], [cluster], {"p": Passage("p", "NVDA", "1", "NVIDIA", "10-K", "a", "2026-01-01", "", "", "Business", 1, "", "test", "1")})[0]["proposed_name"] == "SK Hynix Inc."


def test_geography_with_legal_suffix_like_text_is_not_a_company_candidate():
    cluster = MentionCluster("c", "malaysia", "Malaysia", "Malaysia", resolution_status="name_resolved", mention_count=1)
    mention = EntityMention("Malaysia", "organization", "Malaysia", cluster_id="c", passage_id="p")
    passage = Passage("p", "NVDA", "1", "NVIDIA", "10-K", "a", "2026-01-01", "", "", "Business", 1, "", "test", "1")
    assert build_alias_review_queue([mention], [cluster], {"p": passage}) == []
