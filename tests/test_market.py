import pytest
from pydantic import ValidationError

from google_keyword_ai.errors import InvalidConfigurationError, ProviderUnavailableError
from google_keyword_ai.market import Market


def test_market_normalizes_and_maps_russian_market() -> None:
    market = Market.parse("RU", "ru")

    assert market.language == "ru"
    assert market.country == "RU"
    assert market.autocomplete_params() == {"hl": "ru", "gl": "RU"}
    assert market.trends_geo() == "RU"
    assert market.gsc_country() == "rus"


@pytest.mark.parametrize(
    ("language", "country", "gsc_country"),
    [("th", "TH", "tha"), ("en", "US", "usa"), ("pt", "BR", "bra")],
)
def test_market_maps_supported_countries(language: str, country: str, gsc_country: str) -> None:
    assert Market.parse(language, country).gsc_country() == gsc_country


def test_unknown_country_is_invalid_configuration() -> None:
    with pytest.raises(InvalidConfigurationError, match="country"):
        Market.parse("en", "ZZ")


def test_unknown_language_is_invalid_configuration() -> None:
    with pytest.raises(InvalidConfigurationError, match="language"):
        Market.parse("xx", "US")


def test_ads_criteria_id_is_deferred_to_m4() -> None:
    with pytest.raises(ProviderUnavailableError, match="M4"):
        Market.parse("ru", "RU").ads_criteria_id()


def test_market_is_frozen() -> None:
    market = Market.parse("en", "US")

    with pytest.raises(ValidationError):
        market.__setattr__("country", "GB")
