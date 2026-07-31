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

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

import storage
from orchestrator import classify

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("team-bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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
        "/done <id> — позначити задачу виконаною\n\n"
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


async def main():
    log.info("Бот запущено, чекаю повідомлень...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
