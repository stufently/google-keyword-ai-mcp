class GkaiError(Exception):
    """Base exception for errors exposed by google-keyword-ai."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        self.message = message
        self.details = {} if details is None else dict(details)
        super().__init__(message)


class AuthenticationError(GkaiError):
    pass


class RateLimitError(GkaiError):
    pass


class ProviderUnavailableError(GkaiError):
    pass


class InvalidConfigurationError(GkaiError):
    pass


class NetworkError(GkaiError):
    pass


class ApiError(GkaiError):
    pass


class PartialResultError(GkaiError):
    pass
