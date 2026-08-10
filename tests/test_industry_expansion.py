from valuechain.industry_expansion import ExpansionConfig, build_industry_expansion
from valuechain.models import Company


def relationship(rel_id, source_id, source, target_id, target, *, status="unreviewed", forms=None, family="supply_chain", evidence=1):
    return {
        "relationship_id": rel_id,
        "source_entity_id": source_id, "source_entity_name": source,
        "target_entity_id": target_id, "target_entity_name": target,
        "relationship_type": "supplies_to", "relationship_family": family,
        "review_status": status, "source_types": forms or ["10-K"],
        "source_accession_numbers": [f"acc-{rel_id}"], "evidence_ids": [f"ev-{rel_id}"],
        "issuer_names": [target], "evidence_count": evidence, "confidence": .9,
    }


def fixture():
    companies = [
        Company("NVDA", "NVIDIA Corporation", "accelerator_compute"),
        Company("TSM", "Taiwan Semiconductor", "foundry"),
        Company("ASML", "ASML Holding", "semicap"),
        Company("MSFT", "Microsoft", "cloud_hyperscaler"),
    ]
    entities = [{"entity_id": f"e:{row.ticker}", "canonical_name": row.company_name, "role": row.role} for row in companies]
    relationships = [
        relationship("1", "e:TSM", "Taiwan Semiconductor", "e:NVDA", "NVIDIA Corporation", status="accepted", evidence=4),
        relationship("2", "e:ASML", "ASML Holding", "e:TSM", "Taiwan Semiconductor", evidence=3),
        relationship("3", "e:NVDA", "NVIDIA Corporation", "e:MSFT", "Microsoft", forms=["10-Q"], evidence=2),
        relationship("4", "e:BAD", "Customer A", "e:NVDA", "NVIDIA Corporation", status="rejected"),
        relationship("5", "e:MSFT", "Microsoft", "e:ASML", "ASML Holding", forms=["8-K"]),
        relationship("6", "e:MSFT", "Microsoft", "e:NVDA", "NVIDIA Corporation", family="ownership_control"),
    ]
    return companies, entities, relationships


def test_expansion_has_real_hop_delta_and_filing_provenance():
    companies, entities, relationships = fixture()
    one = build_industry_expansion(companies, entities, relationships, ExpansionConfig(seeds=["NVDA"], max_hops=1))
    two = build_industry_expansion(companies, entities, relationships, ExpansionConfig(seeds=["NVDA"], max_hops=2))

    assert one["summary"]["node_count"] == 3
    assert two["summary"]["node_count"] == 4
    asml = next(row for row in two["nodes"] if row["ticker"] == "ASML")
    assert asml["expansion_depth"] == 2
    assert asml["discovered_from"]["relationship_id"] == "2"
    assert asml["discovered_from"]["source_accession_numbers"] == ["acc-2"]
    assert two["diagnostics"]["excluded_relationships"] == {"filing_form": 1, "relationship_family": 1, "rejected": 1}


def test_expansion_is_bounded_and_preserves_discovery_edges():
    companies, entities, relationships = fixture()
    result = build_industry_expansion(
        companies, entities, relationships,
        ExpansionConfig(seeds=["NVIDIA Corporation"], max_hops=3, max_nodes=2, max_edges=1),
    )
    assert result["summary"]["node_count"] == 2
    assert result["summary"]["edge_count"] == 1
    assert result["summary"]["node_cap_reached"] is True
    assert result["edges"][0]["relationship_id"] == "1"


def test_semiconductor_industry_can_choose_seeds_without_explicit_names():
    companies, entities, relationships = fixture()
    result = build_industry_expansion(
        companies, entities, relationships,
        ExpansionConfig(industry="semiconductor", max_hops=0, max_seeds=10),
    )
    assert {row["ticker"] for row in result["nodes"]} == {"NVDA", "TSM", "ASML"}
    assert result["summary"]["seed_count"] == 3
