from valuechain.embeddings import (
    ann_candidate_pairs,
    blocking_candidate_pairs,
    build_embedding_alias_map,
    choose_cluster_representative,
    cluster_labels,
    cosine_similarity,
    should_allow_embedding_merge,
)


def test_cosine_similarity_identifies_close_vectors() -> None:
    assert round(cosine_similarity([1.0, 0.0], [0.9, 0.1]), 2) == 0.99
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_embedding_alias_map_uses_representative_for_cluster() -> None:
    labels = ["TSMC", "Taiwan Semiconductor Manufacturing Company Limited", "Amazon.com Inc."]
    vectors = [
        [1.0, 0.0, 0.0],
        [0.98, 0.02, 0.0],
        [0.0, 1.0, 0.0],
    ]
    alias_map = build_embedding_alias_map(labels, vectors, threshold=0.95)
    assert alias_map["TSMC"] == "Taiwan Semiconductor Manufacturing Company Limited"
    assert "Amazon.com Inc." not in alias_map


def test_embedding_alias_map_does_not_merge_distinct_legal_entities() -> None:
    labels = ["Intel Corporation", "Intel Americas, Inc", "Xilinx Development Corporation", "Xilinx Holding LLC"]
    vectors = [[1.0, 0.0, 0.0] for _ in labels]
    alias_map = build_embedding_alias_map(labels, vectors, threshold=0.95)
    assert alias_map == {}


def test_embedding_alias_map_merges_footnote_variants() -> None:
    labels = ["Xilinx Development Corporation", "Xilinx Development Corporation (1"]
    vectors = [[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]]
    alias_map = build_embedding_alias_map(labels, vectors, threshold=0.95)
    assert alias_map["Xilinx Development Corporation (1"] == "Xilinx Development Corporation"


def test_embedding_merge_gate_allows_aliases_but_blocks_subsidiaries() -> None:
    assert should_allow_embedding_merge("TSMC", "Taiwan Semiconductor Manufacturing Company Limited", 0.99)
    assert should_allow_embedding_merge("Xilinx Development Corporation", "Xilinx Development Corporation (1", 0.99)
    assert not should_allow_embedding_merge("Intel Corporation", "Intel Americas, Inc", 0.99)
    assert not should_allow_embedding_merge("NextEra Energy Inc", "NextEra Energy Capital Holdings", 0.99)
    assert not should_allow_embedding_merge("Direct Customer A", "Direct Customer B", 0.99)
    assert not should_allow_embedding_merge("Advanced Micro Devices AB", "Advanced Micro Devices S.p.A", 0.99)
    assert not should_allow_embedding_merge("Equinix Hyperscale 1 GK", "Equinix Hyperscale 2 (PA12) SAS", 0.99)
    assert not should_allow_embedding_merge(
        "Equinix Hyperscale 2 Holdings B.V",
        "Equinix Hyperscale 2 Holdings A B.V",
        0.99,
    )
    assert not should_allow_embedding_merge(
        "Dell Equipment Finance Trust 2018-2",
        "Dell Equipment Finance Trust 2024-1",
        0.99,
    )
    assert not should_allow_embedding_merge("Equinix Services, Inc", "Equinix (Services) Limited", 0.99)
    assert not should_allow_embedding_merge("Vertiv Group Corporation", "Vertiv Company Group Limited", 0.99)
    assert not should_allow_embedding_merge("E&I Engineering Limited", "E&I Engineering Corporation", 0.99)


def test_cluster_labels_keeps_distant_labels_separate() -> None:
    clusters = cluster_labels(["a", "b"], [[1.0, 0.0], [0.0, 1.0]], threshold=0.9)
    assert clusters == [["a"], ["b"]]


def test_blocking_candidate_pairs_links_lexical_alias_candidates() -> None:
    labels = [
        "Taiwan Semiconductor Manufacturing Company Limited",
        "Taiwan Semiconductor",
        "Amazon.com Inc.",
    ]
    pairs = blocking_candidate_pairs(labels)
    assert (0, 1) in pairs
    assert (0, 2) not in pairs


def test_ann_candidate_pairs_switches_from_exact_to_blocked_lsh() -> None:
    labels = [f"Company {idx} Inc." for idx in range(1005)]
    labels[10] = "Taiwan Semiconductor Manufacturing Company Limited"
    labels[500] = "Taiwan Semiconductor"
    vectors = [[1.0, 0.0, 0.0] for _ in labels]
    pairs, stats = ann_candidate_pairs(labels, vectors, exact_pair_limit=1000, max_bucket_size=16)
    assert stats["ann_mode"] == "blocked_lsh"
    assert (10, 500) in pairs
    assert len(pairs) < (len(labels) * (len(labels) - 1)) // 2


def test_embedding_alias_map_records_ann_diagnostics() -> None:
    labels = ["TSMC", "Taiwan Semiconductor Manufacturing Company Limited", "Amazon.com Inc."]
    vectors = [
        [1.0, 0.0, 0.0],
        [0.98, 0.02, 0.0],
        [0.0, 1.0, 0.0],
    ]
    diagnostics = []
    build_embedding_alias_map(labels, vectors, threshold=0.95, diagnostics=diagnostics)
    assert diagnostics[0]["action"] == "ann_index"
    assert diagnostics[0]["ann_mode"] == "exact_small"


def test_cluster_representative_prefers_canonical_company_over_fragment() -> None:
    representative = choose_cluster_representative(["Contents NVIDIA Corporation", "NVIDIA Corporation"])
    assert representative == "NVIDIA Corporation"
