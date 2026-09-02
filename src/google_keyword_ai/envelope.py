from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from google_keyword_ai.errors import InvalidConfigurationError

SCHEMA_VERSION = "1.0.0"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"


class Envelope[T](BaseModel):
    schema_version: str = SCHEMA_VERSION
    data: T
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    completeness: Completeness = Completeness.COMPLETE
    completeness_reason: str | None = None
    run_id: str | None = None

    @model_validator(mode="after")
    def require_completeness_reason(self) -> Self:
        if self.completeness is not Completeness.COMPLETE and not self.completeness_reason:
            raise InvalidConfigurationError(
                "completeness_reason is required when completeness is not complete."
            )
        return self

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")
