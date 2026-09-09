"""Provider-independent durable retry contract (messages must be safe to persist)."""


class RetryableProviderError(RuntimeError):
    def __init__(self, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


class PermanentProviderError(RuntimeError):
    pass


class MalformedProviderResponse(RuntimeError):
    pass
