"""
Changelog-агент.

Тригериться GitHub Actions на успішний мердж PR у main (див.
.github/workflows/changelog-agent.yml — pull_request:closed з
перевіркою merged == true). Claude бере заголовок PR, повідомлення
комітів і diff та формулює короткий запис людською мовою (не
технічний опис diff'а) — той самий текст іде і в Telegram, і
дописується в CHANGELOG.md прямо в main (без окремого PR — це разовий
допис у файл, а не зміна коду).

Спрацьовує на будь-який змерджений PR (людський, деплой-агента,
багфікс-агента) — без фільтрів за гілкою, на відміну від рев'ю-агента.

Локальний тест без реального GitHub-репозиторію:
    python agents/changelog_agent.py generate <номер-PR>
(допише в CHANGELOG.md і закомітить локально; push вимагає git remote
з правом push).
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
import api_usage
from agents.common import extract_text, gh, run, set_output

MODEL = "claude-sonnet-5"
REPO_ROOT = Path(__file__).parent.parent
CHANGELOG_FILE = REPO_ROOT / "CHANGELOG.md"

CHANGELOG_SYSTEM_PROMPT = """Ти пишеш короткий запис у CHANGELOG команди
(2-5 людей), що веде Telegram-бот з AI-агентами.

Отримуєш заголовок PR, повідомлення комітів і diff. Сформулюй 1-4
пункти змін звичайною людською мовою — НЕ технічний опис diff'а, а що
це означає для користувача/команди (напр. "Додано швидке додавання
задач через тег @дизайн", а не "Implemented maybe_dispatch_tagged_task
in bot.py").

Якщо зміна суто технічна/внутрішня (рефакторинг, лінт, тести) і не має
видимого ефекту — так і напиши одним реченням ("Внутрішнє технічне
покращення, без видимих змін для користувача").

Відповідай ЛИШЕ списком, без заголовка й пояснень навколо:
- Перший пункт.
- Другий пункт (якщо є).
"""


def generate_changelog(pr_title: str, commits_text: str, diff_text: str) -> str:
    api_usage.guard()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=CHANGELOG_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"PR: {pr_title}\n\nКоміти:\n{commits_text}\n\n```diff\n{diff_text[:8000]}\n```",
        }],
    )
    api_usage.record_call()
    return extract_text(response).strip()


def _prepend_entry(entry_block: str) -> None:
    existing = CHANGELOG_FILE.read_text(encoding="utf-8") if CHANGELOG_FILE.exists() else "# Changelog\n"

    marker = "\n## "
    idx = existing.find(marker)
    if idx == -1:
        # Ще немає жодного запису — дописуємо в кінець після хедера.
        new_content = existing.rstrip("\n") + "\n\n" + entry_block + "\n"
    else:
        # Вставляємо новий запис перед найпершим існуючим (найновіші зверху).
        new_content = existing[:idx + 1] + entry_block + "\n\n" + existing[idx + 1:]

    CHANGELOG_FILE.write_text(new_content, encoding="utf-8")


def cmd_generate(args: argparse.Namespace) -> int:
    pr = json.loads(gh("pr", "view", str(args.pr_number), "--json", "title,url,commits").stdout)
    commits_text = "\n".join(f"- {c['messageHeadline']}" for c in pr.get("commits", [])) or "(немає даних про коміти)"
    diff_text = gh("pr", "diff", str(args.pr_number)).stdout

    try:
        changelog_text = generate_changelog(pr["title"], commits_text, diff_text)
    except api_usage.AnthropicLimitExceeded:
        set_output("skipped", "true")
        set_output("should_warn", "true" if api_usage.should_warn_once() else "false")
        return 0
    set_output("skipped", "false")

    entry_block = f"## {datetime.now(timezone.utc):%Y-%m-%d} — PR #{args.pr_number}: {pr['title']}\n\n{changelog_text}"
    _prepend_entry(entry_block)

    run(["git", "add", "CHANGELOG.md"])
    run(["git", "commit", "-m", f"changelog-agent: PR #{args.pr_number}"])
    run(["git", "push", "origin", "main"])

    set_output("pr_url", pr["url"])
    set_output("changelog_text", changelog_text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_generate = sub.add_parser("generate")
    p_generate.add_argument("pr_number")

    args = parser.parse_args()
    handlers = {"generate": cmd_generate}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
