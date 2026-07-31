"""
Багфікс-агент.

Тригериться GitHub Actions на дві події (див. .github/workflows/bugfix-agent.yml
і .github/workflows/ci.yml):
  1. CI впав на push/PR -> ci.yml сам відкриває issue з лейблом "bug"
     і логами падіння.
  2. Помилка в проді (виняток у bot.py) -> bot.py створює issue з лейблом
     "bug" напряму через GitHub API (agents.common.create_github_issue).

Як і деплой-агент, працює як послідовність окремих кроків workflow
(не один монолітний прогін), щоб кожен крок міг одразу відзвітувати в
Telegram через curl (.github/scripts/telegram_notify.sh):

  propose <issue_number> — Claude читає issue + весь .py-код репо,
      пропонує фікс ОДНОГО файлу (повний новий вміст, не патч —
      простіше і надійніше застосувати без бібліотек diff/patch);
      виводить has_fix/file/explanation
  test — той самий крок, що й у деплой-агента (agents.common.run_tests)
  open-pr <issue_number> <file> <explanation> — коммітить, пушить,
      відкриває PR "Fixes #N"; виводить pr_number/pr_url

Багфікс-агент НІКОЛИ не мерджить сам (навіть тривіальні фікси) —
workflow завжди шле повідомлення з кнопками "Затвердити"/"Відхилити"
(рішення команди: автомердж лише в деплой-агента). Якщо тести після
фіксу червоні — workflow відкидає зміни (`git checkout -- .`) і лишає
issue відкритою для людини.

Локальний тест без реального GitHub-репозиторію:
    python agents/bugfix_agent.py propose <номер-issue>
(відкриє issue через gh, застосує фікс у робочу директорію, не пушить
і не створює PR — це вже наступні кроки).
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
from agents.common import gh, run, run_tests, set_output

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


def cmd_propose(args: argparse.Namespace) -> int:
    issue = json.loads(gh("issue", "view", str(args.issue_number), "--json", "title,body,url").stdout)
    fix = propose_fix(issue["title"], issue.get("body") or "")

    set_output("issue_url", issue["url"])
    set_output("explanation", fix["explanation"])

    if not fix["file"]:
        set_output("has_fix", "false")
        return 0

    target = REPO_ROOT / fix["file"]
    target.write_text(fix["new_content"], encoding="utf-8")
    set_output("has_fix", "true")
    set_output("file", fix["file"])
    return 0


def cmd_test(_args: argparse.Namespace) -> int:
    set_output("passed", "true" if run_tests() else "false")
    return 0


def cmd_open_pr(args: argparse.Namespace) -> int:
    branch = f"bugfix-agent/issue-{args.issue_number}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    run(["git", "checkout", "-b", branch])
    run(["git", "add", args.file])
    run(["git", "commit", "-m", f"bugfix-agent: fix for #{args.issue_number}"])
    run(["git", "push", "origin", branch])

    pr = gh(
        "pr", "create",
        "--title", f"bugfix-agent: fix #{args.issue_number}",
        "--body", f"Fixes #{args.issue_number}\n\n{args.explanation}",
        "--base", "main",
        "--head", branch,
    )
    pr_url = pr.stdout.strip()
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]

    set_output("pr_number", pr_number)
    set_output("pr_url", pr_url)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose")
    p_propose.add_argument("issue_number")

    sub.add_parser("test")

    p_open = sub.add_parser("open-pr")
    p_open.add_argument("issue_number")
    p_open.add_argument("file")
    p_open.add_argument("explanation")

    args = parser.parse_args()
    handlers = {
        "propose": cmd_propose,
        "test": cmd_test,
        "open-pr": cmd_open_pr,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
