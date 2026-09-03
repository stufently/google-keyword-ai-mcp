"""One refusal for a limit that can never select anything.

A limit of zero or less is not a small request but an impossible one: sliced
against any list it yields nothing at all. Left unchecked it does not fail --
it succeeds emptily, and the answer reads as "this niche has no keywords"
when the truth is "you asked for none of them". Two commands already refused
it and five did not, so the same argument meant different things depending on
which one received it.
"""

from google_keyword_ai.errors import InvalidConfigurationError


def require_positive_limit(limit: int | None, subject: str) -> None:
    """Refuse a non-positive limit, naming the limit the caller passed."""
    if limit is not None and limit <= 0:
        raise InvalidConfigurationError(f"{subject} limit must be positive.")
