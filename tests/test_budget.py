import anyio
import pytest

from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard


def test_each_named_budget_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(anyio, "current_time", lambda: clock[0])
    guard = BudgetGuard(
        Budget(
            max_keywords=2,
            max_autocomplete_queries=2,
            max_ads_calls=2,
            max_trends_calls=2,
            max_runtime_seconds=2.0,
        )
    )

    for kind, reason in (
        ("keywords", "max_keywords"),
        ("autocomplete", "max_autocomplete_queries"),
        ("ads", "max_ads_calls"),
        ("trends", "max_trends_calls"),
    ):
        isolated = BudgetGuard(guard.budget)
        isolated.spend(kind, 2)
        assert isolated.can_spend(kind) is False
        assert isolated.exhausted_reason() == reason

    timed = BudgetGuard(guard.budget)
    assert timed.can_spend("ads") is True
    clock[0] = 12.0
    assert timed.can_spend("ads") is False
    assert timed.exhausted_reason() == "max_runtime_seconds"


def test_spending_the_whole_allowance_is_not_a_cut() -> None:
    """Reaching a limit is not evidence that anything was lost.

    `exhausted_reason` used to answer on `spend >= limit`, so a run that needed
    exactly one Google Ads batch under `max_ads_calls=1` was reported as
    `partial / stopped by max_ads_calls` even though it had finished the work
    it set out to do, and the CLI exited 1 for a healthy run.
    """

    async def exercise() -> None:
        guard = BudgetGuard(Budget(max_ads_calls=1))
        guard.spend("ads")
        assert guard.exhausted_reason() is None

    anyio.run(exercise)


def test_a_refused_operation_is_a_cut() -> None:
    async def exercise() -> None:
        guard = BudgetGuard(Budget(max_ads_calls=1))
        guard.spend("ads")
        assert guard.can_spend("ads") is False
        assert guard.exhausted_reason() == "max_ads_calls"

    anyio.run(exercise)


def test_truncating_a_result_is_a_cut() -> None:
    """Slicing a list to a limit drops data without asking permission."""

    async def exercise() -> None:
        guard = BudgetGuard(Budget(max_keywords=2))
        guard.spend("keywords", 2)
        assert guard.exhausted_reason() is None
        guard.mark_cut("keywords")
        assert guard.exhausted_reason() == "max_keywords"

    anyio.run(exercise)


def test_unknown_budget_kind_is_rejected() -> None:
    async def exercise() -> None:
        with pytest.raises(InvalidConfigurationError, match="Unknown budget spend kind"):
            BudgetGuard(Budget()).can_spend("other")

    anyio.run(exercise)


@pytest.mark.parametrize(
    "field",
    [
        "max_keywords",
        "max_autocomplete_queries",
        "max_ads_calls",
        "max_trends_calls",
        "max_runtime_seconds",
    ],
)
def test_nonpositive_budget_values_are_rejected(field: str) -> None:
    with pytest.raises(InvalidConfigurationError, match="positive"):
        Budget.model_validate({field: 0})
