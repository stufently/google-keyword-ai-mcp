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
        raise ProviderUnavailableError("Google Ads criteria ID table is not available until M4.")
