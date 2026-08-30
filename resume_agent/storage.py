from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT


DATA_DIR = ROOT / "data"
FACTS_PATH = DATA_DIR / "confirmed_facts.json"
FEEDBACK_PATH = DATA_DIR / "feedback.json"


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_facts() -> list[dict[str, str]]:
    return _load(FACTS_PATH, [])


def save_confirmed_fact(question: str, answer: str, session_id: str) -> dict[str, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    facts = load_facts()
    item = {
        "id": f"F{len(facts) + 1}",
        "question": question,
        "value": answer.strip(),
        "source": "user_confirmation",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
    }
    facts.append(item)
    FACTS_PATH.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    return item


def ensure_feedback_placeholder() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not FEEDBACK_PATH.exists():
        FEEDBACK_PATH.write_text("[]\n", encoding="utf-8")

