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
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
    trigger_workflow_dispatch,
)
from design import generate_mockup
from ideas_agent import generate_content_ideas, generate_product_ideas
from ideas_storage import add_idea, list_ideas, save_idea
from orchestrator import classify
from presentation import build_report
from standup import build_standup_message

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
DESIGNER_BOT_TOKEN = os.environ.get("TELEGRAM_DESIGNER_BOT_TOKEN", "")
STANDUP_BOT_TOKEN = os.environ.get("TELEGRAM_STANDUP_BOT_TOKEN", "")
IDEAS_BOT_TOKEN = os.environ.get("TELEGRAM_IDEAS_BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}
TELEGRAM_ALERT_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")

# Час і дні щоденного авто-стендапу (локальний час хоста бота — без
# нової залежності типу zoneinfo/pytz, для команди в одному часовому
# поясі цього достатньо). Формат STANDUP_TIME — "HH:MM" (24-год).
STANDUP_TIME = os.environ.get("STANDUP_TIME", "09:30")
STANDUP_DAYS = {
    d.strip().lower() for d in os.environ.get("STANDUP_DAYS", "mon,tue,wed,thu,fri").split(",") if d.strip()
}
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# День/час щотижневої розсилки ідей — тут, на відміну від стендапу,
# використовуємо zoneinfo (стандартна бібліотека з Python 3.9, без
# нової залежності): користувач явно попросив Europe/Kyiv, а не просто
# "локальний час хоста".
IDEAS_DAY = os.environ.get("IDEAS_DAY", "mon").strip().lower()
IDEAS_TIME = os.environ.get("IDEAS_TIME", "10:00")
IDEAS_TIMEZONE = ZoneInfo(os.environ.get("IDEAS_TIMEZONE", "Europe/Kyiv"))

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

# Дизайн-агент — не крон-агент, а команда за запитом (/дизайн), тому
# на відміну від деплой/багфікс тут немає notify_telegram-фолбеку:
# якщо DESIGNER_BOT_TOKEN не задано, команду /дизайн просто обробляє
# основний бот (див. реєстрацію хендлера нижче).
designer_bot = Bot(token=DESIGNER_BOT_TOKEN) if DESIGNER_BOT_TOKEN else None
designer_dp = Dispatcher() if DESIGNER_BOT_TOKEN else None

# Стендап-агент — теж не крон-агент у GitHub Actions, а фоновий таск
# всередині bot.py (standup_loop нижче), бо зведення читає tasks.json,
# який живе лише на хості бота, а не в git-репозиторії.
standup_bot = Bot(token=STANDUP_BOT_TOKEN) if STANDUP_BOT_TOKEN else None
standup_dp = Dispatcher() if STANDUP_BOT_TOKEN else None

# Ідея-агент — команди за запитом (/ідея_продукт, /ідея_контент,
# /ідеї_збережені) + щотижневий фоновий таск (ideas_loop нижче).
ideas_bot = Bot(token=IDEAS_BOT_TOKEN) if IDEAS_BOT_TOKEN else None
ideas_dp = Dispatcher() if IDEAS_BOT_TOKEN else None


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


async def process_idea_callback(callback: CallbackQuery) -> None:
    """Кнопка "💾 Зберегти" під згенерованою ідеєю. Ідея вже лежить у
    ideas.json зі статусом "new" (записана одразу при генерації) —
    тут лише переводимо в "saved"."""
    if not is_allowed_user(callback.from_user.id):
        return await callback.answer("Немає доступу.", show_alert=True)

    _, idea_id = callback.data.split(":", 1)
    ok = save_idea(int(idea_id))

    original_text = callback.message.text or ""
    outcome = "💾 Збережено" if ok else "⚠️ Не вдалось зберегти (ідею не знайдено)"
    await callback.message.edit_text(f"{original_text}\n\n— {outcome}")
    await callback.answer(outcome)


@dp.callback_query(F.data.startswith("idea:"))
async def handle_idea_callback(callback: CallbackQuery):
    await process_idea_callback(callback)


if ideas_dp is not None:
    @ideas_dp.callback_query(F.data.startswith("idea:"))
    async def handle_ideas_bot_callback(callback: CallbackQuery):
        await process_idea_callback(callback)


async def send_idea_with_button(chat_id: int | str, idea_type: str, text: str, target_bot: Bot) -> None:
    """Зберігає ідею (статус "new") і шле окремим повідомленням з
    кнопкою "💾 Зберегти" (callback_data "idea:<id>")."""
    idea_id = add_idea(idea_type, text)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Зберегти", callback_data=f"idea:{idea_id}")]
    ])
    label = "💡 Продуктова ідея" if idea_type == "product" else "🎬 Контентна ідея"
    await target_bot.send_message(
        chat_id=chat_id,
        text=f"{label}\n\n{text}",
        reply_markup=keyboard,
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я диспетчер команди.\n\n"
        "/задача <текст> — додати задачу\n"
        "/задачі — показати відкриті задачі\n"
        "/done <id> — позначити задачу виконаною\n"
        "/презентація [днів|all] — звіт по задачах команди (pptx)\n"
        "/дизайн <опис> — HTML-мокап екрана продукту\n"
        "/стендап — зведення по задачах + запрошення на стендап (і щодня автоматично)\n"
        "/ідея_продукт — 5 продуктових ідей\n"
        "/ідея_контент — 5 ідей для Reels/TikTok\n"
        "/ідеї_збережені — список збережених ідей (і щотижня автоматично 3+3)\n"
        "/статус_агентів — стан деплой-агента, відкритих PR і watchdog\n"
        "/допомога — повний список команд по кожному боту окремо\n\n"
        "Або просто напиши повідомлення — я сам розберусь, задача це чи ні."
    )


