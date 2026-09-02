import pytest

from google_keyword_ai.expansion import (
    ExpansionStrategy,
    build_queries,
    load_alphabet,
    load_modifiers,
)


@pytest.mark.parametrize(("language", "length"), [("ru", 33), ("en", 26)])
def test_alphabet_has_expected_length(language: str, length: int) -> None:
    assert len(load_alphabet(language)) == length


def test_unknown_alphabet_falls_back_to_english() -> None:
    assert load_alphabet("de") == load_alphabet("en")
    assert load_modifiers("de") == load_modifiers("en")


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (ExpansionStrategy.SUFFIX_ALPHABET, "seed a"),
        (ExpansionStrategy.PREFIX_ALPHABET, "a seed"),
        (ExpansionStrategy.DIGITS, "seed 0"),
        (ExpansionStrategy.MODIFIERS, "how seed"),
    ],
)
def test_queries_include_each_strategy(
    strategy: ExpansionStrategy,
    expected: str,
) -> None:
    queries = build_queries("seed", "en", [strategy])

    assert expected in [query.text for query in queries]
    assert all(query.strategy is strategy for query in queries)


def test_queries_never_include_seed_itself() -> None:
    queries = build_queries("seed", "en", list(ExpansionStrategy))

    assert "seed" not in [query.text for query in queries]


def test_queries_are_deduplicated_in_first_seen_order() -> None:
    queries = build_queries(
        "seed",
        "en",
        [ExpansionStrategy.MODIFIERS, ExpansionStrategy.MODIFIERS],
    )
    texts = [query.text for query in queries]

    assert texts == list(dict.fromkeys(texts))
    assert texts.count("order seed") == 1
