"""
Стендап-агент: щоденне зведення по задачах команди — що закрито за
останню добу, скільки лишилось відкритих — плюс запрошення відповісти
на 3 стандартні стендап-питання. Дані ті самі, що й у /задачі
(tasks.json), нового сховища не додано.

Спрацьовує або по розкладу (фоновий таск у bot.py, час/дні —
STANDUP_TIME/STANDUP_DAYS в .env), або вручну командою /стендап.

Крос-агентне зведення: разом із задачами показує ще й відкриті PR від
деплой/багфікс-агентів (GitHub API) — щоб команда бачила все, що
потребує уваги, в одному повідомленні, а не бігала по різних ботах.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import storage
from agents.common import list_open_agent_prs


def build_standup_message(hours: int = 24) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    all_tasks = storage.list_tasks(status=None)

    done_recent = [
        t for t in all_tasks
        if t["status"] == "done" and t.get("done_at") and datetime.fromisoformat(t["done_at"]) >= since
    ]
    open_tasks = [t for t in all_tasks if t["status"] == "open"]

    lines = ["☀️ Стендап команди", ""]

    if done_recent:
        lines.append(f"✅ Закрито за останні {hours} год:")
        lines += [f"  • {t['text']} ({t['author']})" for t in done_recent]
    else:
        lines.append(f"✅ За останні {hours} год нічого не закрито.")

    lines.append("")
    lines.append(f"📋 Відкритих задач: {len(open_tasks)}")

    tagged = [t for t in open_tasks if t.get("agent")]
    if tagged:
        lines.append(f"🔗 З них для агентів: {len(tagged)}")

    open_prs = list_open_agent_prs()
    lines.append("")
    if open_prs:
        pr_lines = "\n".join(f"  • #{pr['number']} {pr['title']}" for pr in open_prs)
        lines.append(f"🔍 PR, що чекають підтвердження: {len(open_prs)}\n{pr_lines}")
    else:
        lines.append("🔍 Відкритих PR немає.")

    lines.append("")
    lines.append(
        "Напишіть у відповідь:\n"
        "1️⃣ Що робили вчора\n"
        "2️⃣ Що плануєте сьогодні\n"
        "3️⃣ Чи є блокери"
    )
    return "\n".join(lines)