@dp.message(Command("допомога"))
async def cmd_help(message: Message):
    """Повний список команд по кожному боту — теги @дизайн/@багфікс/@деплой
    у /задача підмінюють необхідність писати в кожен бот окремо, тож тут
    же й пояснюємо цей ярлик."""
    await message.answer(
        "📋 Усі команди системи, по ботах:\n\n"
        "🗂 Диспетчер (цей бот):\n"
        "/задача <текст> — додати задачу (теги @дизайн/@багфікс/@деплой "
        "на початку тексту одразу викликають агента)\n"
        "/задачі — відкриті задачі\n"
        "/done <id> — позначити задачу виконаною\n"
        "/презентація [днів|all] — pptx-звіт по задачах команди\n"
        "/статус_агентів — стан усіх агентів, відкриті PR, watchdog\n"
        "вільний текст без команди — Claude сам визначить, задача це чи ні\n\n"
        "🐛 Багфікс-бот — своїх команд немає, лише сповіщення про знайдені "
        "баги й пропозиції фіксу з кнопками Затвердити/Відхилити\n\n"
        "🚀 Деплой-бот — своїх команд немає, лише сповіщення про прогрес "
        "деплою й PR з кнопками підтвердження (тривіальні фікси мерджаться "
        "автоматично)\n\n"
        "🎨 Дизайн-бот:\n"
        "/дизайн <опис екрана> — HTML-мокап екрана продукту файлом\n\n"
        "📅 Стендап-бот:\n"
        "/стендап — зведення по задачах і відкритих PR "
        "(і щодня автоматично о " + STANDUP_TIME + " у " + ",".join(sorted(STANDUP_DAYS)) + ")\n\n"
        "💡 Ідея-бот:\n"
        "/ідея_продукт — 5 продуктових ідей\n"
        "/ідея_контент — 5 ідей для Reels/TikTok\n"
        "/ідеї_збережені — список збережених ідей "
        f"(і щотижня автоматично {IDEAS_DAY} {IDEAS_TIME}, {IDEAS_TIMEZONE.key})\n\n"
        "Команди дизайн/стендап/ідея-бота треба набирати саме в тому чаті, "
        "що представляє цього агента — крім /задача @тег, який якраз і "
        "замінює необхідність писати в кожен бот окремо."
    )


AGENT_TAGS = ("@дизайн", "@багфікс", "@деплой")


