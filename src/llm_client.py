"""Unified LLM client: provider-agnostic entry point used by the rest of the app.

BYOK: the API key lives only in st.session_state for the duration of the browser
session - never written to disk, never logged, never persisted to the job DB.
"""

from dataclasses import dataclass

from src.config import ALLOWED_MODELS
from src.providers import anthropic_provider, openai_provider, gemini_provider

_PROVIDERS = {
    "anthropic": anthropic_provider,
    "openai": openai_provider,
    "gemini": gemini_provider,
}


class ModelNotAllowedError(ValueError):
    pass


class UnknownProviderError(ValueError):
    pass


@dataclass
class LLMConfig:
    provider: str  # "anthropic" | "openai" | "gemini"
    model: str
    api_key: str

    def __post_init__(self):
        if self.provider not in _PROVIDERS:
            raise UnknownProviderError(f"Unknown provider: {self.provider}")
        if self.model not in ALLOWED_MODELS[self.provider]:
            raise ModelNotAllowedError(
                f"Model '{self.model}' is not on the allowed list for {self.provider}. "
                f"Allowed: {ALLOWED_MODELS[self.provider]}"
            )


def call(config: LLMConfig, system_prompt: str, user_prompt: str,
         max_tokens: int = 8192, response_mode: str = "balanced") -> str:
    module = _PROVIDERS[config.provider]
    return module.call(
        config.api_key, config.model, system_prompt, user_prompt, max_tokens,
        response_mode=response_mode,
    )


def call_with_cache(config: LLMConfig, system_prompt: str, cacheable_context: str,
                    dynamic_content: str, max_tokens: int = 8192,
                    response_mode: str = "balanced") -> str:
    """Use when the same system_prompt + cacheable_context will be sent again
    on a subsequent call within the same job (e.g. one chunk call per section
    of a document, all sharing the same framework instructions + digest) -
    cuts real cost on the repeated portion. See each provider module for how
    caching is actually achieved (explicit cache_control for Anthropic,
    automatic server-side for OpenAI and Gemini)."""
    module = _PROVIDERS[config.provider]
    return module.call_with_cache(
        config.api_key, config.model, system_prompt, cacheable_context,
        dynamic_content, max_tokens, response_mode=response_mode,
    )


def validate_key(config: LLMConfig) -> tuple[bool, str]:
    module = _PROVIDERS[config.provider]
    return module.validate_key(config.api_key, config.model)
