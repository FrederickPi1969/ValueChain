from valuechain.api import build_filters


def test_build_filters_composes_optional_edge_filters() -> None:
    where, params = build_filters(
        run_id="r1",
        company="NVIDIA Corporation",
        relation="foundry_dependency",
        modality="current_fact",
        q="TSMC",
        subject_col="subject",
    )
    assert "run_id = %s" in where
    assert "subject = %s" in where
    assert "relation_type = %s" in where
    assert params[:4] == ("r1", "NVIDIA Corporation", "foundry_dependency", "current_fact")


def test_build_filters_can_search_evidence_text_columns() -> None:
    where, params = build_filters(
        run_id="r1",
        company="",
        relation="",
        modality="",
        q="supplier",
        subject_col="subject",
        q_columns=("subject", "object", "evidence_text"),
    )
    assert "evidence_text ILIKE %s" in where
    assert params == ("r1", "%supplier%", "%supplier%", "%supplier%")
