class LLMConfigurationError(RuntimeError):
    """The live LLM adapter is not configured and no fallback is allowed."""


class LLMOutputValidationError(ValueError):
    """Structured model output failed the evidence or mapping contract."""


class LLMAdapterTimeout(TimeoutError):
    """The configured LLM call exceeded its timeout."""


class LLMProviderError(RuntimeError):
    """The configured LLM provider rejected or could not complete the request."""
