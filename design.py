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

from agents.common import extract_text

MODEL = "claude-sonnet-5"
OUTPUT_PATH = Path(__file__).parent / "design.html"

SYSTEM_PROMPT = """Ти — продуктовий дизайнер мобільного застосунку
"календар + AI-асистент, адаптивний під професію користувача".

Отримуєш короткий опис екрана чи фічі й повертаєш ОДИН самодостатній
HTML-файл (весь CSS інлайном у <style>, без зовнішніх ресурсів,
шрифтів чи бібліотек з інтернету) — робочий мокап, який одразу
відкривається в браузері.

Вимоги:
- Мобільний формат: контейнер шириною ~390px по центру сторінки, з
  тінню/рамкою, що імітує екран телефону.
- Реалістичний UI: справжні підписи, кнопки, іконки через unicode/emoji
  (не <img> і не зовнішні іконки) — без "Lorem ipsum" чи placeholder-тексту.
- Чиста типографіка, акуратні відступи, без зовнішніх CDN
  (ніяких <link> на Google Fonts/Bootstrap/Tailwind).
- Відповідай ЛИШЕ кодом HTML, без пояснень навколо і без ```-огорож.
"""


def generate_mockup(description: str) -> Path:
    """Генерує design.html за описом і повертає шлях до файлу."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=12000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "xhigh"},
        messages=[{"role": "user", "content": description}],
    )
    html = extract_text(response).strip()
    html = html.removeprefix("```html").removeprefix("```").removesuffix("```").strip()

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return OUTPUT_PATH
