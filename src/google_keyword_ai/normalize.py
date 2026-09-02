import unicodedata
from collections.abc import Iterable

from pydantic import BaseModel


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def normalize_keyword(text: str, *, collapse_punctuation: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if collapse_punctuation:
        normalized = "".join(
            " " if unicodedata.category(character).startswith("P") else character
            for character in normalized
        )
    return _collapse_whitespace(normalized)


class KeywordCandidate(BaseModel):
    raw: str
    normalized: str
    discovered_from: list[str]
    relevance: int | None = None


def deduplicate(candidates: Iterable[KeywordCandidate]) -> list[KeywordCandidate]:
    result: list[KeywordCandidate] = []
    by_normalized: dict[str, KeywordCandidate] = {}

    for candidate in candidates:
        existing = by_normalized.get(candidate.normalized)
        if existing is None:
            copied = candidate.model_copy(deep=True)
            by_normalized[candidate.normalized] = copied
            result.append(copied)
            continue

        known_sources = set(existing.discovered_from)
        for source in candidate.discovered_from:
            if source not in known_sources:
                existing.discovered_from.append(source)
                known_sources.add(source)
        if candidate.relevance is not None and (
            existing.relevance is None or candidate.relevance > existing.relevance
        ):
            existing.relevance = candidate.relevance

    return result
