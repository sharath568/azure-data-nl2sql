"""Azure OpenAI chat completions provider (default deployment: gpt-4.1-mini).

Plain REST over httpx, structured output (JSON schema), temperature 0,
retries on 429 and 5xx honouring ``Retry-After``.

Configuration (environment or constructor):

    AZURE_OPENAI_ENDPOINT          https://<name>.openai.azure.com
    AZURE_OPENAI_API_KEY           key, or use AZURE_OPENAI_AD_TOKEN (Entra ID bearer)
    AZURE_OPENAI_CHAT_DEPLOYMENT   deployment name (default gpt-4.1-mini)
    AZURE_OPENAI_API_VERSION       default 2024-10-21
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable

import httpx

from nl2sql.providers.base import (
    Generation,
    GenerationContext,
    Provider,
    ProviderError,
    Usage,
)
from nl2sql.providers.prompt import RESPONSE_SCHEMA, build_messages

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class AzureOpenAIProvider(Provider):
    name = "azure-openai"

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        ad_token: str | None = None,
        deployment: str = "gpt-4.1-mini",
        api_version: str = "2024-10-21",
        model: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        max_tokens: int = 800,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not endpoint:
            raise ProviderError("AZURE_OPENAI_ENDPOINT is not set")
        if not api_key and not ad_token:
            raise ProviderError("set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN")
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_version = api_version
        self.model = model or deployment
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self._sleep = sleep
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {ad_token}"
        self.client = client or httpx.Client(timeout=timeout_s)
        self.headers = headers

    @classmethod
    def from_env(cls, **overrides) -> AzureOpenAIProvider:
        kwargs = {
            "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
            "api_key": os.environ.get("AZURE_OPENAI_API_KEY") or None,
            "ad_token": os.environ.get("AZURE_OPENAI_AD_TOKEN") or None,
            "deployment": os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4.1-mini"),
            "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            "model": os.environ.get("AZURE_OPENAI_CHAT_MODEL") or None,
        }
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    @property
    def url(self) -> str:
        return (
            f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions"
            f"?api-version={self.api_version}"
        )

    def _post(self, body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.post(self.url, headers=self.headers, json=body)
            except httpx.TransportError as e:
                last = e
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in RETRY_STATUS:
                    raise ProviderError(
                        f"Azure OpenAI returned {resp.status_code}: {resp.text[:300]}"
                    )
                last = ProviderError(f"Azure OpenAI returned {resp.status_code}")
                retry_after = resp.headers.get("retry-after")
                if attempt < self.max_retries:
                    try:
                        delay = float(retry_after) if retry_after else 2.0**attempt
                    except ValueError:
                        delay = 2.0**attempt
                    self._sleep(min(delay, 30.0))
                    continue
            if attempt < self.max_retries:
                self._sleep(min(2.0**attempt, 30.0))
        raise ProviderError(f"Azure OpenAI request failed after retries: {last}")

    def generate(self, ctx: GenerationContext) -> Generation:
        body = {
            "messages": build_messages(ctx),
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "nl2sql_answer", "strict": True, "schema": RESPONSE_SCHEMA},
            },
        }
        start = time.perf_counter()
        data = self._post(body)
        latency = (time.perf_counter() - start) * 1000
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from e
        model = data.get("model") or self.model
        if message.get("refusal"):
            return Generation(
                None, f"model refused: {message['refusal']}", [], self.name, model, usage, latency
            )
        content = message.get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return Generation(
                None,
                "model did not return valid JSON",
                [],
                self.name,
                model,
                usage,
                latency,
                content,
            )
        sql = str(parsed.get("sql") or "").strip()
        if parsed.get("answerable") is False:
            sql = ""
        return Generation(
            sql=sql or None,
            explanation=str(parsed.get("explanation", "")),
            tables_used=[str(t) for t in parsed.get("tables_used", []) or []],
            provider=self.name,
            model=model,
            usage=usage,
            latency_ms=latency,
            raw=content,
        )
