from valuechain.edge_quality import looks_like_legal_entity


def test_legal_entity_check_preserves_suffix_before_normalization() -> None:
    assert looks_like_legal_entity("SK Hynix Inc")
    assert looks_like_legal_entity("Micron Technology, Inc")
