"""
Багфікс-агент.

Тригериться GitHub Actions на дві події (див. .github/workflows/bugfix-agent.yml
і .github/workflows/ci.yml):
  1. CI впав на push/PR -> ci.yml сам відкриває issue з лейблом "bug"
     і логами падіння.
  2. Помилка в проді (виняток у bot.py) -> bot.py створює issue з лейблом
     "bug" напряму через GitHub API (agents.common.create_github_issue).

У обох випадках відкриття issue з лейблом "bug" тригерить bugfix-agent.yml,
який запускає цей скрипт з номером issue.

Логіка:
  1. Читає issue (заголовок, тіло — там стектрейс/опис).
  2. Дає Claude весь .py-код репозиторію + текст issue, просить
     запропонувати виправлення ОДНОГО файлу (повний новий вміст файлу,
     не патч — простіше і надійніше застосувати без бібліотек diff/patch).
  3. Застосовує, ганяє тести.
     - Тести червоні -> відкидає зміни, коментує в issue, що не зміг
       безпечно виправити автоматично, і сповіщає Telegram — issue
       лишається відкритою для людини.
     - Тести зелені -> відкриває PR з посиланням "Fixes #N".
  4. ЗАВЖДИ (незалежно від тривіальності) лишає PR на ручне
     підтвердження в Telegram — багфікс-агент не має права автомерджити
     (рішення команди: автомердж лише в деплой-агента).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.common import gh, notify_telegram, run

MODEL = "claude-sonnet-5"
REPO_ROOT = Path(__file__).parent.parent

FIX_SYSTEM_PROMPT = """Ти — інженер, який виправляє баг у невеликому Python-проєкті
(Telegram-бот команди на aiogram + Anthropic SDK).

Тобі дають опис проблеми (issue з логами/стектрейсом) і повний вміст
усіх .py-файлів репозиторію. Запропонуй виправлення РІВНО ОДНОГО файлу.

Відповідай ЛИШЕ JSON:
{"file": "відносний/шлях.py" | null, "new_content": "повний новий вміст файлу" | null, "explanation": "коротко що і чому"}

Якщо не можеш впевнено визначити причину й безпечний фікс — постав
"file": null і поясни, чого бракує (наприклад, треба більше логів).
Не вигадуй зміни поведінки, які не випливають прямо з опису помилки.
"""


def collect_source_files() -> dict:
    files = {}
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if any(part in {".venv", "venv", "__pycache__"} for part in rel.parts):
            continue
        files[str(rel)] = path.read_text(encoding="utf-8")
    return files


def propose_fix(issue_title: str, issue_body: str) -> dict:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    files = collect_source_files()
    files_blob = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in files.items())

    user_content = f"Issue: {issue_title}\n\n{issue_body}\n\n---\nФайли репозиторію:\n\n{files_blob}"

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=FIX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content[:100_000]}],
    )
    raw = response.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"file": None, "new_content": None, "explanation": "не вдалось розпарсити відповідь моделі"}
    result.setdefault("file", None)
    result.setdefault("new_content", None)
    result.setdefault("explanation", "")
    return result


def run_tests() -> bool:
    result = run(["pytest", "-q"], check=False)
    print(result.stdout)
    print(result.stderr)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_number", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    issue = json.loads(gh("issue", "view", str(args.issue_number), "--json", "title,body,url").stdout)
    fix = propose_fix(issue["title"], issue.get("body") or "")

    if not fix["file"]:
        msg = f"🐞 Багфікс-агент не зміг впевнено виправити issue #{args.issue_number} автоматично: {fix['explanation']}\n{issue['url']}"
        notify_telegram(msg)
        if not args.dry_run:
            gh("issue", "comment", str(args.issue_number), "--body", fix["explanation"] or "Не вдалось автоматично визначити фікс.")
        return 0

    target = REPO_ROOT / fix["file"]
    original = target.read_text(encoding="utf-8") if target.exists() else None

    if args.dry_run:
        print(f"[dry-run] змінив би {fix['file']}: {fix['explanation']}")
        return 0

    target.write_text(fix["new_content"], encoding="utf-8")

    if not run_tests():
        print("Багфікс-агент: тести червоні після фіксу — відкидаю зміни.")
        if original is not None:
            target.write_text(original, encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        notify_telegram(
            f"🐞 Багфікс-агент спробував виправити issue #{args.issue_number}, "
            f"але тести не пройшли — зміни відкинуто, потрібне ручне втручання.\n{issue['url']}"
        )
        gh("issue", "comment", str(args.issue_number),
           "--body", f"Автоматична спроба фіксу не пройшла тести:\n\n{fix['explanation']}")
        return 1

    branch = f"bugfix-agent/issue-{args.issue_number}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    run(["git", "checkout", "-b", branch])
    run(["git", "add", fix["file"]])
    run(["git", "commit", "-m", f"bugfix-agent: fix for #{args.issue_number}"])
    run(["git", "push", "origin", branch])

    pr = gh(
        "pr", "create",
        "--title", f"bugfix-agent: fix #{args.issue_number}",
        "--body", f"Fixes #{args.issue_number}\n\n{fix['explanation']}",
        "--base", "main",
        "--head", branch,
    )
    pr_url = pr.stdout.strip()

    notify_telegram(
        f"🐞 Багфікс-агент відкрив PR для issue #{args.issue_number} — "
        f"потрібне ручне підтвердження (багфікси автомердж не роблять):\n{pr_url}\n\n{fix['explanation']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
