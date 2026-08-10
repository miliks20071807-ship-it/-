"""
Дизайн-агент: команда /дизайн <опис> у боті-дизайнері генерує
самодостатній HTML/CSS-мокап екрана продукту й повертає файл у чат —
відкривається в браузері як реальний візуальний макет.

Не малює растрові картинки (жодна текстова модель не вміє) — тому
"дизайн" тут завжди робочий HTML/CSS, незалежно від провайдера.

Основний рушій — безкоштовний тариф Groq (Kimi K2, сильна для
коду/HTML, без карти — console.groq.com/keys), щоб не платити за
Claude на кожен мокап. Якщо GROQ_API_KEY не задано чи запит впав —
автоматичний фолбек на Claude (дорожче, але надійно), щоб фіча не
ламалась через тимчасову недоступність безкоштовного тарифу.
"""

from __future__ import annotations

import os
from pathlib import Path

from anthropic import Anthropic

from agents.common import call_groq, extract_text, load_product_description

CLAUDE_MODEL = "claude-sonnet-5"
GROQ_MODEL = "moonshotai/kimi-k2-instruct"
OUTPUT_PATH = Path(__file__).parent / "design.html"

SYSTEM_PROMPT = """Ти — продуктовий дизайнер застосунку, описаного нижче.

Продукт (структура, розділи, дизайн-система — кольори/типографіка/форма):
{product}

Отримуєш короткий опис екрана чи фічі й повертаєш ОДИН самодостатній
HTML-файл (весь CSS інлайном у <style>, без зовнішніх ресурсів,
шрифтів чи бібліотек з інтернету) — робочий мокап, який одразу
відкривається в браузері.

Вимоги:
- Дотримуйся дизайн-системи продукту вище буквально: ті самі HEX-токени
  кольору (bg/card/line/ink/dim/faint/grn/red/gold/blue), ті самі радіуси
  заокруглення, правило "колір лише як функціональний сигнал, не
  декоративний фон". Якщо в описі немає вказівки на конкретний розділ —
  візуально впізнавано продовжуй той самий стиль, що й решта застосунку.
- Мобільний формат: контейнер шириною ~390px по центру сторінки, з
  тінню/рамкою, що імітує екран телефону (радіус ~32px).
- Реалістичний UI: справжні підписи, кнопки, іконки через unicode/emoji
  (не <img> і не зовнішні іконки) — без "Lorem ipsum" чи placeholder-тексту.
- Чиста типографіка (Onest для тексту, Unbounded для великих
  чисел/hero-значень — якщо шрифти недоступні офлайн, підстав системний
  sans-serif з такою ж заокругленістю, без CDN-посилань), акуратні
  відступи, без зовнішніх CDN (ніяких <link> на Google Fonts/Bootstrap/
  Tailwind).
- Відповідай ЛИШЕ кодом HTML, без пояснень навколо і без ```-огорож.
"""


def _generate_via_claude(system: str, description: str) -> str:
    """Фолбек на Claude, коли Groq недоступний. xhigh-thinking з'їдає
    тисячі токенів max_tokens ще до самого HTML — тому ліміт піднято, а
    виклик стрімінговий: SDK сам вимагає streaming для запитів, що
    можуть перевищити 10 хв (ValueError без цього при високому
    max_tokens)."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": "xhigh"},
        messages=[{"role": "user", "content": description}],
    ) as stream:
        response = stream.get_final_message()
    return extract_text(response)


def generate_mockup(description: str) -> Path:
    """Генерує design.html за описом і повертає шлях до файлу."""
    system = SYSTEM_PROMPT.format(product=load_product_description())

    try:
        html = call_groq(system, description, model=GROQ_MODEL, max_tokens=8000)
    except Exception as e:  # noqa: BLE001 — навмисно широко: будь-яка помилка безкоштовного API веде на фолбек, а не крашить фічу
        print(f"[дизайн] Groq недоступний ({e}), фолбек на Claude")
        html = _generate_via_claude(system, description)

    html = html.strip()
    html = html.removeprefix("```html").removeprefix("```").removesuffix("```").strip()

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return OUTPUT_PATH
