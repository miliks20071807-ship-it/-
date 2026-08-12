"""
Рев'ю-агент.

Тригериться GitHub Actions на новий PR, відкритий деплой- чи
багфікс-агентом (див. .github/workflows/review-agent.yml — фільтр за
префіксом гілки deploy-agent/ або bugfix-agent/, людські PR не в
скоупі). Claude аналізує diff — шукає очевидні баги, проблеми стилю,
потенційні security-діри (захардкожені секрети, SQL-ін'єкції,
відсутню валідацію вводу) — і лишає коментар прямо в PR на GitHub.

НІКОЛИ не блокує і не мерджить — лише інформує людину, яка все одно
тисне "Затвердити"/"Відхилити" в Telegram сама (рішення команди:
перевіряються навіть тривіальні PR деплой-агента, бо саме автомердж —
єдиний шлях у системі без живої людини між кодом і продом).

Локальний тест без реального GitHub-репозиторію:
    python agents/review_agent.py review <номер-PR>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from anthropic import Anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api_usage
from agents.common import extract_text, gh, set_output

MODEL = "claude-sonnet-5"

REVIEW_SYSTEM_PROMPT = """Ти — код-рев'юер, що перевіряє diff PR у невеликому
Python-проєкті команди 2-5 людей (Telegram-бот на aiogram + Anthropic SDK).

Шукай:
- очевидні баги (логічні помилки, необроблені винятки, off-by-one тощо)
- проблеми стилю (неконсистентність з рештою кодової бази, заплутаний код)
- потенційні security-діри (захардкожені секрети/токени, SQL-ін'єкції,
  відсутня валідація користувацького вводу, command/shell injection)

Diff відкритий автоматичним агентом (деплой- чи багфікс-агент), тести
вже пройшли — не повторюй те, що й так перевіряють тести. Якщо diff
чистий, так і скажи, не вигадуй зауважень про всяк випадок.

Відповідай ЛИШЕ JSON:
{
  "issues": [
    {"severity": "bug" | "style" | "security", "description": "конкретно що і де (файл, якщо видно з diff)"}
  ],
  "summary": "одне-два речення загального висновку"
}
"""


def review_diff(pr_title: str, diff_text: str) -> dict:
    api_usage.guard()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"PR: {pr_title}\n\n```diff\n{diff_text[:12000]}\n```"}],
    )
    api_usage.record_call()
    raw = extract_text(response).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"issues": [], "summary": "не вдалось розпарсити відповідь моделі"}
    result.setdefault("issues", [])
    result.setdefault("summary", "")
    return result


_SEVERITY_EMOJI = {"bug": "🐛", "style": "🎨", "security": "🔒"}


def _format_comment(review: dict) -> str:
    if not review["issues"]:
        return f"🔍 **Автоматичне рев'ю** (Claude)\n\n✅ Зауважень не знайдено. {review['summary']}"

    lines = [f"🔍 **Автоматичне рев'ю** (Claude)\n\n{review['summary']}\n"]
    for issue in review["issues"]:
        emoji = _SEVERITY_EMOJI.get(issue.get("severity"), "•")
        lines.append(f"- {emoji} **{issue.get('severity', '?')}**: {issue.get('description', '')}")
    return "\n".join(lines)


def cmd_review(args: argparse.Namespace) -> int:
    pr = json.loads(gh("pr", "view", str(args.pr_number), "--json", "title,url").stdout)

    try:
        review = review_diff(pr["title"], gh("pr", "diff", str(args.pr_number)).stdout)
    except api_usage.AnthropicLimitExceeded:
        set_output("skipped", "true")
        set_output("should_warn", "true" if api_usage.should_warn_once() else "false")
        return 0
    set_output("skipped", "false")

    set_output("pr_url", pr["url"])
    set_output("issue_count", str(len(review["issues"])))
    set_output("summary", review["summary"])

    gh("pr", "comment", str(args.pr_number), "--body", _format_comment(review))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_review = sub.add_parser("review")
    p_review.add_argument("pr_number")

    args = parser.parse_args()
    handlers = {"review": cmd_review}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
