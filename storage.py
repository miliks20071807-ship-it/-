"""
Мінімальне сховище задач команди.

Навмисно на базі JSON-файлу, а не БД — для команди 2-5 людей цього
вистачає на старті. Коли/якщо стане тісно, весь модуль можна замінити
на SQLite чи Notion API, не міняючи bot.py.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DATA_FILE = Path(__file__).parent / "tasks.json"
_lock = Lock()


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"next_id": 1, "tasks": []}
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_task(text: str, author: str) -> int:
    """Додає нову задачу, повертає її id."""
    with _lock:
        data = _load()
        task_id = data["next_id"]
        data["next_id"] += 1
        data["tasks"].append({
            "id": task_id,
            "text": text,
            "author": author,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        _save(data)
        return task_id


def list_tasks(status: str = "open") -> list[dict]:
    """Повертає задачі за статусом ('open', 'done' або None = всі)."""
    data = _load()
    if status is None:
        return data["tasks"]
    return [t for t in data["tasks"] if t["status"] == status]


def complete_task(task_id: int) -> bool:
    """Позначає задачу виконаною. Повертає False, якщо id не знайдено."""
    with _lock:
        data = _load()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = "done"
                t["done_at"] = datetime.now(timezone.utc).isoformat()
                _save(data)
                return True
        return False
