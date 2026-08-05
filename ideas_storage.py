"""
Сховище ідей (продуктових і контентних) від ідея-агента.

Той самий патерн, що storage.py: JSON-файл, next_id, статуси —
тут "new" (щойно згенерована) / "saved" (людина натиснула кнопку
"Зберегти"). Окремий файл від tasks.json, бо це інша сутність.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DATA_FILE = Path(__file__).parent / "ideas.json"
_lock = Lock()


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"next_id": 1, "ideas": []}
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_idea(idea_type: str, text: str) -> int:
    """Додає нову ідею зі статусом "new", повертає її id.
    idea_type — "product" або "content"."""
    with _lock:
        data = _load()
        idea_id = data["next_id"]
        data["next_id"] += 1
        data["ideas"].append({
            "id": idea_id,
            "type": idea_type,
            "text": text,
            "status": "new",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        _save(data)
        return idea_id


def save_idea(idea_id: int) -> bool:
    """Позначає ідею збереженою. Повертає False, якщо id не знайдено."""
    with _lock:
        data = _load()
        for i in data["ideas"]:
            if i["id"] == idea_id:
                i["status"] = "saved"
                i["saved_at"] = datetime.now(timezone.utc).isoformat()
                _save(data)
                return True
        return False


def list_ideas(status: str | None = "saved") -> list[dict]:
    """Повертає ідеї за статусом ("new", "saved" або None = всі)."""
    data = _load()
    if status is None:
        return data["ideas"]
    return [i for i in data["ideas"] if i["status"] == status]
