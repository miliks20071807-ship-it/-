"""
Ідея-агент: генерує продуктові й контентні ідеї для розвитку продукту.

Окремо від orchestrator.py — той розпізнає намір у вільному тексті
(chat-класифікатор), цей — детермінований генератор за командою чи
розкладом, без інтерпретації довільного вводу користувача.

Опис продукту винесено в PRODUCT.md (не хардкод у промпті), щоб
міняти позиціонування без правок коду.

Продуктові ідеї — безкоштовний тариф Google Gemini як основний рушій
(без карти — aistudio.google.com/app/apikey), Claude — фолбек, якщо
Gemini недоступний. Контентні ідеї — ЗАВЖДИ через Claude напряму
(без Gemini): їм потрібен вбудований web_search tool, якого немає в
безкоштовних провайдерах, щоб формати коротких відео спирались на
реальні поточні тренди, а не лише на знання моделі з тренування
(продуктовим ідеям пошук трендів не потрібен — там і далі Gemini).
"""

from __future__ import annotations

import json
import os

from anthropic import Anthropic

import api_usage
from agents.common import call_gemini, extract_text, load_product_description

CLAUDE_MODEL = "claude-sonnet-5"
GEMINI_MODEL = "gemini-3.6-flash"

PRODUCT_SYSTEM_PROMPT = """Ти — продуктовий стратег невеликого стартапу.

Продукт:
{product}

Згенеруй {n} конкретних продуктових ідей — нові фічі, покращення UX,
механіки утримання/залучення користувачів. Кожна ідея: одна конкретна
фіча (не абстрактний напрямок) + одне речення, чому саме вона підвищить
утримання чи залучення.

Відповідай ЛИШЕ JSON-масивом рядків, без пояснень навколо:
["Назва фічі — чому вона працює.", "..."]
"""

CONTENT_SYSTEM_PROMPT = """Ти — контент-стратег, що просуває мобільний
застосунок через короткі відео (Reels/TikTok, формат 15-30 секунд,
спільний під обидві платформи).

Продукт:
{product}

СПЕРШУ скористайся web_search і знайди актуальні (за останні кілька
місяців) формати й тренди коротких відео — не покладайся лише на
власні знання з тренування, формати змінюються швидко, а мета саме
відштовхнутись від реально популярного зараз, а не застарілого.

ПОТІМ, на основі знайдених трендів, згенеруй {n} конкретних ідей
коротких відео під цей конкретний продукт. Кожна ідея — реальний
формат короткого відео (челендж, "до/після", "3 речі, які...",
закадровий голос під екран застосунку тощо), а НЕ абстрактне
"розкажи про фічу". Опиши конкретний хук/концепцію одним-двома
реченнями — за бажанням згадай, який тренд/формат надихнув ідею.

Після пошуку відповідай ЛИШЕ JSON-масивом рядків, без пояснень
навколо і без посилань на джерела в самому масиві:
["Хук/концепція відео.", "..."]
"""


def _generate_via_claude(system: str, user: str) -> str:
    """Фолбек на Claude, коли Gemini недоступний. Перед платним викликом
    перевіряє денний ліміт Claude API (api_usage.guard()) — щоб
    недоступність безкоштовного Gemini не призводила до мовчазного
    накопичення рахунку понад ліміт."""
    api_usage.guard()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=6000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": "xhigh"},
        messages=[{"role": "user", "content": user}],
    )
    api_usage.record_call()
    return extract_text(response)


def _generate(system_prompt: str, n: int) -> list[str]:
    system = system_prompt.format(product=load_product_description(), n=n)
    user = f"Згенеруй {n} ідей."

    try:
        raw = call_gemini(system, user, model=GEMINI_MODEL, max_tokens=4000)
    except Exception as e:  # noqa: BLE001 — навмисно широко: будь-яка помилка безкоштовного API веде на фолбек, а не крашить фічу
        print(f"[ідеї] Gemini недоступний ({e}), фолбек на Claude")
        raw = _generate_via_claude(system, user)

    raw = raw.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        ideas = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    return [str(idea) for idea in ideas][:n]


def generate_product_ideas(n: int = 5) -> list[str]:
    return _generate(PRODUCT_SYSTEM_PROMPT, n)


def generate_content_ideas(n: int = 5) -> list[str]:
    """Контентні ідеї — окремий шлях від generate_product_ideas: завжди
    напряму через Claude з увімкненим web_search tool (не Gemini/Groq —
    вбудований пошук є лише в Anthropic API), щоб ідеї спирались на
    реальні поточні тренди коротких відео, а не тільки на творчість
    моделі. api_usage.guard() тут теж діє — це такий самий платний
    виклик Claude, як і будь-який інший, просто без фолбек-гілки."""
    api_usage.guard()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=CONTENT_SYSTEM_PROMPT.format(product=load_product_description(), n=n),
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": f"Згенеруй {n} ідей на основі актуальних трендів."}],
    )
    api_usage.record_call()

    raw = extract_text(response).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        ideas = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    return [str(idea) for idea in ideas][:n]