async def maybe_dispatch_tagged_task(text: str, message: Message) -> tuple[str, str | None]:
    """Якщо текст задачі починається з тега агента (@дизайн/@багфікс/
    @деплой) — одразу запускає відповідного агента з рештою тексту як
    описом. Повертає (текст_без_тега, ім'я_агента_або_None) —
    ім'я_агента піде в storage.add_task(agent=...), щоб /статус_агентів
    і /стендап могли показати крос-агентне зведення. Відповідь шле від
    імені бота відповідного агента, а не диспетчера — щоб результат
    виглядав "від нього", навіть якщо задачу додали в чаті з диспетчером."""
    lowered = text.lower()
    for tag in AGENT_TAGS:
        if not lowered.startswith(tag):
            continue

        rest = text[len(tag):].strip()
        if not rest:
            return text, None

        if tag == "@дизайн":
            await message.answer("Побачив тег @дизайн — одразу малюю мокап...")
            path = generate_mockup(rest)
            target = designer_bot or message.bot
            await target.send_document(
                chat_id=message.chat.id,
                document=FSInputFile(path),
                caption="Мокап готовий 🎨 (за задачею з тегом @дизайн)",
            )
            return rest, "design"

        if tag == "@багфікс":
            issue_url = create_github_issue(
                title=f"[з задачі] {rest[:80]}",
                body=f"Створено автоматично з тегованої задачі команди.\n\n{rest}",
                labels=["bug"],
            )
            target = bugfix_bot or message.bot
            if issue_url:
                await target.send_message(
                    chat_id=message.chat.id,
                    text=f"🐞 Побачив тег @багфікс — відкрив issue: {issue_url}\nБагфікс-агент підхопить автоматично.",
                )
            else:
                await target.send_message(
                    chat_id=message.chat.id,
                    text="⚠️ Побачив тег @багфікс, але не вдалось відкрити issue (перевір GITHUB_TOKEN/GITHUB_REPOSITORY на боті).",
                )
            return rest, "bugfix"

        if tag == "@деплой":
            ok, detail = trigger_workflow_dispatch(DEPLOY_WORKFLOW_FILE)
            target = deploy_bot or message.bot
            if ok:
                await target.send_message(
                    chat_id=message.chat.id,
                    text="🚀 Побачив тег @деплой — запустив позачергову перевірку репозиторію (не чекаючи щогодинного розкладу).",
                )
            else:
                await target.send_message(
                    chat_id=message.chat.id,
                    text=f"⚠️ Побачив тег @деплой, але не вдалось запустити: {detail}",
                )
            return rest, "deploy"

    return text, None


@dp.message(Command("задача"))
async def cmd_add_task(message: Message):
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.answer(
            "Напиши текст задачі після команди, наприклад:\n/задача полагодити авторизацію\n\n"
            "Теги на початку одразу викликають агента:\n"
            "@дизайн — HTML-мокап, @багфікс — GitHub issue, @деплой — позачергова перевірка репо"
        )

    text, agent = await maybe_dispatch_tagged_task(text, message)
    task_id = storage.add_task(text, author=message.from_user.full_name, agent=agent)
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


async def cmd_design(message: Message):
    """Дизайн-агент: /дизайн <опис екрана/фічі> генерує HTML-мокап
    через Claude і шле файл у чат (відкривається в браузері). Claude —
    текстова модель, не малює картинки, тому результат — робочий
    HTML/CSS, а не зображення."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    description = message.text.partition(" ")[2].strip()
    if not description:
        return await message.answer(
            "Опиши, що намалювати, наприклад:\n/дизайн екран календаря на день з подіями та кнопкою додати подію"
        )

    await message.answer("Малюю мокап...")
    path = generate_mockup(description)
    await message.answer_document(FSInputFile(path), caption="Мокап готовий 🎨 Відкрий файл у браузері")


(designer_dp or dp).message(Command("дизайн"))(cmd_design)


async def cmd_standup(message: Message):
    """Стендап-агент: /стендап — миттєве зведення (те саме, що й
    щоденний авто-стендап за розкладом, лише вручну й одразу)."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")
    await message.answer(build_standup_message())


(standup_dp or dp).message(Command("стендап"))(cmd_standup)


