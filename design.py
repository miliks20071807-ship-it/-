"""
Дизайн-агент: команда /дизайн <опис> у боті-дизайнері генерує
самодостатній HTML/CSS-мокап екрана продукту через Claude і повертає
файл у чат — відкривається в браузері як реальний візуальний макет.

Claude — текстова модель, не DALL-E, малювати растрові картинки не
вміє. Тому "дизайн" тут — робочий HTML/CSS, а не згенероване
зображення. Без нових залежностей (лише anthropic SDK, вже є в проєкті).
"""

from __future__ import annotations

import os
from pathlib import Path

from anthropic import Anthropic

from agents.common import extract_text, load_product_description

MODEL = "claude-sonnet-5"
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


def generate_mockup(description: str) -> Path:
    """Генерує design.html за описом і повертає шлях до файлу.

    xhigh-thinking з'їдає left thousands токенів з max_tokens ще до
    самого HTML — тому ліміт піднято, а виклик стрімінговий: SDK сам
    вимагає streaming для запитів, що можуть перевищити 10 хв
    (ValueError без цього при високому max_tokens)."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=SYSTEM_PROMPT.format(product=load_product_description()),
        thinking={"type": "adaptive"},
        output_config={"effort": "xhigh"},
        messages=[{"role": "user", "content": description}],
    ) as stream:
        response = stream.get_final_message()
    html = extract_text(response).strip()
    html = html.removeprefix("```html").removeprefix("```").removesuffix("```").strip()

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return OUTPUT_PATH
