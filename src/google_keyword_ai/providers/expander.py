from collections.abc import Sequence
from typing import Self

import anyio
from pydantic import BaseModel, model_validator

from google_keyword_ai.errors import GkaiError, InvalidConfigurationError
from google_keyword_ai.expansion import ExpansionStrategy, build_queries
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate, deduplicate, normalize_keyword
from google_keyword_ai.providers.autocomplete import AutocompleteProvider


class ExpansionLimits(BaseModel):
    max_depth: int = 1
    max_queries: int = 500
    max_results: int = 2000
    max_runtime_seconds: float = 120.0

    @model_validator(mode="after")
    def validate_positive_limits(self) -> Self:
        if (
            self.max_depth <= 0
            or self.max_queries <= 0
            or self.max_results <= 0
            or self.max_runtime_seconds <= 0
        ):
            raise InvalidConfigurationError("Expansion limits must all be positive.")
        return self


class ExpansionStats(BaseModel):
    queries_executed: int
    depth_reached: int
    stopped_by: str | None = None
    queries_failed: int = 0
    """Requests that failed and were skipped.

    Skipping is deliberate -- one dead query must not sink a fan-out of a
    hundred -- but skipping in silence is not: eighty failures out of
    eighty-eight leave a thin result that still looks whole. The count is what
    lets the caller tell "the niche is small" from "the source was down".
    """


class KeywordExpander:
    def __init__(self, provider: AutocompleteProvider, limits: ExpansionLimits) -> None:
        self._provider = provider
        self._limits = limits

    async def expand(
        self,
        seed: str,
        market: Market,
        *,
        strategies: Sequence[ExpansionStrategy],
    ) -> tuple[list[KeywordCandidate], ExpansionStats]:
        started_at = anyio.current_time()
        queries_executed = 0
        queries_failed = 0
        depth_reached = 0
        all_candidates: list[KeywordCandidate] = []
        unique_candidates: set[str] = set()
        seen_seeds = {normalize_keyword(seed)}
        current_seeds = [seed]
        current_depth = 0

        def stopped_by_guard() -> str | None:
            if queries_executed >= self._limits.max_queries:
                return "max_queries"
            if len(unique_candidates) >= self._limits.max_results:
                return "max_results"
            if anyio.current_time() - started_at >= self._limits.max_runtime_seconds:
                return "max_runtime"
            return None

        while current_seeds:
            depth_reached = current_depth
            depth_candidates: list[KeywordCandidate] = []
            for current_seed in current_seeds:
                planned_queries = [(current_seed, "seed")]
                planned_queries.extend(
                    (query.text, query.strategy.value)
                    for query in build_queries(current_seed, market.language, strategies)
                )
                for query_text, strategy_name in planned_queries:
                    stopped_by = stopped_by_guard()
                    if stopped_by is not None:
                        return deduplicate(all_candidates), ExpansionStats(
                            queries_executed=queries_executed,
                            depth_reached=depth_reached,
                            stopped_by=stopped_by,
                            queries_failed=queries_failed,
                        )

                    queries_executed += 1
                    try:
                        suggestions = await self._provider.suggest(query_text, market)
                    except GkaiError:
                        if current_depth == 0 and current_seed == seed and strategy_name == "seed":
                            raise
                        queries_failed += 1
                        continue

                    source = f"autocomplete:{strategy_name}:{query_text}"
                    for suggestion in suggestions:
                        candidate = KeywordCandidate(
                            raw=suggestion.text,
                            normalized=normalize_keyword(suggestion.text),
                            discovered_from=[source],
                            relevance=suggestion.relevance,
                        )
                        all_candidates.append(candidate)
                        depth_candidates.append(candidate)
                        unique_candidates.add(candidate.normalized)

            all_candidates = deduplicate(all_candidates)
            next_seeds: list[str] = []
            for candidate in deduplicate(depth_candidates):
                if candidate.normalized in seen_seeds:
                    continue
                seen_seeds.add(candidate.normalized)
                next_seeds.append(candidate.normalized)

            if not next_seeds:
                break
            # max_depth counts ROUNDS of fan-out, so depth index 0 is already the
            # first one. Comparing the index itself would run N+1 rounds and make
            # "--depth 1" cost twice what the user asked for.
            if current_depth + 1 >= self._limits.max_depth:
                return all_candidates, ExpansionStats(
                    queries_executed=queries_executed,
                    depth_reached=depth_reached,
                    stopped_by="max_depth",
                    queries_failed=queries_failed,
                )
            current_seeds = next_seeds
            current_depth += 1

        return all_candidates, ExpansionStats(
            queries_executed=queries_executed,
            depth_reached=depth_reached,
            stopped_by=None,
            queries_failed=queries_failed,
        )
