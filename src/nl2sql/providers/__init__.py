"""SQL generation providers."""

from __future__ import annotations

from nl2sql.providers.base import (
    Generation,
    GenerationContext,
    Provider,
    ProviderError,
    Usage,
)

__all__ = [
    "PROVIDERS",
    "Generation",
    "GenerationContext",
    "Provider",
    "ProviderError",
    "Usage",
    "get_provider",
]

PROVIDERS = ("stub", "azure-openai")


def get_provider(name: str, **kwargs) -> Provider:
    name = name.lower()
    if name in ("stub", "template", "offline"):
        from nl2sql.providers.stub import TemplateProvider

        return TemplateProvider()
    if name in ("azure-openai", "azure", "aoai"):
        from nl2sql.providers.azure_openai import AzureOpenAIProvider

        return AzureOpenAIProvider.from_env(**kwargs)
    raise ProviderError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
