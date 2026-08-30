from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import ConfigError, Settings


T = TypeVar("T", bound=BaseModel)


class ModelServiceError(RuntimeError):
    """不含密钥或隐私内容的模型服务错误。"""


def _extract_json(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I | re.S)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ModelServiceError("模型未返回 JSON。请换用支持稳定文本输出的模型后重试。")
    return candidate[start : end + 1]


class LLMGateway:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()

    def structured(self, system: str, user: str, schema: type[T]) -> T:
        text = self._call(system, user)
        try:
            return schema.model_validate_json(_extract_json(text))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ModelServiceError(f"模型输出不符合预期结构：{exc}") from exc

    def _call(self, system: str, user: str) -> str:
        if self.settings.protocol == "openai":
            return self._call_openai(system, user)
        return self._call_anthropic(system, user)

    def _call_openai(self, system: str, user: str) -> str:
        try:
            from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

            client = OpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url)
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
            raise ModelServiceError("模型服务限流，请稍后重试。") from exc
        except APIConnectionError as exc:
            raise ModelServiceError("无法连接到 OpenAI 兼容 API；请检查 LLM_BASE_URL 和网络。") from exc
        except APIStatusError as exc:
            raise ModelServiceError(f"OpenAI 兼容 API 返回错误（HTTP {exc.status_code}）。请检查模型名和权限。") from exc
        except ModelServiceError:
            raise
        except Exception as exc:
            raise ModelServiceError(f"调用 OpenAI 兼容 API 失败：{exc.__class__.__name__}。") from exc

    def _call_anthropic(self, system: str, user: str) -> str:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.settings.api_key, base_url=self.settings.base_url)
            response = client.messages.create(
                model=self.settings.model,
                max_tokens=8000,
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
            raise ModelServiceError("Anthropic 兼容 API 限流，请稍后重试。") from exc
        except anthropic.APIStatusError as exc:
            raise ModelServiceError(f"Anthropic 兼容 API 返回错误（HTTP {exc.status_code}）。") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelServiceError("无法连接到 Anthropic 兼容 API；请检查 LLM_BASE_URL 和网络。") from exc
        except ModelServiceError:
            raise
        except Exception as exc:
            raise ModelServiceError(f"调用 Anthropic 兼容 API 失败：{exc.__class__.__name__}。") from exc


def require_configured_gateway() -> LLMGateway:
    try:
        return LLMGateway()
    except ConfigError as exc:
        raise ModelServiceError(str(exc)) from exc

