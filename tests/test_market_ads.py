import pytest

from google_keyword_ai.errors import ProviderUnavailableError
from google_keyword_ai.market import (
    ADS_COUNTRY_CRITERIA,
    ADS_LANGUAGE_CONSTANTS,
    Market,
)


def test_ads_tables_match_supported_google_values() -> None:
    assert ADS_COUNTRY_CRITERIA == {
        "AE": 2784,
        "BR": 2076,
        "BY": 2112,
        "CN": 2156,
        "DE": 2276,
        "ES": 2724,
        "FR": 2250,
        "GB": 2826,
        "IN": 2356,
        "IT": 2380,
        "JP": 2392,
        "KZ": 2398,
        "PL": 2616,
        "RU": 2643,
        "TH": 2764,
        "TR": 2792,
        "UA": 2804,
        "US": 2840,
    }
    assert ADS_LANGUAGE_CONSTANTS == {
        "ar": 1019,
        "de": 1001,
        "en": 1000,
        "es": 1003,
        "fr": 1002,
        "hi": 1023,
        "it": 1004,
        "ja": 1005,
        "pl": 1030,
        "pt": 1014,
        "ru": 1031,
        "th": 1044,
        "tr": 1037,
        "uk": 1036,
        "zh": 1017,
        "zh_CN": 1017,
        "zh_TW": 1018,
    }


def test_russian_ads_resources() -> None:
    market = Market.parse("ru", "RU")

    assert market.ads_criteria_id() == 2643
    assert market.ads_language_id() == 1031
    assert market.ads_geo_target_resource() == "geoTargetConstants/2643"
    assert market.ads_language_resource() == "languageConstants/1031"


def test_kazakh_language_is_not_available_in_google_ads() -> None:
    with pytest.raises(ProviderUnavailableError, match="kk"):
        Market.parse("kk", "KZ").ads_language_id()


def test_unmapped_country_is_reported() -> None:
    market = Market.model_construct(language="en", country="ZZ")

    with pytest.raises(ProviderUnavailableError, match="ZZ"):
        market.ads_criteria_id()
