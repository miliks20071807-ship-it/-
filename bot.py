"""
MVP Telegram-бот команди: /задача, /задачі, /done, і звичайний чат,
який оркестратор сам розпізнає як задачу чи просто повідомлення.

Запуск:
    pip install -r requirements.txt --break-system-packages
    cp .env.example .env   # і заповнити токени
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, FSInputFile, Message
from dotenv import load_dotenv

# Мусить виконатись до імпорту наших модулів (orchestrator.py читає
# ANTHROPIC_API_KEY одразу при імпорті, на рівні модуля) — інакше
# .env ще не встигає завантажитись і process падає з KeyError.
load_dotenv()

import storage
from agents.common import (
    close_pr,
    create_github_issue,
    get_last_workflow_run,
    list_open_agent_prs,
    merge_pr,
    notify_telegram,
)
from orchestrator import classify
from presentation import build_report

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("team-bot")

ERRORS_LOG = Path(__file__).parent / "errors.log"
HEARTBEAT_FILE = Path(__file__).parent / "heartbeat.txt"
PID_FILE = Path(__file__).parent / "bot.pid"
WATCHDOG_HEARTBEAT_FILE = Path(__file__).parent / "watchdog_heartbeat.txt"
HEARTBEAT_INTERVAL_SECONDS = 60
WATCHDOG_STALE_AFTER_SECONDS = 900  # запас у 3x типовий cron-інтервал watchdog (5 хв)

DEPLOY_WORKFLOW_FILE = "deploy-agent.yml"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BUGFIX_BOT_TOKEN = os.environ.get("TELEGRAM_BUGFIX_BOT_TOKEN", "")
DEPLOY_BOT_TOKEN = os.environ.get("TELEGRAM_DEPLOY_BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Окремі боти під конкретних агентів (своя ідентичність у Telegram —
# команда бачить сповіщення від "багфіксера" чи "деплоєра", а не всі
# змішані під одним диспетчером). Технічно це додаткові Bot+Dispatcher
# в тому ж процесі (не окремі скрипти) — так watchdog і далі стежить
# лише за одним процесом через один heartbeat.txt/bot.pid. Якщо
# відповідний TELEGRAM_*_BOT_TOKEN не заданий — сповіщення того агента
# просто йдуть через основного бота (graceful degradation).
bugfix_bot = Bot(token=BUGFIX_BOT_TOKEN) if BUGFIX_BOT_TOKEN else None
bugfix_dp = Dispatcher() if BUGFIX_BOT_TOKEN else None

deploy_bot = Bot(token=DEPLOY_BOT_TOKEN) if DEPLOY_BOT_TOKEN else None
deploy_dp = Dispatcher() if DEPLOY_BOT_TOKEN else None


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
        notify_telegram(
            f"⚠️ Помилка в боті (issue не відкрито — GITHUB_TOKEN не налаштовано):\n{type(event.exception).__name__}: {event.exception}",
            bot_token=BUGFIX_BOT_TOKEN or None,
        )


def is_allowed_user(user_id: int) -> bool:
    # Якщо список не заданий — бот відкритий для всіх (зручно на тесті,
    # але для проду обов'язково заповніть ALLOWED_USER_IDS в .env).
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def is_allowed(message: Message) -> bool:
    return is_allowed_user(message.from_user.id)


async def process_pr_callback(callback: CallbackQuery) -> None:
    """Спільна логіка кнопок "Затвердити"/"Відхилити" — зареєстрована
    на обох ботах (основному і багфікс-боті), щоб PR від будь-якого
    агента можна було підтвердити незалежно від того, який саме бот
    надіслав повідомлення з кнопками. merge_pr/close_pr працюють через
    GitHub REST API — боту для цього потрібен GITHUB_TOKEN з правом на
    merge PR (див. env.example)."""
    if not is_allowed_user(callback.from_user.id):
        return await callback.answer("Немає доступу.", show_alert=True)

    _, action, pr_number = callback.data.split(":", 2)

    if action == "approve":
        ok, detail = merge_pr(int(pr_number))
        outcome = f"✅ PR #{pr_number} замерджено" if ok else f"⚠️ Не вдалось замерджити PR #{pr_number}: {detail}"
    else:
        ok, detail = close_pr(int(pr_number))
        outcome = f"❌ PR #{pr_number} відхилено" if ok else f"⚠️ Не вдалось закрити PR #{pr_number}: {detail}"

    original_text = callback.message.text or ""
    await callback.message.edit_text(f"{original_text}\n\n— {outcome} ({callback.from_user.full_name})")
    await callback.answer(outcome)


@dp.callback_query(F.data.startswith("pr:"))
async def handle_pr_callback(callback: CallbackQuery):
    await process_pr_callback(callback)


if bugfix_dp is not None:
    @bugfix_dp.callback_query(F.data.startswith("pr:"))
    async def handle_bugfix_pr_callback(callback: CallbackQuery):
        await process_pr_callback(callback)

if deploy_dp is not None:
    @deploy_dp.callback_query(F.data.startswith("pr:"))
    async def handle_deploy_pr_callback(callback: CallbackQuery):
        await process_pr_callback(callback)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я диспетчер команди.\n\n"
        "/задача <текст> — додати задачу\n"
        "/задачі — показати відкриті задачі\n"
        "/done <id> — позначити задачу виконаною\n"
        "/презентація [днів|all] — звіт по задачах команди (pptx)\n"
        "/статус_агентів — стан деплой-агента, відкритих PR і watchdog\n\n"
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


def _file_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        ts = datetime.fromisoformat(path.read_text().strip())
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


@dp.message(Command("статус_агентів"))
async def cmd_agents_status(message: Message):
    """Крок з доповнення архітектури: короткий звіт по всій системі
    агентів прямо в Telegram, щоб не заходити на GitHub навіть подивитись."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    lines = ["📊 Статус агентів:"]

    last_run = get_last_workflow_run(DEPLOY_WORKFLOW_FILE)
    if last_run:
        lines.append(
            f"🚀 Деплой-агент: останній запуск {last_run['created_at']} — "
            f"{last_run['conclusion'] or last_run['status']}"
        )
    else:
        lines.append("🚀 Деплой-агент: даних немає (перевір GITHUB_TOKEN/GITHUB_REPOSITORY на боті)")

    open_prs = list_open_agent_prs()
    if open_prs:
        pr_lines = "\n".join(f"  • #{pr['number']} {pr['title']}" for pr in open_prs)
        lines.append(f"🔍 Відкритих PR на підтвердження: {len(open_prs)}\n{pr_lines}")
    else:
        lines.append("🔍 Відкритих PR на підтвердження: 0")

    lines.append("🐛 Багфікс-бот: увімкнено" if bugfix_bot is not None else "🐛 Багфікс-бот: вимкнено (TELEGRAM_BUGFIX_BOT_TOKEN не задано)")
    lines.append("🤖 Деплой-бот: увімкнено" if deploy_bot is not None else "🤖 Деплой-бот: вимкнено (TELEGRAM_DEPLOY_BOT_TOKEN не задано)")

    watchdog_age = _file_age_seconds(WATCHDOG_HEARTBEAT_FILE)
    if watchdog_age is None:
        lines.append("🐕 Watchdog: даних немає (ще не запускався на цьому хості)")
    elif watchdog_age < WATCHDOG_STALE_AFTER_SECONDS:
        lines.append(f"🐕 Watchdog: живий (остання перевірка {int(watchdog_age)}с тому)")
    else:
        lines.append(f"🐕 Watchdog: НЕ запускався {int(watchdog_age)}с — перевір системний cron")

    await message.answer("\n".join(lines))


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

    tasks = [heartbeat_loop(), dp.start_polling(bot)]
    if bugfix_bot is not None:
        log.info("Багфікс-бот теж запущено (TELEGRAM_BUGFIX_BOT_TOKEN заданий).")
        tasks.append(bugfix_dp.start_polling(bugfix_bot))
    else:
        log.info("TELEGRAM_BUGFIX_BOT_TOKEN не задано — кнопки багфікс-агента обробляє основний бот.")

    if deploy_bot is not None:
        log.info("Деплой-бот теж запущено (TELEGRAM_DEPLOY_BOT_TOKEN заданий).")
        tasks.append(deploy_dp.start_polling(deploy_bot))
    else:
        log.info("TELEGRAM_DEPLOY_BOT_TOKEN не задано — кнопки деплой-агента обробляє основний бот.")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
