"""One reading of a competitor target, and one refusal when it is not a URL.

``urlsplit`` raises a bare ``ValueError`` on a netloc with an unclosed bracket:
``http://[`` is enough. Neither facade recognises that as a refusal, because
both watch for ``GkaiError`` -- the CLI prints a traceback and MCP reports an
opaque tool failure with the reason stripped, so a mistyped domain reads as a
crash in the tool rather than as a typo. The two lines that split a target were
copied into the competitor use case and the competitor scenario, which is why
one typo broke in two places at once; they live here now, once.
"""

from urllib.parse import SplitResult, urlsplit

from google_keyword_ai.errors import InvalidConfigurationError


def split_target(target: str) -> SplitResult:
    """Split a competitor target, refusing one that is no URL at all."""
    candidate = target.strip()
    try:
        return urlsplit(candidate if "://" in candidate else f"//{candidate}")
    except ValueError as exc:
        # The target is not echoed back. A URL carries userinfo and query
        # parameters, the envelope is printed and stored with the run, and the
        # string that failed to parse cannot be stripped of them safely --
        # parsing it is exactly what just failed. `_safe_url` drops the query
        # for the same reason wherever a URL does parse.
        raise InvalidConfigurationError("Target is not a valid URL or domain.") from exc


def is_bare_domain(target: str) -> bool:
    """Report whether the target names a whole site rather than one page of it."""
    parsed = split_target(target)
    return bool(parsed.hostname) and parsed.path in {"", "/"} and not parsed.query
