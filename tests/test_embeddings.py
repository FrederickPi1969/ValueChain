from valuechain.embeddings import build_embedding_alias_map, cluster_labels, cosine_similarity


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


def test_cluster_labels_keeps_distant_labels_separate() -> None:
    clusters = cluster_labels(["a", "b"], [[1.0, 0.0], [0.0, 1.0]], threshold=0.9)
    assert clusters == [["a"], ["b"]]
