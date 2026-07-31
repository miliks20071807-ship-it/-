"""
Оркестратор: приймає довільне повідомлення з Telegram і вирішує,
що з ним робити — створити задачу, показати список, чи просто
відповісти в чаті.

Це MVP-версія одного "агента-диспетчера". Коли додаватимете
деплой-агента чи агента презентацій, кожен з них — окрема функція
за тим самим принципом: чіткий system-промпт + JSON-відповідь,
яку код парсить і виконує (а не інтерпретує вільний текст).
"""

import json
import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Швидка/дешева модель для класифікації наміру.
# Для складніших агентів (деплой, багфікси) варто узяти claude-sonnet-5.
MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """Ти — диспетчер команди у Telegram-боті.
Отримуєш повідомлення від учасника команди і визначаєш намір.

Відповідай ЛИШЕ JSON-об'єктом, без жодного тексту навколо, у форматі:
{"intent": "add_task" | "list_tasks" | "chat", "task_text": string | null, "reply": string}

Правила:
- "add_task" — якщо людина описує задачу, яку треба зробити.
  task_text — стисле формулювання задачі своїми словами.
- "list_tasks" — якщо просять показати поточні задачі/статус.
- "chat" — усе інше. У полі reply дай коротку доречну відповідь
  українською, 1-2 речення.
"""


def classify(message_text: str) -> dict:
    """Повертає dict з ключами intent, task_text, reply."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message_text}],
    )
    raw = response.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Якщо модель раптом відповіла не JSON-ом — не валимо бота,
        # а деградуємо до звичайного чату.
        return {"intent": "chat", "task_text": None, "reply": raw}

    result.setdefault("intent", "chat")
    result.setdefault("task_text", None)
    result.setdefault("reply", "")
    return result
