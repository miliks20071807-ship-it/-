"""
MVP Telegram-бот команди: /задача, /задачі, /done, і звичайний чат,
який оркестратор сам розпізнає як задачу чи просто повідомлення.

Запуск:
    pip install -r requirements.txt --break-system-packages
    cp .env.example .env   # і заповнити токени
    python bot.py
"""

import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, FSInputFile, Message
from dotenv import load_dotenv

import storage
from agents.common import create_github_issue, notify_telegram
from orchestrator import classify
from presentation import build_report

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("team-bot")

ERRORS_LOG = Path(__file__).parent / "errors.log"
HEARTBEAT_FILE = Path(__file__).parent / "heartbeat.txt"
PID_FILE = Path(__file__).parent / "bot.pid"
HEARTBEAT_INTERVAL_SECONDS = 60

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.error()
async def handle_error(event: ErrorEvent):
    """Джерело помилок для багфікс-агента (крок 3): будь-який
    необроблений виняток у хендлерах пишемо в errors.log і, якщо
    налаштовано GITHUB_TOKEN/GITHUB_REPOSITORY, відкриваємо GitHub
    issue з лейблом "bug" — це тригерить bugfix-agent.yml. Бот при
    цьому не падає і продовжує обробляти наступні повідомлення."""
    tb = "".join(traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__))
    log.error("Необроблений виняток:\n%s", tb)

    with ERRORS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n--- {datetime.now(timezone.utc).isoformat()} ---\n{tb}\n")

    issue_url = create_github_issue(
        title=f"Помилка в проді: {type(event.exception).__name__}",
        body=f"Автоматично зафіксовано ботом.\n\n```\n{tb[:6000]}\n```",
        labels=["bug"],
    )
    if issue_url:
        log.info("Відкрито issue для багфікс-агента: %s", issue_url)
    else:
        notify_telegram(f"⚠️ Помилка в боті (issue не відкрито — GITHUB_TOKEN не налаштовано):\n{type(event.exception).__name__}: {event.exception}")


def is_allowed(message: Message) -> bool:
    # Якщо список не заданий — бот відкритий для всіх (зручно на тесті,
    # але для проду обов'язково заповніть ALLOWED_USER_IDS в .env).
    if not ALLOWED_USER_IDS:
        return True
    return message.from_user.id in ALLOWED_USER_IDS


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я диспетчер команди.\n\n"
        "/задача <текст> — додати задачу\n"
        "/задачі — показати відкриті задачі\n"
        "/done <id> — позначити задачу виконаною\n"
        "/презентація [днів|all] — звіт по задачах команди (pptx)\n\n"
        "Або просто напиши повідомлення — я сам розберусь, задача це чи ні."
    )


@dp.message(Command("задача"))
async def cmd_add_task(message: Message):
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.answer("Напиши текст задачі після команди, наприклад:\n/задача полагодити авторизацію")

    task_id = storage.add_task(text, author=message.from_user.full_name)
    await message.answer(f"Задача #{task_id} додана: {text}")


@dp.message(Command("задачі"))
async def cmd_list_tasks(message: Message):
    tasks = storage.list_tasks(status="open")
    if not tasks:
        return await message.answer("Відкритих задач немає 🎉")

    lines = [f"#{t['id']} — {t['text']} (від {t['author']})" for t in tasks]
    await message.answer("Відкриті задачі:\n" + "\n".join(lines))


@dp.message(Command("done"))
async def cmd_done(message: Message):
    arg = message.text.partition(" ")[2].strip()
    if not arg.isdigit():
        return await message.answer("Вкажи id задачі, наприклад: /done 3")

    ok = storage.complete_task(int(arg))
    await message.answer(f"Задача #{arg} закрита ✅" if ok else f"Задачу #{arg} не знайдено")


@dp.message(Command("презентація"))
async def cmd_presentation(message: Message):
    """Крок 4: генерує pptx-звіт по задачах команди за період.
    /презентація — за останні 7 днів (дефолт), /презентація 30 — за 30 днів,
    /презентація all — за весь час."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    arg = message.text.partition(" ")[2].strip().lower()
    if arg == "all":
        days = None
    elif arg.isdigit():
        days = int(arg)
    else:
        days = 7

    await message.answer("Генерую презентацію...")
    path = build_report(days=days)
    await message.answer_document(FSInputFile(path), caption="Звіт команди готовий 📊")


@dp.message(F.text)
async def handle_free_text(message: Message):
    """Будь-яке інше повідомлення йде через оркестратор Claude,
    який сам вирішує — це задача, запит статусу, чи просто чат."""
    if not is_allowed(message):
        return

    result = classify(message.text)

    if result["intent"] == "add_task" and result["task_text"]:
        task_id = storage.add_task(result["task_text"], author=message.from_user.full_name)
        await message.answer(f"Додав як задачу #{task_id}: {result['task_text']}")

    elif result["intent"] == "list_tasks":
        await cmd_list_tasks(message)

    else:
        await message.answer(result["reply"] or "Ок 👍")


async def heartbeat_loop():
    """Крок 5: раз на HEARTBEAT_INTERVAL_SECONDS оновлює файл-мітку часу,
    за яким watchdog.py визначає, що бот живий (без цього — процес завис
    би непомітно для watchdog, навіть якщо сам процес технічно виконується)."""
    while True:
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def main():
    log.info("Бот запущено, чекаю повідомлень...")
    PID_FILE.write_text(str(os.getpid()))
    asyncio.create_task(heartbeat_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
