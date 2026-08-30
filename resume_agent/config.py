from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    """可直接展示给本地用户的配置错误。"""


@dataclass(frozen=True)
class Settings:
    protocol: str
    api_key: str
    base_url: str | None
    model: str
    max_input_chars: int
    xelatex_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        protocol = os.getenv("LLM_PROTOCOL", "").strip().lower()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "").strip() or None
        if protocol not in {"openai", "anthropic"}:
            raise ConfigError("请在 .env 中设置 LLM_PROTOCOL=openai 或 anthropic。")
        if not api_key or not model:
            raise ConfigError("请在 .env 中填写 LLM_API_KEY 和 LLM_MODEL，密钥不会在页面显示。")
        max_chars = int(os.getenv("MAX_INPUT_CHARS", "180000"))
        return cls(
            protocol=protocol,
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_input_chars=max_chars,
            xelatex_path=find_xelatex(),
        )


def find_xelatex() -> str:
    configured = os.getenv("XELATEX_PATH", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(
        [
            shutil.which("xelatex") or "",
            r"D:\\Program Files (x86)\\LaTex\\MiKTeX\\miktex\\bin\\x64\\xelatex.exe",
            r"C:\\Program Files\\MiKTeX\\miktex\\bin\\x64\\xelatex.exe",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""

