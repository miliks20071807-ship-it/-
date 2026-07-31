"""
Деплой-агент.

Запускається по крону (GitHub Actions, раз на годину — див.
.github/workflows/deploy-agent.yml). Логіка:

  1. Пробує авто-виправити дрібні проблеми (ruff --fix).
  2. Якщо після фіксу є diff — ганяє тести.
     - Тести червоні -> відкидає зміни, нічого не мерджить і не пушить,
       лише лог (за живі помилки відповідає багфікс-агент, не цей).
     - Тести зелені -> відкриває PR.
  3. Просить Claude оцінити, чи PR "тривіальний і некритичний"
     (форматування, лінт, залежності і т.п.) — і ЛИШЕ якщо так І тести
     зелені, автомерджить. Це єдиний агент у системі, якому дозволено
     автомердж (рішення команди). У решті випадків PR лишається
     відкритим і йде повідомлення в Telegram на ручне підтвердження.

Ніколи не пушить і не мерджить напряму в main поза цим PR-флоу.

Тестування без реального GitHub-репозиторію:
    python agents/deploy_agent.py --dry-run
(виконає кроки 1-2, але замість git push / gh pr create лише
надрукує, що б сталось).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from anthropic import Anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.common import gh, notify_telegram, run

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
    raw = response.content[0].text.strip()
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


def run_autofix() -> None:
    run(["ruff", "check", "--fix", "."], check=False)


def run_tests() -> bool:
    result = run(["pytest", "-q"], check=False)
    print(result.stdout)
    print(result.stderr)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="не пушити і не відкривати PR, лише показати план")
    args = parser.parse_args()

    run_autofix()

    if not has_pending_changes():
        print("Деплой-агент: нічого виправляти, репозиторій чистий.")
        return 0

    diff_text = run(["git", "diff"], check=False).stdout

    if not run_tests():
        print("Деплой-агент: тести червоні після автофіксу — відкидаю зміни, PR не відкриваю.")
        run(["git", "checkout", "--", "."], check=False)
        return 1

    branch = f"deploy-agent/auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    assessment = assess_diff(diff_text)

    if args.dry_run:
        print(f"[dry-run] створив би гілку {branch}, відкрив PR, trivial={assessment['trivial']} ({assessment['reason']})")
        run(["git", "checkout", "--", "."], check=False)
        return 0

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

    if assessment["trivial"]:
        merge = gh("pr", "merge", branch, "--squash", "--delete-branch", check=False)
        if merge.returncode == 0:
            notify_telegram(f"🚀 Деплой-агент: автомердж тривіального PR\n{pr_url}\n\n{assessment['reason']}")
        else:
            notify_telegram(
                f"⚠️ Деплой-агент: PR оцінено як тривіальний, але автомердж не вдався "
                f"(можливо, потрібен review за правилами репо). Потрібне ручне підтвердження:\n{pr_url}\n\n{merge.stderr}"
            )
    else:
        notify_telegram(
            f"🔍 Деплой-агент відкрив PR, що потребує ручного підтвердження:\n{pr_url}\n\n"
            f"Причина не автомерджити: {assessment['reason']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
