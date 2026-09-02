from google_keyword_ai.clustering import cluster_keywords, similarity, tokenize
from google_keyword_ai.config import Settings


def test_similar_keywords_join_one_cluster_and_different_keywords_do_not() -> None:
    result = cluster_keywords(
        ["keyword research tool", "keyword research software", "winter boots"],
        Settings(cluster_min_size=1),
    )
    assert result[0].keywords == ["keyword research tool", "keyword research software"]
    assert result[1].keywords == ["winter boots"]


def test_threshold_from_settings_changes_result() -> None:
    keywords = ["red running shoe", "red shoe"]
    low = cluster_keywords(keywords, Settings(cluster_min_size=1, cluster_similarity_threshold=0.5))
    high = cluster_keywords(
        keywords, Settings(cluster_min_size=1, cluster_similarity_threshold=0.8)
    )
    assert len(low) == 1
    assert len(high) == 2


def test_small_clusters_merge_into_unclustered_last() -> None:
    result = cluster_keywords(
        ["red shoe", "red shoes", "isolated"],
        Settings(cluster_similarity_threshold=0.3, cluster_min_size=2),
    )
    assert result[-1].label == "unclustered"
    assert result[-1].keywords == ["isolated"]


def test_clustering_is_deterministic() -> None:
    keywords = ["b a", "a b c", "x y"]
    settings = Settings(cluster_min_size=1)
    assert cluster_keywords(keywords, settings) == cluster_keywords(keywords, settings)


def test_tokenize_and_similarity() -> None:
    assert tokenize("  Red   SHOES ") == ["red", "shoes"]
    assert similarity(["red", "shoe"], ["red", "boot"]) == 1 / 3
    assert similarity([], []) == 0.0