async def cmd_idea_product(message: Message):
    """Ідея-агент: /ідея_продукт — 5 продуктових ідей (нові фічі,
    UX-покращення, механіки утримання), кожна окремим повідомленням з
    кнопкою "Зберегти"."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    await message.answer("Генерую продуктові ідеї...")
    target = ideas_bot or message.bot
    for text in generate_product_ideas(5):
        await send_idea_with_button(message.chat.id, "product", text, target)


(ideas_dp or dp).message(Command("ідея_продукт"))(cmd_idea_product)


async def cmd_idea_content(message: Message):
    """Ідея-агент: /ідея_контент — 5 ідей коротких відео (Reels/TikTok),
    кожна окремим повідомленням з кнопкою "Зберегти"."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    await message.answer("Генерую контентні ідеї...")
    target = ideas_bot or message.bot
    for text in generate_content_ideas(5):
        await send_idea_with_button(message.chat.id, "content", text, target)


(ideas_dp or dp).message(Command("ідея_контент"))(cmd_idea_content)


async def cmd_ideas_saved(message: Message):
    """Ідея-агент: /ідеї_збережені — весь список ідей зі статусом saved."""
    if not is_allowed(message):
        return await message.answer("Немає доступу.")

    saved = list_ideas(status="saved")
    if not saved:
        return await message.answer("Збережених ідей поки немає.")

    lines = [f"#{i['id']} [{i['type']}] {i['text']}" for i in saved]
    await message.answer("Збережені ідеї:\n" + "\n\n".join(lines))


(ideas_dp or dp).message(Command("ідеї_збережені"))(cmd_ideas_saved)


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
    lines.append("🎨 Дизайн-бот: увімкнено" if designer_bot is not None else "🎨 Дизайн-бот: /дизайн обробляє основний бот (TELEGRAM_DESIGNER_BOT_TOKEN не задано)")
    lines.append(
        f"📅 Стендап-бот: увімкнено, {STANDUP_TIME} у {','.join(sorted(STANDUP_DAYS))}"
        if standup_bot is not None
        else "📅 Стендап-бот: /стендап обробляє основний бот (TELEGRAM_STANDUP_BOT_TOKEN не задано)"
    )
    lines.append(
        f"💡 Ідея-бот: увімкнено, {IDEAS_DAY} {IDEAS_TIME} ({IDEAS_TIMEZONE.key})"
        if ideas_bot is not None
        else "💡 Ідея-бот: команди обробляє основний бот (TELEGRAM_IDEAS_BOT_TOKEN не задано)"
    )

    watchdog_age = _file_age_seconds(WATCHDOG_HEARTBEAT_FILE)
    if watchdog_age is None:
        lines.append("🐕 Watchdog: даних немає (ще не запускався на цьому хості)")
    elif watchdog_age < WATCHDOG_STALE_AFTER_SECONDS:
        lines.append(f"🐕 Watchdog: живий (остання перевірка {int(watchdog_age)}с тому)")
    else:
        lines.append(f"🐕 Watchdog: НЕ запускався {int(watchdog_age)}с — перевір системний cron")

    # Крос-агентне зведення: відкриті задачі, додані через теги
    # (@дизайн/@багфікс/@деплой) — видно, яка задача пішла якому агенту.
    tagged = [t for t in storage.list_tasks(status="open") if t.get("agent")]
    if tagged:
        tagged_lines = "\n".join(f"  • #{t['id']} [{t['agent']}] {t['text']}" for t in tagged)
        lines.append(f"🔗 Тегованих задач для агентів (відкриті): {len(tagged)}\n{tagged_lines}")

    await message.answer("\n".join(lines))


@dp.message(F.text)
async def handle_free_text(message: Message):
    """Будь-яке інше повідомлення йде через оркестратор Claude,
    який сам вирішує — це задача, запит статусу, чи просто чат."""
    if not is_allowed(message):
        return

    # Теги агентів перевіряємо на сирому тексті, ДО класифікації —
    # orchestrator.classify() переформульовує task_text "своїми
    # словами", тому тег міг би загубитись при перефразуванні.
    stripped = message.text.strip()
    if stripped.lower().startswith(AGENT_TAGS):
        text, agent = await maybe_dispatch_tagged_task(stripped, message)
        task_id = storage.add_task(text, author=message.from_user.full_name, agent=agent)
        return await message.answer(f"Додав як задачу #{task_id}: {text}")

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


