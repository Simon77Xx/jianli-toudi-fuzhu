from __future__ import annotations

import json
import logging
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import ConfigError, Settings


logger = logging.getLogger("resume_agent.llm")
T = TypeVar("T", bound=BaseModel)


class ModelServiceError(RuntimeError):
    """不含密钥或隐私内容的模型服务错误。"""


class _RetryableModelError(ModelServiceError):
    """可短暂重试的瞬时模型服务错误（如限流、连接抖动或 5xx）。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", flags=re.I | re.S)
_INVALID_JSON_ESCAPE_RE = re.compile(r'(?<!\\)\\(?!(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))')


def _repair_invalid_json_escapes(raw: str) -> str:
    """Escape stray backslashes (common in LaTeX like \\item, \\%, \\_) inside JSON text."""
    return _INVALID_JSON_ESCAPE_RE.sub(r"\\\\", raw)


def _decode_first_json_object(candidate: str) -> str | None:
    decoder = json.JSONDecoder()
    pos = candidate.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(candidate, pos)
            if isinstance(obj, dict):
                return candidate[pos:end]
        except json.JSONDecodeError:
            pass
        pos = candidate.find("{", pos + 1)
    return None


def _extract_json(text: str) -> str:
    candidate = text.strip()
    if not candidate:
        raise ModelServiceError("模型未返回 JSON。请换用支持稳定文本输出的模型后重试。")

    # 1. Prefer fenced ```json ... ``` blocks when present.
    fenced_blocks = _FENCE_RE.findall(candidate)
    search_pool = [*fenced_blocks, candidate]
    for chunk in search_pool:
        stripped = chunk.strip()
        found = _decode_first_json_object(stripped)
        if found is not None:
            return found

    # 2. Retry decoding after repairing common unescaped LaTeX backslashes.
    for chunk in search_pool:
        repaired = _repair_invalid_json_escapes(chunk.strip())
        found = _decode_first_json_object(repaired)
        if found is not None:
            return found

    # 3. Last-resort slice between outermost braces (for downstream error reporting).
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I | re.S)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ModelServiceError("模型未返回 JSON。请换用支持稳定文本输出的模型后重试。")
    return _repair_invalid_json_escapes(cleaned[start : end + 1])


class LLMGateway:
    def __init__(self, settings: Settings | None = None, max_attempts: int = 3):
        self.settings = settings or Settings.from_env()
        self.max_attempts = max(1, max_attempts)

    def structured(self, system: str, user: str, schema: type[T]) -> T:
        prompt = user
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                text = self._call(system, prompt)
                extracted = _extract_json(text)
                try:
                    return schema.model_validate_json(extracted)
                except (ValidationError, json.JSONDecodeError):
                    repaired = _repair_invalid_json_escapes(extracted)
                    return schema.model_validate_json(repaired)
            except _RetryableModelError as exc:
                last_error = exc
                logger.warning("Transient LLM error on attempt %d/%d: %s", attempt, self.max_attempts, exc)
                if attempt < self.max_attempts:
                    time.sleep(min(2.0 * attempt, 5.0))
                    continue
                raise ModelServiceError(str(exc)) from exc
            except (ValidationError, json.JSONDecodeError, ModelServiceError) as exc:
                # Non-retryable client errors (e.g. 401/403/404) should fail immediately.
                if isinstance(exc, ModelServiceError) and "未返回 JSON" not in str(exc) and "空内容" not in str(exc):
                    raise
                last_error = exc
                logger.warning("Structured output parse failed on attempt %d/%d: %s", attempt, self.max_attempts, exc)
                if attempt < self.max_attempts:
                    prompt = (
                        f"{user}\n\n"
                        f"【格式修正提醒】你上一次的输出未能通过 JSON/结构校验（错误：{str(exc)[:300]}）。"
                        "请仅输出一个合法的 JSON 对象，不要包含额外说明；所有 LaTeX 反斜杠必须双写为 \\\\。"
                    )
                    continue
                if isinstance(exc, ModelServiceError):
                    raise
                raise ModelServiceError(f"模型输出不符合预期结构：{exc}") from exc
        raise ModelServiceError(f"模型调用失败：{last_error}")

    def _call(self, system: str, user: str) -> str:
        if self.settings.protocol == "openai":
            return self._call_openai(system, user)
        return self._call_anthropic(system, user)

    def _call_openai(self, system: str, user: str) -> str:
        try:
            from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

            client = OpenAI(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                timeout=self.settings.llm_timeout,
            )
            response = client.chat.completions.create(
                model=self.settings.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.1,
            )
            content = response.choices[0].message.content
            if not content:
                raise ModelServiceError("模型返回了空内容。")
            return content
        except RateLimitError as exc:
            raise _RetryableModelError("模型服务限流，请稍后重试。") from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise _RetryableModelError("无法连接到 OpenAI 兼容 API 或请求超时；请检查 LLM_BASE_URL 和网络。") from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise _RetryableModelError(f"OpenAI 兼容 API 临时错误（HTTP {exc.status_code}）。") from exc
            raise ModelServiceError(f"OpenAI 兼容 API 返回错误（HTTP {exc.status_code}）。请检查模型名和权限。") from exc
        except ModelServiceError:
            raise
        except Exception as exc:
            raise ModelServiceError(f"调用 OpenAI 兼容 API 失败：{exc.__class__.__name__}。") from exc

    def _call_anthropic(self, system: str, user: str) -> str:
        try:
            import anthropic

            client = anthropic.Anthropic(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                timeout=self.settings.llm_timeout,
            )
            response = client.messages.create(
                model=self.settings.model,
                max_tokens=8000,
                temperature=0.1,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            content = "\n".join(block.text for block in response.content if block.type == "text")
            if not content:
                raise ModelServiceError("模型返回了空内容。")
            return content
        except anthropic.NotFoundError as exc:
            raise ModelServiceError("Anthropic 兼容 API 未找到该模型或端点；请检查 LLM_MODEL / LLM_BASE_URL。") from exc
        except anthropic.RateLimitError as exc:
            raise _RetryableModelError("Anthropic 兼容 API 限流，请稍后重试。") from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise _RetryableModelError("无法连接到 Anthropic 兼容 API 或请求超时；请检查 LLM_BASE_URL 和网络。") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise _RetryableModelError(f"Anthropic 兼容 API 临时错误（HTTP {exc.status_code}）。") from exc
            raise ModelServiceError(f"Anthropic 兼容 API 返回错误（HTTP {exc.status_code}）。") from exc
        except ModelServiceError:
            raise
        except Exception as exc:
            raise ModelServiceError(f"调用 Anthropic 兼容 API 失败：{exc.__class__.__name__}。") from exc


def require_configured_gateway(settings: Settings | None = None) -> LLMGateway:
    try:
        return LLMGateway(settings=settings)
    except ConfigError as exc:
        raise ModelServiceError(str(exc)) from exc

