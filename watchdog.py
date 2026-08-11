"""
Health-check / watchdog (крок 5).

Важливо: це НЕ агент. Не має доступу до Claude API, не редагує код,
не відкриває PR і не мерджить — свідоме архітектурне рішення, щоб
процес з правом "перезапускати все" не мав одночасно права
"самомодифікуватись". Лише читає heartbeat.txt/bot.pid, перезапускає
bot.py, якщо той завис чи впав, і шле алерт у Telegram.

Деплой- і багфікс-агенти сюди не входять: вони не довгоживучі процеси,
а завдання GitHub Actions за розкладом/подією — самі GitHub Actions
показують провал джоби (і, за потреби, надсилають окреме сповіщення
з самого job'а через notify_telegram у agents/common.py).

Запуск (рекомендовано — через системний cron, раз на 5 хв):
    */5 * * * * cd /path/to/repo && /path/to/venv/bin/python watchdog.py

Або як фоновий процес, що сам спить між перевірками:
    python watchdog.py --loop --interval 300
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from agents.common import notify_telegram

REPO_ROOT = Path(__file__).parent
HEARTBEAT_FILE = REPO_ROOT / "heartbeat.txt"
PID_FILE = REPO_ROOT / "bot.pid"
WATCHDOG_HEARTBEAT_FILE = REPO_ROOT / "watchdog_heartbeat.txt"
STALE_AFTER_SECONDS = 180  # запас у 3x heartbeat-інтервал бота (60с)


def _read_heartbeat_age_seconds() -> float | None:
    if not HEARTBEAT_FILE.exists():
        return None
    try:
        ts = datetime.fromisoformat(HEARTBEAT_FILE.read_text().strip())
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _bot_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return False
    return _process_alive(pid)


def restart_bot() -> None:
    log_file = (REPO_ROOT / "bot.out.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "bot.py")],
        cwd=REPO_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))


def check_once() -> None:
    # Пишеться на кожному запуску, незалежно від результату — саме
    # за цим файлом /статус_агентів у bot.py визначає, чи системний
    # cron взагалі викликає watchdog (а не лише чи живий бот).
    WATCHDOG_HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())

    age = _read_heartbeat_age_seconds()
    alive = _bot_running()

    if alive and age is not None and age < STALE_AFTER_SECONDS:
        return

    if not alive:
        reason = "процес бота не знайдено (bot.pid відсутній або процес мертвий)"
    elif age is None:
        reason = "heartbeat.txt відсутній або пошкоджений"
    else:
        reason = f"heartbeat не оновлювався {int(age)}с (bot.py, схоже, завис)"

    notify_telegram(f"🚨 Watchdog: бот не відповідає ({reason}). Перезапускаю...")
    try:
        restart_bot()
    except OSError as e:
        notify_telegram(f"🔥 Watchdog: не вдалось перезапустити бота: {e}")
        return
    notify_telegram("✅ Watchdog: команду на перезапуск бота надіслано.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="не виходити, перевіряти в циклі")
    parser.add_argument("--interval", type=int, default=300, help="секунд між перевірками в режимі --loop")
    args = parser.parse_args()

    if not args.loop:
        check_once()
        return 0

    while True:
        try:
            check_once()
        except Exception:  # noqa: BLE001 — навмисно широко: --loop не має падати через жодну помилку однієї перевірки
            # У --loop-режимі це довгоживучий процес — необроблений
            # виняток тут означає, що watchdog більше нікого не
            # перевіряє, поки хтось не помітить і не перезапустить його
            # вручну. Пропускаємо цю ітерацію, а не валимо весь процес.
            print("watchdog: check_once() впав, пропускаю цю ітерацію", file=sys.stderr)
            traceback.print_exc()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
