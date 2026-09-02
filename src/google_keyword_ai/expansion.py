import tomllib
from collections.abc import Sequence
from enum import StrEnum
from importlib import resources

from pydantic import BaseModel, ConfigDict

from google_keyword_ai import data
from google_keyword_ai.errors import InvalidConfigurationError


class ExpansionStrategy(StrEnum):
    SUFFIX_ALPHABET = "suffix_alphabet"
    PREFIX_ALPHABET = "prefix_alphabet"
    DIGITS = "digits"
    MODIFIERS = "modifiers"


class ExpansionQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    strategy: ExpansionStrategy
    seed: str


def _data_file(directory: str, language: str, suffix: str) -> resources.abc.Traversable:
    normalized_language = language.strip().lower()
    requested = resources.files(data).joinpath(directory, f"{normalized_language}.{suffix}")
    if requested.is_file():
        return requested
    return resources.files(data).joinpath(directory, f"en.{suffix}")


def load_alphabet(language: str) -> list[str]:
    """Load a packaged alphabet, deliberately falling back to English."""
    alphabet_file = _data_file("alphabets", language, "txt")
    return [line for line in alphabet_file.read_text(encoding="utf-8").splitlines() if line]


def load_modifiers(language: str) -> dict[str, dict[str, list[str]]]:
    """Load packaged modifiers, deliberately falling back to English."""
    modifier_file = _data_file("modifiers", language, "toml")
    loaded = tomllib.loads(modifier_file.read_text(encoding="utf-8"))
    result: dict[str, dict[str, list[str]]] = {}
    for category, raw_section in loaded.items():
        if not isinstance(raw_section, dict):
            raise InvalidConfigurationError(f"Modifier category {category} must be a table.")
        section: dict[str, list[str]] = {}
        for position, raw_values in raw_section.items():
            if (
                position not in {"prefix", "suffix"}
                or not isinstance(raw_values, list)
                or not all(isinstance(value, str) for value in raw_values)
            ):
                raise InvalidConfigurationError(
                    f"Modifier category {category} has invalid {position} values."
                )
            section[position] = raw_values
        result[category] = section
    return result


def build_queries(
    seed: str,
    language: str,
    strategies: Sequence[ExpansionStrategy],
) -> list[ExpansionQuery]:
    queries: list[ExpansionQuery] = []
    seen_texts: set[str] = {seed}

    def append(text: str, strategy: ExpansionStrategy) -> None:
        if text in seen_texts:
            return
        seen_texts.add(text)
        queries.append(ExpansionQuery(text=text, strategy=strategy, seed=seed))

    for strategy in strategies:
        if strategy is ExpansionStrategy.SUFFIX_ALPHABET:
            for letter in load_alphabet(language):
                append(f"{seed} {letter}", strategy)
        elif strategy is ExpansionStrategy.PREFIX_ALPHABET:
            for letter in load_alphabet(language):
                append(f"{letter} {seed}", strategy)
        elif strategy is ExpansionStrategy.DIGITS:
            for digit in range(10):
                append(f"{seed} {digit}", strategy)
        elif strategy is ExpansionStrategy.MODIFIERS:
            for section in load_modifiers(language).values():
                for modifier in section.get("prefix", []):
                    append(f"{modifier} {seed}", strategy)
                for modifier in section.get("suffix", []):
                    append(f"{seed} {modifier}", strategy)
    return queries
