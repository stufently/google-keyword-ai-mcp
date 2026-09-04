from pathlib import Path

import httpx
import respx

from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.providers.autocomplete import PRIMARY_ENDPOINT
from google_keyword_ai.usecases.suggest import run_suggest


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir, http_max_attempts=1)


def _params(client_name: str = "chrome") -> dict[str, str]:
    return {
        "client": client_name,
        "ie": "utf-8",
        "oe": "utf-8",
        "q": "seed",
        "hl": "en",
        "gl": "US",
    }


def test_success_returns_complete_envelope(data_dir: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(PRIMARY_ENDPOINT, params=_params()).mock(
            return_value=httpx.Response(
                200,
                json=["seed", ["seed one"], [], {}, {"google:suggestrelevance": [900]}],
            )
        )
        envelope = run_suggest(_settings(data_dir), "seed", language="en", country="US")

    assert envelope.completeness is Completeness.COMPLETE
    assert envelope.errors == []
    assert envelope.data.suggestions[0].text == "seed one"
    assert envelope.data.suggestions[0].relevance == 900


def test_rate_limit_returns_empty_envelope_without_raising(data_dir: Path) -> None:
    """A refusal to serve is not answered by asking the other host.

    Only the primary endpoint is registered on purpose: the fallback exists for
    an endpoint that broke, and reaching for it here would double the request
    count precisely while Google is asking the run to slow down.
    """
    with respx.mock(assert_all_called=True) as router:
        primary = router.get(PRIMARY_ENDPOINT, params=_params()).mock(
            return_value=httpx.Response(429)
        )
        envelope = run_suggest(_settings(data_dir), "seed")

    assert primary.call_count == 1
    assert envelope.completeness is Completeness.EMPTY
    assert envelope.data.suggestions == []
    assert envelope.errors
    assert envelope.completeness_reason


def test_successful_empty_response_has_no_errors(data_dir: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(PRIMARY_ENDPOINT, params=_params()).mock(
            return_value=httpx.Response(200, json=["seed", []])
        )
        envelope = run_suggest(_settings(data_dir), "seed")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "no suggestions"
    assert envelope.errors == []
