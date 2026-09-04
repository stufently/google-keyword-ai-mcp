from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel

from google_keyword_ai.config import Settings
from google_keyword_ai.normalize import normalize_keyword


class KeywordCluster(BaseModel):
    """One group of keywords, or the remainder that formed no group.

    `is_remainder` marks the leftovers bucket. It used to be recognised by its
    label alone, which made two headline numbers count it as a cluster while a
    third excluded it, and misread a real cluster whose shared tokens happened
    to spell the sentinel.
    """

    label: str
    keywords: list[str]
    size: int
    shared_tokens: list[str]
    is_remainder: bool = False


def tokenize(text: str) -> list[str]:
    return normalize_keyword(text).split()


def similarity(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _shared_tokens(keywords: Sequence[str]) -> list[str]:
    token_lists = [tokenize(keyword) for keyword in keywords]
    if not token_lists:
        return []
    shared = set(token_lists[0])
    for tokens in token_lists[1:]:
        shared.intersection_update(tokens)
    counts = Counter(token for tokens in token_lists for token in tokens if token in shared)
    return sorted(shared, key=lambda token: (-counts[token], token))


def _build_cluster(
    keywords: list[str], *, label: str | None = None, is_remainder: bool = False
) -> KeywordCluster:
    shared = _shared_tokens(keywords)
    return KeywordCluster(
        label=label if label is not None else (" ".join(shared) if shared else keywords[0]),
        keywords=keywords,
        size=len(keywords),
        shared_tokens=shared,
        is_remainder=is_remainder,
    )


def cluster_keywords(keywords: Sequence[str], settings: Settings) -> list[KeywordCluster]:
    groups: list[list[str]] = []
    token_groups: list[list[list[str]]] = []
    for keyword in keywords:
        tokens = tokenize(keyword)
        for group, members in zip(groups, token_groups, strict=True):
            if all(
                similarity(tokens, member) >= settings.cluster_similarity_threshold
                for member in members
            ):
                group.append(keyword)
                members.append(tokens)
                break
        else:
            groups.append([keyword])
            token_groups.append([tokens])

    retained: list[KeywordCluster] = []
    unclustered: list[str] = []
    for group in groups:
        if len(group) < settings.cluster_min_size:
            unclustered.extend(group)
        else:
            retained.append(_build_cluster(group))
    if unclustered:
        retained.append(_build_cluster(unclustered, label="unclustered", is_remainder=True))
    return retained
