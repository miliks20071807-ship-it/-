"""
Деплой-агент.

Запускається по крону (GitHub Actions, раз на годину — див.
.github/workflows/deploy-agent.yml) як послідовність окремих кроків
workflow (не один монолітний прогін) — так кожен крок може одразу
відзвітувати в Telegram через curl (.github/scripts/telegram_notify.sh),
навіть якщо наступний крок впаде.

Підкоманди (кожна — окремий крок у deploy-agent.yml):
  autofix    — ruff --fix, виводить has_changes=true|false
  test       — pytest, виводить passed=true|false
  open-pr    — Claude оцінює diff на "тривіальність", коммітить,
               пушить, відкриває PR; виводить pr_number/pr_url/trivial/reason
  merge-pr <pr_number> — squash-мердж (лише для тривіальних PR); виводить merged=true|false

Автомердж робиться ЛИШЕ якщо Claude визнав зміну тривіальною І тести
зелені — це єдиний агент системи з таким правом (рішення команди). В
усіх інших випадках workflow сам (через telegram_notify.sh approve)
шле повідомлення з кнопками "Затвердити"/"Відхилити"; натискання
обробляє bot.py через GitHub API (agents.common.merge_pr/close_pr).

Ніколи не пушить і не мерджить напряму в main поза цим PR-флоу.

Локальний тест без реального GitHub-репозиторію:
    python agents/deploy_agent.py autofix
    python agents/deploy_agent.py test
(open-pr/merge-pr вимагають git remote + gh auth, тому для локальної
перевірки логіки достатньо перших двох кроків.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from anthropic import Anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.common import extract_text, gh, run, run_tests, set_output

MODEL = "claude-sonnet-5"

ASSESS_SYSTEM_PROMPT = """Ти оцінюєш diff у невеликому Python-проєкті команди 2-5 людей.
Відповідай ЛИШЕ JSON: {"trivial": true|false, "reason": "коротко чому"}.

"trivial": true — лише якщо diff складається виключно з форматування,
лінт-фіксів, оновлення версій залежностей у requirements.txt чи інших
механічних, легко зворотних змін, які НЕ чіпають бізнес-логіку.
Будь-яка зміна поведінки коду, логіки агентів, обробки грошей/даних
користувачів чи конфігурації доступу — "trivial": false.
"""


def assess_diff(diff_text: str) -> dict:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=ASSESS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": diff_text[:8000]}],
    )
    raw = extract_text(response).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"trivial": False, "reason": "не вдалось розпарсити відповідь моделі"}
    result.setdefault("trivial", False)
    result.setdefault("reason", "")
    return result


def has_pending_changes() -> bool:
    result = run(["git", "status", "--porcelain"], check=False)
    return bool(result.stdout.strip())


def cmd_autofix(_args: argparse.Namespace) -> int:
    run(["ruff", "check", "--fix", "."], check=False)
    set_output("has_changes", "true" if has_pending_changes() else "false")
    return 0


def cmd_test(_args: argparse.Namespace) -> int:
    set_output("passed", "true" if run_tests() else "false")
    return 0


def cmd_open_pr(_args: argparse.Namespace) -> int:
    diff_text = run(["git", "diff"], check=False).stdout
    assessment = assess_diff(diff_text)

    branch = f"deploy-agent/auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    run(["git", "checkout", "-b", branch])
    run(["git", "commit", "-am", "deploy-agent: автофікс лінту/форматування"])
    run(["git", "push", "origin", branch])

    pr = gh(
        "pr", "create",
        "--title", "deploy-agent: автоматичний фікс",
        "--body", f"Автоматично згенеровано деплой-агентом.\n\nОцінка Claude: trivial={assessment['trivial']}\n{assessment['reason']}\n\n```diff\n{diff_text[:3000]}\n```",
        "--base", "main",
        "--head", branch,
    )
    pr_url = pr.stdout.strip()
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]

    set_output("pr_number", pr_number)
    set_output("pr_url", pr_url)
    set_output("trivial", "true" if assessment["trivial"] else "false")
    set_output("reason", assessment["reason"])
    return 0


def cmd_merge_pr(args: argparse.Namespace) -> int:
    merge = gh("pr", "merge", args.pr_number, "--squash", "--delete-branch", check=False)
    set_output("merged", "true" if merge.returncode == 0 else "false")
    set_output("merge_error", merge.stderr.strip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("autofix")
    sub.add_parser("test")
    sub.add_parser("open-pr")

    p_merge = sub.add_parser("merge-pr")
    p_merge.add_argument("pr_number")

    args = parser.parse_args()
    handlers = {
        "autofix": cmd_autofix,
        "test": cmd_test,
        "open-pr": cmd_open_pr,
        "merge-pr": cmd_merge_pr,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
