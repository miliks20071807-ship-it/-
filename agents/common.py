"""
Спільні дрібниці для крон-агентів (деплой, багфікс), які виконуються
як окремі GitHub Actions джоби, а не всередині bot.py.

Навмисно без aiogram/anthropic-специфічних імпортів понад необхідне і
без нових важких залежностей: Telegram-алерти йдуть напряму через
Bot API (urllib, stdlib), а робота з GitHub — через `gh` CLI, який вже
є на GitHub-хостованих раннерах і автоматично автентифікований через
GITHUB_TOKEN.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALERT_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")


def run(cmd: list[str], check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Запускає команду, повертає CompletedProcess зі stdout/stderr як текст."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=cwd)


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Виклик `gh` CLI (GitHub Actions runner має його вбудованим і
    автентифікованим через env GH_TOKEN/GITHUB_TOKEN)."""
    return run(["gh", *args], check=check)


def notify_telegram(text: str, chat_id: str | None = None) -> None:
    """Шле повідомлення в Telegram напряму через Bot API HTTP-запит,
    без залежності від aiogram — агенти це окремі короткоживучі процеси,
    а не частина event loop бота."""
    target = chat_id or TELEGRAM_ALERT_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        print(f"[telegram alert skipped, no token/chat_id configured]: {text}")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"[telegram alert failed]: {e}")


def create_github_issue(title: str, body: str, labels: list[str] | None = None) -> str | None:
    """Створює GitHub issue через REST API напряму (urllib), без залежності
    від `gh` CLI — цим користується bot.py, який може працювати на хості
    без встановленого gh. Повертає URL issue або None, якщо не вдалось
    (наприклад, GITHUB_TOKEN/GITHUB_REPOSITORY не налаштовані на проді —
    тоді помилка просто лишається в локальному errors.log)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return None

    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps({"title": title, "body": body, "labels": labels or []}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("html_url")
    except urllib.error.URLError as e:
        print(f"[github issue creation failed]: {e}")
        return None