async def standup_loop():
    """Раз на хвилину перевіряє, чи не настав час щоденного стендапу
    (STANDUP_TIME/STANDUP_DAYS, локальний час хоста) — і якщо так,
    шле зведення в TELEGRAM_ALERT_CHAT_ID через standup_bot (чи
    основного бота, якщо STANDUP_BOT_TOKEN не задано). last_sent
    захищає від повторної відправки в ту саму хвилину/дату."""
    last_sent_date = None
    target_bot = standup_bot or bot

    while True:
        now = datetime.now()  # noqa: DTZ005 — навмисно локальний час хоста, не UTC
        today_key = _WEEKDAY_KEYS[now.weekday()]

        if (
            today_key in STANDUP_DAYS
            and now.strftime("%H:%M") == STANDUP_TIME
            and last_sent_date != now.date()
        ):
            if TELEGRAM_ALERT_CHAT_ID:
                await target_bot.send_message(chat_id=TELEGRAM_ALERT_CHAT_ID, text=build_standup_message())
                last_sent_date = now.date()
            else:
                log.warning("Час стендапу настав, але TELEGRAM_ALERT_CHAT_ID не задано — нікуди слати.")

        await asyncio.sleep(30)


async def ideas_loop():
    """Раз на хвилину перевіряє, чи не настав час щотижневої розсилки
    ідей (IDEAS_DAY/IDEAS_TIME, IDEAS_TIMEZONE — за замовчуванням
    Europe/Kyiv) — і якщо так, шле 3 продуктові + 3 контентні ідеї в
    TELEGRAM_ALERT_CHAT_ID без запиту, кожну окремим повідомленням з
    кнопкою "Зберегти". last_sent захищає від повторної відправки в
    той самий тиждень."""
    last_sent_week = None
    target = ideas_bot or bot

    while True:
        now = datetime.now(IDEAS_TIMEZONE)
        today_key = _WEEKDAY_KEYS[now.weekday()]
        this_week = now.isocalendar()[:2]  # (рік, номер тижня)

        if today_key == IDEAS_DAY and now.strftime("%H:%M") == IDEAS_TIME and last_sent_week != this_week:
            if TELEGRAM_ALERT_CHAT_ID:
                await target.send_message(
                    chat_id=TELEGRAM_ALERT_CHAT_ID,
                    text="💡 Щотижнева пачка ідей від ідея-агента:",
                )
                for text in generate_product_ideas(3):
                    await send_idea_with_button(TELEGRAM_ALERT_CHAT_ID, "product", text, target)
                for text in generate_content_ideas(3):
                    await send_idea_with_button(TELEGRAM_ALERT_CHAT_ID, "content", text, target)
                last_sent_week = this_week
            else:
                log.warning("Час розсилки ідей настав, але TELEGRAM_ALERT_CHAT_ID не задано — нікуди слати.")

        await asyncio.sleep(30)


async def main():
    log.info("Бот запущено, чекаю повідомлень...")
    PID_FILE.write_text(str(os.getpid()))

    tasks = [heartbeat_loop(), standup_loop(), ideas_loop(), dp.start_polling(bot)]
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

    if designer_bot is not None:
        log.info("Дизайн-бот теж запущено (TELEGRAM_DESIGNER_BOT_TOKEN заданий).")
        tasks.append(designer_dp.start_polling(designer_bot))
    else:
        log.info("TELEGRAM_DESIGNER_BOT_TOKEN не задано — /дизайн обробляє основний бот.")

    if standup_bot is not None:
        log.info("Стендап-бот теж запущено (TELEGRAM_STANDUP_BOT_TOKEN заданий).")
        tasks.append(standup_dp.start_polling(standup_bot))
    else:
        log.info("TELEGRAM_STANDUP_BOT_TOKEN не задано — /стендап обробляє основний бот.")

    if ideas_bot is not None:
        log.info("Ідея-бот теж запущено (TELEGRAM_IDEAS_BOT_TOKEN заданий).")
        tasks.append(ideas_dp.start_polling(ideas_bot))
    else:
        log.info("TELEGRAM_IDEAS_BOT_TOKEN не задано — команди ідея-агента обробляє основний бот.")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
