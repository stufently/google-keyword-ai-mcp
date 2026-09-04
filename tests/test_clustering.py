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


def test_the_leftovers_bucket_is_marked_rather_than_named() -> None:
    """The remainder is one of the returned objects but it is not a cluster.

    Recognising it by its label made two headline numbers count it while the
    niche diversity factor excluded it — two answers to one question in the same
    response — and it misread a real cluster whose shared tokens happened to
    spell the sentinel.
    """
    settings = Settings(cluster_similarity_threshold=0.3, cluster_min_size=2)

    clusters = cluster_keywords(["red shoe", "red shoes", "isolated"], settings)

    formed = [cluster for cluster in clusters if not cluster.is_remainder]
    remainder = [cluster for cluster in clusters if cluster.is_remainder]
    assert len(formed) == 1
    assert [cluster.keywords for cluster in remainder] == [["isolated"]]


def test_a_cluster_that_spells_the_sentinel_is_still_a_cluster() -> None:
    """A label is text the data produced, not a marker this code owns."""
    settings = Settings(cluster_similarity_threshold=0.3, cluster_min_size=2)

    clusters = cluster_keywords(["unclustered", "unclustered"], settings)

    assert [cluster.label for cluster in clusters] == ["unclustered"]
    assert clusters[0].is_remainder is False
