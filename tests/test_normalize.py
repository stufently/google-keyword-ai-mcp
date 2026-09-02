from google_keyword_ai.normalize import KeywordCandidate, deduplicate, normalize_keyword


def test_normalize_applies_nfkc_whitespace_casefold_and_cyrillic() -> None:
    text = "  \uff26\uff2f\uff2f\tStraße\n\u0410\u0420\u0415\u041d\u0414\u0410  "
    assert normalize_keyword(text) == "foo strasse аренда"


def test_normalize_can_collapse_punctuation() -> None:
    assert normalize_keyword("Купить—дёшево, сейчас!", collapse_punctuation=True) == (
        "купить дёшево сейчас"
    )


def test_deduplicate_merges_sources_and_maximum_relevance_in_order() -> None:
    candidates = [
        KeywordCandidate(
            raw="Первая форма",
            normalized="общий ключ",
            discovered_from=["seed", "alphabet"],
            relevance=10,
        ),
        KeywordCandidate(
            raw="Вторая форма",
            normalized="общий ключ",
            discovered_from=["alphabet", "intent"],
            relevance=50,
        ),
        KeywordCandidate(
            raw="Другой",
            normalized="другой",
            discovered_from=["seed"],
            relevance=None,
        ),
    ]

    result = deduplicate(candidates)

    assert [candidate.raw for candidate in result] == ["Первая форма", "Другой"]
    assert result[0].discovered_from == ["seed", "alphabet", "intent"]
    assert result[0].relevance == 50
