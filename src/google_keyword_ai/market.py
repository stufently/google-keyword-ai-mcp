from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator

from google_keyword_ai.errors import InvalidConfigurationError, ProviderUnavailableError

COUNTRY_ALPHA3: dict[str, str] = {
    "RU": "RUS",
    "TH": "THA",
    "US": "USA",
    "GB": "GBR",
    "DE": "DEU",
    "FR": "FRA",
    "ES": "ESP",
    "IT": "ITA",
    "KZ": "KAZ",
    "BY": "BLR",
    "UA": "UKR",
    "PL": "POL",
    "TR": "TUR",
    "AE": "ARE",
    "CN": "CHN",
    "JP": "JPN",
    "IN": "IND",
    "BR": "BRA",
}

# Official Google Ads geotargets-2026-08-12.csv, captured 2026-09-02.
ADS_COUNTRY_CRITERIA: dict[str, int] = {
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

# Official Google Ads codes-formats page, captured 2026-09-02.
# The project's single `zh` code maps deliberately to Google's zh_CN constant.
ADS_LANGUAGE_CONSTANTS: dict[str, int] = {
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

SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"ru", "en", "th", "de", "fr", "es", "it", "kk", "uk", "pl", "tr", "ar", "zh", "ja", "hi", "pt"}
)


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    language: str
    country: str

    @field_validator("language", mode="before")
    @classmethod
    def validate_language(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise InvalidConfigurationError("Market language must be a string.")
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_LANGUAGES:
            raise InvalidConfigurationError(f"Unknown language code: {value}")
        return normalized

    @field_validator("country", mode="before")
    @classmethod
    def validate_country(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise InvalidConfigurationError("Market country must be a string.")
        normalized = value.strip().upper()
        if normalized not in COUNTRY_ALPHA3:
            raise InvalidConfigurationError(f"Unknown country code: {value}")
        return normalized

    @classmethod
    def parse(cls, language: str, country: str) -> Self:
        return cls(language=language, country=country)

    def autocomplete_params(self) -> dict[str, str]:
        return {"hl": self.language, "gl": self.country}

    def trends_geo(self) -> str:
        return self.country

    def gsc_country(self) -> str:
        return COUNTRY_ALPHA3[self.country].lower()

    def ads_criteria_id(self) -> int:
        try:
            return ADS_COUNTRY_CRITERIA[self.country]
        except KeyError as exc:
            raise ProviderUnavailableError(
                f"Google Ads has no configured criteria ID for country {self.country}."
            ) from exc

    def ads_language_id(self) -> int:
        try:
            return ADS_LANGUAGE_CONSTANTS[self.language]
        except KeyError as exc:
            raise ProviderUnavailableError(
                f"Google Ads has no language constant for language {self.language}."
            ) from exc

    def ads_geo_target_resource(self) -> str:
        return f"geoTargetConstants/{self.ads_criteria_id()}"

    def ads_language_resource(self) -> str:
        return f"languageConstants/{self.ads_language_id()}"
