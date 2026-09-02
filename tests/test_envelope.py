import pytest

from google_keyword_ai.envelope import SCHEMA_VERSION, Completeness, Envelope
from google_keyword_ai.errors import InvalidConfigurationError


def test_to_wire_returns_json_ready_envelope() -> None:
    envelope = Envelope[dict[str, str]](data={"status": "ok"})

    assert envelope.to_wire() == {
        "schema_version": SCHEMA_VERSION,
        "data": {"status": "ok"},
        "warnings": [],
        "errors": [],
        "completeness": "complete",
        "completeness_reason": None,
        "run_id": None,
    }


@pytest.mark.parametrize("completeness", [Completeness.PARTIAL, Completeness.EMPTY])
def test_incomplete_envelope_requires_reason(completeness: Completeness) -> None:
    with pytest.raises(InvalidConfigurationError, match="completeness_reason"):
        Envelope[str](data="result", completeness=completeness)


def test_incomplete_envelope_accepts_reason() -> None:
    envelope = Envelope[str](
        data="result",
        completeness=Completeness.PARTIAL,
        completeness_reason="database unavailable",
    )

    assert envelope.to_wire()["completeness_reason"] == "database unavailable"
