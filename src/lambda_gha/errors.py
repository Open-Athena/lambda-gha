"""Error classes for lambda-gha."""

from dataclasses import dataclass, field


class LambdaGHAError(Exception):
    """Base exception for lambda-gha errors."""


class CapacityError(LambdaGHAError):
    """Raised when instance launch fails due to insufficient capacity."""

    def __init__(self, instance_type: str, region: str, message: str = ""):
        self.instance_type = instance_type
        self.region = region
        self.message = message or f"No capacity for {instance_type} in {region}"
        super().__init__(self.message)


class RateLimitError(LambdaGHAError):
    """Raised when API request is rate limited."""

    def __init__(self, message: str = "Rate limited", retry_after: int = None):
        self.retry_after = retry_after
        super().__init__(message)


class ConfigurationError(LambdaGHAError):
    """Raised for invalid configuration (non-retryable)."""


@dataclass
class LaunchAttempt:
    """Record of a single instance launch attempt."""

    instance_type: str
    region: str
    attempt: int
    success: bool = False
    error: str = ""
    instance_id: str = ""


@dataclass
class AllCapacityExhaustedError(LambdaGHAError):
    """Raised when all instance type/region combinations have been exhausted."""

    attempts: list[LaunchAttempt] = field(default_factory=list)

    def __post_init__(self):
        types_tried = sorted(set(a.instance_type for a in self.attempts))
        regions_tried = sorted(set(a.region for a in self.attempts))
        self.message = (
            f"All capacity exhausted. "
            f"Tried {len(self.attempts)} combinations: "
            f"types={types_tried}, regions={regions_tried}"
        )
        super().__init__(self.message)


# Error codes from Lambda API that indicate capacity issues (retryable with different type/region)
CAPACITY_ERROR_CODES = {
    "insufficient-capacity",
    "insufficient_capacity",
    "no-capacity",
    "no_capacity",
}

# Error codes that indicate rate limiting (retryable with backoff)
RATE_LIMIT_ERROR_CODES = {
    "rate-limit",
    "rate_limit",
    "too-many-requests",
    "too_many_requests",
}

# Error codes that are not retryable
NON_RETRYABLE_ERROR_CODES = {
    "invalid-instance-type",
    "invalid_instance_type",
    "invalid-region",
    "invalid_region",
    "authentication-error",
    "authentication_error",
    "quota-exceeded",
    "quota_exceeded",
    "invalid-ssh-key",
    "invalid_ssh_key",
}


def classify_api_error(error_response: dict) -> LambdaGHAError:
    """Classify a Lambda API error response into the appropriate exception type.

    Parameters
    ----------
    error_response : dict
        The error response from the Lambda API, typically containing
        'error' with 'code' and 'message' fields.

    Returns
    -------
    LambdaGHAError
        The appropriate exception type for the error.
    """
    error = error_response.get("error", {})
    code = error.get("code", "").lower().replace(" ", "-")
    message = error.get("message", str(error_response))

    # Check for capacity errors
    if code in CAPACITY_ERROR_CODES or "capacity" in message.lower():
        return CapacityError("", "", message)

    # Check for rate limit errors
    if code in RATE_LIMIT_ERROR_CODES or "rate" in message.lower():
        retry_after = error.get("retry_after")
        return RateLimitError(message, retry_after)

    # Check for non-retryable errors
    if code in NON_RETRYABLE_ERROR_CODES:
        return ConfigurationError(message)

    # Default to base error
    return LambdaGHAError(message)
