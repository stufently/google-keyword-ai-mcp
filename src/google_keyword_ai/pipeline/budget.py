from collections.abc import Callable
from typing import Self

import anyio
from pydantic import BaseModel, PrivateAttr, model_validator

from google_keyword_ai.errors import InvalidConfigurationError


class Budget(BaseModel):
    max_keywords: int = 2000
    max_autocomplete_queries: int = 500
    max_ads_calls: int = 20
    max_trends_calls: int = 3
    max_runtime_seconds: float = 300.0

    @model_validator(mode="after")
    def validate_positive_limits(self) -> Self:
        if any(
            value <= 0
            for value in (
                self.max_keywords,
                self.max_autocomplete_queries,
                self.max_ads_calls,
                self.max_trends_calls,
                self.max_runtime_seconds,
            )
        ):
            raise InvalidConfigurationError("Budget limits must all be positive.")
        return self


class BudgetSpend(BaseModel):
    autocomplete_queries: int = 0
    ads_calls: int = 0
    trends_calls: int = 0
    keywords: int = 0
    elapsed_seconds: float = 0.0

    _spend_callback: Callable[[str, int], None] | None = PrivateAttr(default=None)

    def __call__(self, kind: str, amount: int = 1) -> None:
        if self._spend_callback is None:
            raise InvalidConfigurationError("This BudgetSpend is not attached to a guard.")
        self._spend_callback(kind, amount)


_FIELDS = {
    "autocomplete": ("autocomplete_queries", "max_autocomplete_queries"),
    "ads": ("ads_calls", "max_ads_calls"),
    "trends": ("trends_calls", "max_trends_calls"),
    "keywords": ("keywords", "max_keywords"),
}


class BudgetGuard:
    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        try:
            self._started_at: float | None = anyio.current_time()
        except RuntimeError:
            # Dry-run plans are deliberately built outside an event loop. The
            # guard starts lazily if no AnyIO clock is active at construction.
            self._started_at = None
        self._spend = BudgetSpend()
        self._spend._spend_callback = self._record_spend
        # The limit that actually refused something, or None. Set only when an
        # operation was denied or a result list was truncated -- never merely
        # because a counter reached its limit. A run that needed exactly its
        # allowance and got it is complete, and reporting it as cut short made
        # the exit code useless (see `mark_cut` and `exhausted_reason`).
        self._cut: str | None = None

    def _now(self) -> float:
        now = anyio.current_time()
        if self._started_at is None:
            self._started_at = now
        return now

    def _refresh_elapsed(self) -> None:
        now = self._now()
        assert self._started_at is not None
        self._spend.elapsed_seconds = max(0.0, now - self._started_at)

    @staticmethod
    def _validate_kind(kind: str) -> tuple[str, str]:
        try:
            return _FIELDS[kind]
        except KeyError as exc:
            raise InvalidConfigurationError(f"Unknown budget spend kind: {kind}.") from exc

    @staticmethod
    def _validate_amount(amount: int) -> None:
        if amount <= 0:
            raise InvalidConfigurationError("Budget spend amount must be positive.")

    def can_spend(self, kind: str, amount: int = 1) -> bool:
        """Ask permission for one operation, recording a refusal as a cut.

        Every caller treats a False answer as "skip this work", so a refusal
        here is exactly the moment something is lost. Asking and being allowed
        records nothing, which is what keeps a run that used its whole
        allowance from being reported as truncated.
        """
        spend_field, limit_field = self._validate_kind(kind)
        self._validate_amount(amount)
        self._refresh_elapsed()
        if self._spend.elapsed_seconds >= self.budget.max_runtime_seconds:
            self._cut = "max_runtime_seconds"
            return False
        current = int(getattr(self._spend, spend_field))
        limit = int(getattr(self.budget, limit_field))
        if current + amount > limit:
            self._cut = limit_field
            return False
        return True

    def mark_cut(self, kind: str) -> None:
        """Record that a limit truncated a result the caller already had.

        Slicing a candidate list down to `max_keywords` drops data without ever
        asking permission, so the guard has to be told.
        """
        _, limit_field = self._validate_kind(kind)
        self._cut = limit_field

    def _record_spend(self, kind: str, amount: int = 1) -> None:
        spend_field, _ = self._validate_kind(kind)
        self._validate_amount(amount)
        if not self.can_spend(kind, amount):
            return
        setattr(self._spend, spend_field, int(getattr(self._spend, spend_field)) + amount)
        self._refresh_elapsed()

    def exhausted_reason(self) -> str | None:
        self._refresh_elapsed()
        # Running out of wall clock is reported on its own: past the deadline
        # every further operation would be refused, so the run ended early
        # whether or not it happened to ask for anything else. Count limits get
        # no such treatment -- only a recorded refusal or truncation counts.
        if self._spend.elapsed_seconds >= self.budget.max_runtime_seconds:
            return "max_runtime_seconds"
        return self._cut

    @property
    def spend(self) -> BudgetSpend:
        self._refresh_elapsed()
        return self._spend
