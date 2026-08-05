"""
Спільні дрібниці для крон-агентів (деплой, багфікс), які виконуються
як окремі GitHub Actions джоби, а не всередині bot.py, і для bot.py
самого (йому також потрібні merge_pr/close_pr/status-запити).

Навмисно без нових важких залежностей (без PyGithub/httpx): Telegram і
GitHub REST — через stdlib urllib, робота з git/PR всередині Actions —
через `gh` CLI, який вже є на GitHub-хостованих раннерах і автоматично
автентифікований через GITHUB_TOKEN.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request


def extract_text(response) -> str:
    """Повертає текст першого текстового блоку відповіді Claude.

    Деякі моделі (напр. claude-sonnet-5) можуть повертати ThinkingBlock
    першим елементом content — content[0].text напряму на них падає з
    AttributeError. Шукаємо перший блок з type == "text" замість
    жорсткого припущення про індекс."""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("У відповіді Claude немає текстового блоку")


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALERT_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")


def run(cmd: list[str], check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Запускає команду, повертає CompletedProcess зі stdout/stderr як текст."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=cwd)


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Виклик `gh` CLI (GitHub Actions runner має його вбудованим і
    автентифікованим через env GH_TOKEN/GITHUB_TOKEN)."""
    return run(["gh", *args], check=check)


def run_tests() -> bool:
    """Спільний прогін тестів для деплой- і багфікс-агента."""
    result = run(["pytest", "-q"], check=False)
    print(result.stdout)
    print(result.stderr)
    return result.returncode == 0


def set_output(name: str, value: str) -> None:
    """Пише значення у $GITHUB_OUTPUT, щоб наступний крок workflow міг
    його прочитати (steps.<id>.outputs.<name>) — саме так деплой/багфікс-
    скрипти передають результат кроку в YAML, який потім сам вирішує,
    яке повідомлення в Telegram слати. Поза Actions (локальний запуск)
    просто друкує в консоль."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"[output] {name}={value}")
        return
    delimiter = "GHADELIM"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def notify_telegram(text: str, chat_id: str | None = None, bot_token: str | None = None) -> None:
    """Шле повідомлення в Telegram напряму через Bot API HTTP-запит.
    Використовується watchdog.py (і як внутрішній fallback у bot.py) —
    основний потік прогресу деплой/багфікс-агентів тепер іде через
    curl прямо з GitHub Actions (.github/scripts/telegram_notify.sh),
    щоб не залежати від того, чи доживе python-скрипт до кінця.

    bot_token — щоб надіслати від імені іншого бота (напр. окремого
    багфікс-бота), а не дефолтного TELEGRAM_BOT_TOKEN."""
    target = chat_id or TELEGRAM_ALERT_CHAT_ID
    token = bot_token or TELEGRAM_BOT_TOKEN
    if not token or not target:
        print(f"[telegram alert skipped, no token/chat_id configured]: {text}")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
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


def _github_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list | None]:
    """Мінімальний GitHub REST-клієнт на urllib. path — це хвіст після
    /repos/{GITHUB_REPOSITORY}, напр. "/issues" чи "/pulls/12/merge".
    Повертає (http_status, json_or_none); http_status=0 означає, що
    GITHUB_TOKEN/GITHUB_REPOSITORY не налаштовані."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return 0, None

    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, None
    except urllib.error.URLError as e:
        print(f"[github api request failed]: {e}")
        return 0, None


def create_github_issue(title: str, body: str, labels: list[str] | None = None) -> str | None:
    """Створює GitHub issue. Повертає URL або None, якщо не вдалось чи
    GITHUB_TOKEN/GITHUB_REPOSITORY не налаштовані (тоді помилка лишається
    в локальному errors.log — див. bot.py)."""
    status, data = _github_request("POST", "/issues", {"title": title, "body": body, "labels": labels or []})
    if status and 200 <= status < 300 and data:
        return data.get("html_url")
    return None


def merge_pr(pr_number: int, method: str = "squash") -> tuple[bool, str]:
    """Мердж PR через GitHub API. Викликається з bot.py у відповідь на
    натискання кнопки "✅ Затвердити і замерджити" — це єдиний шлях,
    яким людина підтверджує деплой/багфікс, не заходячи на GitHub."""
    status, data = _github_request("PUT", f"/pulls/{pr_number}/merge", {"merge_method": method})
    if status == 0:
        return False, "GITHUB_TOKEN/GITHUB_REPOSITORY не налаштовані на боті"
    if 200 <= status < 300:
        return True, "merged"
    return False, (data or {}).get("message", f"HTTP {status}")


def close_pr(pr_number: int) -> tuple[bool, str]:
    """Закриває (відхиляє) PR без мерджу — кнопка "❌ Відхилити"."""
    status, data = _github_request("PATCH", f"/pulls/{pr_number}", {"state": "closed"})
    if status == 0:
        return False, "GITHUB_TOKEN/GITHUB_REPOSITORY не налаштовані на боті"
    if 200 <= status < 300:
        return True, "closed"
    return False, (data or {}).get("message", f"HTTP {status}")


def get_last_workflow_run(workflow_file: str) -> dict | None:
    """Останній запуск GitHub Actions workflow (за іменем файлу, напр.
    "deploy-agent.yml") — для команди /статус_агентів."""
    status, data = _github_request("GET", f"/actions/workflows/{workflow_file}/runs?per_page=1")
    if status == 200 and data and data.get("workflow_runs"):
        run_info = data["workflow_runs"][0]
        return {
            "created_at": run_info["created_at"],
            "status": run_info["status"],
            "conclusion": run_info["conclusion"],
            "html_url": run_info["html_url"],
        }
    return None


def trigger_workflow_dispatch(workflow_file: str, ref: str = "main") -> tuple[bool, str]:
    """Запускає GitHub Actions workflow позачергово (workflow_dispatch),
    не чекаючи cron — використовується тегом @деплой у /задача, щоб
    людина могла попросити деплой-агента перевірити репозиторій прямо
    зараз, а не аж у наступну годину."""
    status, data = _github_request("POST", f"/actions/workflows/{workflow_file}/dispatches", {"ref": ref})
    if status == 0:
        return False, "GITHUB_TOKEN/GITHUB_REPOSITORY не налаштовані на боті"
    if status == 204:
        return True, "запущено"
    return False, (data or {}).get("message", f"HTTP {status}")


def list_open_agent_prs() -> list[dict]:
    """Відкриті PR, створені деплой- чи багфікс-агентом (за префіксом
    гілки) — саме ці чекають на ручне підтвердження в Telegram."""
    status, data = _github_request("GET", "/pulls?state=open&per_page=50")
    if status != 200 or not data:
        return []
    return [
        {"number": pr["number"], "title": pr["title"], "url": pr["html_url"]}
        for pr in data
        if pr.get("head", {}).get("ref", "").startswith(("deploy-agent/", "bugfix-agent/"))
    ]
