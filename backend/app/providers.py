from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any, Protocol

import httpx

from .contracts import ProviderResponse, ToolInvocation
from .prompts import ROLE_PROMPTS


class ProviderError(RuntimeError):
    """Sanitized provider error suitable for run status and SSE."""


class ModelProvider(Protocol):
    async def complete(
        self,
        *,
        role: str,
        case_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse: ...


def _estimated_usage(value: dict[str, Any], input_data: dict[str, Any]) -> dict[str, int]:
    input_chars = len(json.dumps(input_data, ensure_ascii=False, sort_keys=True))
    output_chars = len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return {
        "input_tokens": max(1, input_chars // 4),
        "output_tokens": max(1, output_chars // 4),
    }


class FixtureReplayProvider:
    """Deterministic structured-output replay for demos and offline tests."""

    def __init__(
        self,
        responses: dict[str, Any],
        *,
        delay_seconds: float = 0.0,
        model_name: str = "fixture-replay-v1",
        max_tokens: int = 2048,
    ) -> None:
        self._responses = deepcopy(responses)
        self._delay_seconds = delay_seconds
        self._model_name = model_name
        self._max_tokens = max_tokens
        self._counters: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    async def complete(
        self,
        *,
        role: str,
        case_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse:
        del schema
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)

        if role == "evidence":
            step = input_data["step"]
            value = {"tool": step["tool"], "arguments": step["arguments"]}
            ToolInvocation.model_validate(value)
        else:
            case_responses = self._responses.get("cases", {}).get(case_id, {})
            configured = case_responses.get(role)
            if configured is None:
                raise ProviderError(f"fixture_missing:{case_id}:{role}")
            async with self._lock:
                key = (case_id, role)
                index = self._counters.get(key, 0)
                self._counters[key] = index + 1
            if isinstance(configured, list):
                value = configured[min(index, len(configured) - 1)]
            else:
                value = configured
            value = deepcopy(value)
            value = _resolve_fixture_placeholders(value, input_data)

        return ProviderResponse(
            data=value,
            usage=_estimated_usage(value, input_data),
            model=self._model_name,
            inference_config={
                "temperature": 0,
                "enable_thinking": False,
                "max_tokens": self._max_tokens,
                "requested_model": self._model_name,
            },
        )


class DashScopeProvider:
    """OpenAI-compatible DashScope adapter with strict structured output."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        fallback_model: str = "qwen3.7-plus",
    ) -> None:
        if not api_key:
            raise ProviderError("dashscope_credentials_missing")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._fallback_model = fallback_model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self.total_provider_attempts = 0

    async def complete(
        self,
        *,
        role: str,
        case_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse:
        del case_id
        if role not in ROLE_PROMPTS:
            raise ProviderError("unsupported_provider_role")
        last_error: ProviderError | None = None
        for index, model in enumerate((self._model, self._fallback_model)):
            try:
                self.total_provider_attempts += 1
                response = await self._request(
                    role=role,
                    model=model,
                    input_data=input_data,
                    schema=schema,
                )
                return response.model_copy(
                    update={
                        "inference_config": {
                            **response.inference_config,
                            "provider_attempts": index + 1,
                        }
                    }
                )
            except _ModelUnavailableError as exc:
                last_error = ProviderError("dashscope_model_unavailable")
                if index == 0 and self._fallback_model != self._model:
                    continue
                raise last_error from exc
        raise last_error or ProviderError("dashscope_request_failed")

    async def _request(
        self,
        *,
        role: str,
        model: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": ROLE_PROMPTS[role]},
                {
                    "role": "user",
                    "content": json.dumps(input_data, ensure_ascii=False, sort_keys=True),
                },
            ],
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "enable_thinking": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"emi_{role}_output",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError("dashscope_transport_failure") from exc

        if response.status_code in {400, 404} and _looks_like_model_unavailable(response):
            raise _ModelUnavailableError
        if response.status_code in {401, 403}:
            raise ProviderError("dashscope_authentication_failure")
        if response.status_code >= 400:
            raise ProviderError(f"dashscope_http_{response.status_code}")
        try:
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
            data = json.loads(content)
            usage = envelope.get("usage", {})
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("dashscope_invalid_structured_response") from exc
        return ProviderResponse(
            data=data,
            usage={
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
            },
            model=model,
            inference_config={
                "temperature": 0,
                "enable_thinking": False,
                "max_tokens": self._max_tokens,
                "requested_model": self._model,
            },
        )


class _ModelUnavailableError(RuntimeError):
    pass


def _resolve_fixture_placeholders(value: Any, input_data: dict[str, Any]) -> Any:
    evidence_by_step = {
        item.get("step_id"): item.get("operation_id")
        for item in input_data.get("evidence", [])
        if item.get("step_id") and item.get("operation_id")
    }
    if isinstance(value, str) and value.startswith("{{evidence:") and value.endswith("}}"):
        step_id = value[len("{{evidence:") : -2]
        return evidence_by_step.get(step_id, value)
    if isinstance(value, list):
        return [_resolve_fixture_placeholders(item, input_data) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_fixture_placeholders(item, input_data) for key, item in value.items()
        }
    return value


def _looks_like_model_unavailable(response: httpx.Response) -> bool:
    try:
        body = json.dumps(response.json(), ensure_ascii=False).lower()
    except ValueError:
        body = response.text[:500].lower()
    return "model" in body and any(
        marker in body for marker in ("not found", "not exist", "unsupported", "invalid")
    )
