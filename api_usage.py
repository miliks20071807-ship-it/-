"""
Лічильник викликів Claude API на добу — щоб автоматичні (не по прямій
команді людини) виклики: деплой-крон (щогодини), багфікс-агент
(тригериться issue), щотижнева розсилка ідей — самі зупинялись, коли
денний ліміт (ANTHROPIC_DAILY_CALL_LIMIT з .env/конфігу) вичерпано,
замість непомітного накопичення рахунку. Ручні команди людини
(/дизайн, /ідея_*, вільний чат) не гейтяться — лічильник тільки
рахує їх, обмеження застосовується вибірково в місцях виклику
(див. guard()/record_call() у orchestrator.py, design.py, ideas_agent.py,
pitch.py, agents/deploy_agent.py, agents/bugfix_agent.py).

Той самий JSON-файловий підхід, що storage.py/ideas_storage.py. Для
GitHub Actions-агентів (деплой/багфікс — окремий ефемерний runner на
кожен запуск) файл переживає між запусками через actions/cache у
відповідному workflow; сам модуль про це не знає, просто читає й
пише локальний файл.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DATA_FILE = Path(__file__).parent / "api_usage.json"
_lock = Lock()

DAILY_LIMIT = int(os.environ.get("ANTHROPIC_DAILY_CALL_LIMIT", "200"))


class AnthropicLimitExceeded(RuntimeError):
    """Денний ліміт викликів Claude API вичерпано."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"date": _today(), "count": 0, "warned": False}
    with DATA_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("date") != _today():
        return {"date": _today(), "count": 0, "warned": False}
    return data


def _save(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_call() -> int:
    """Реєструє один виклик Claude API за сьогодні, повертає лічильник."""
    with _lock:
        data = _load()
        data["count"] += 1
        _save(data)
        return data["count"]


def is_limit_exceeded() -> bool:
    return _load()["count"] >= DAILY_LIMIT


def should_warn_once() -> bool:
    """True рівно один раз на добу (перший пропущений автоматичний
    виклик після вичерпання ліміту) — щоб не заспамити той самий
    "ліміт вичерпано" щогодини/щотижня, поки ліміт не скинеться."""
    with _lock:
        data = _load()
        if data.get("warned"):
            return False
        data["warned"] = True
        _save(data)
        return True


def guard() -> None:
    """Кидає AnthropicLimitExceeded, якщо денний ліміт вичерпано —
    викликати перед кожним автоматичним (не по прямій команді людини)
    зверненням до Claude API."""
    if is_limit_exceeded():
        raise AnthropicLimitExceeded(f"Денний ліміт Claude API вичерпано ({DAILY_LIMIT} викликів)")
