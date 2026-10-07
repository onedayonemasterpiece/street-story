"""Provider-independent durable retry contract (messages must be safe to persist)."""


class RetryableProviderError(RuntimeError):
    def __init__(self, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


class PermanentProviderError(RuntimeError):
    pass


class MalformedProviderResponse(RuntimeError):
    pass


def research_retry_at(reason: str, now: float, requested: float | None = None) -> float:
    """Local scheduler waits, not provider quota or a grant to send.

    Local daily admission is rechecked within five minutes: policy may change
    without a provider quota reset. It remains an admission probe, not a send.
    Binding failures need changed owner epoch/profile; 1h bounds scheduler
    rechecks of that fence.
    Configuration/control unavailable routes use a 5m operational cooldown.
    A longer authoritative provider hint wins outside local daily admission.
    Unknown readback retains its own retry deadline and is never reclassified
    as a fresh model attempt.
    """
    import math
    requested = requested if isinstance(requested, (int, float)) and not isinstance(requested, bool) and math.isfinite(requested) else now + 60
    code = str(reason).lower()
    aggregate_wait = code.startswith('all_') or ':all_' in code or '.all_' in code
    if code == 'resource_daily_budget':
        return now + 300
    elif not aggregate_wait and code.endswith('binding_changed'):
        minimum = now + 3600
    elif not aggregate_wait and code.endswith('_unavailable'):
        minimum = now + 300
    else:
        minimum = now + 1
    return max(minimum, requested)
