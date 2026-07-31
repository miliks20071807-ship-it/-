# Telegram-система агентів команди

Архітектура: Telegram → оркестратор (Claude) → 4 агенти-виконавці
(задачі, деплой, багфікси, презентації) + окремий health-check процес.

```
Telegram → bot.py → orchestrator.classify() ─┬─ storage.py (задачі команди)
                                              ├─ presentation.py (/презентація)
                                              ├─ agents/deploy_agent.py (крон, GitHub Actions)
                                              └─ agents/bugfix_agent.py (issue → PR, GitHub Actions)
watchdog.py (окремо, cron) — пінгує bot.py, перезапускає, шле алерти
```

## Структура

- `bot.py` — точка входу бота: команди, обробка вільного тексту, глобальний обробник помилок, heartbeat
- `orchestrator.py` — Claude визначає намір повідомлення (задача / список / чат)
- `storage.py` — задачі команди, JSON-файл `tasks.json`
- `presentation.py` — генерація pptx-звіту для `/презентація`
- `agents/common.py` — спільне для крон-агентів: Telegram-алерти, GitHub issue/PR helpers
- `agents/deploy_agent.py` — деплой-агент (крок 2)
- `agents/bugfix_agent.py` — багфікс-агент (крок 3)
- `watchdog.py` — health-check процес (крок 5)
- `.github/workflows/` — креони й тригери для деплой-, багфікс-агентів і CI
- `tests/` — pytest-тести (потрібні, щоб деплой-агенту й CI було що ганяти)

## Встановлення

```bash
pip install -r requirements.txt --break-system-packages
cp env.example .env
# відкрийте .env і впишіть TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, ALLOWED_USER_IDS
python bot.py
```

Для локального лінту/тестів (не обов'язково для проду — CI ставить їх сам):

```bash
pip install pytest ruff --break-system-packages
ruff check .
pytest -q
```

## Налаштування репозиторію на GitHub (потрібне для кроків 2-3)

1. Запуште цей репозиторій на GitHub.
2. Settings → Secrets and variables → Actions → додайте:
   `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`.
3. Settings → Actions → General → "Workflow permissions" → увімкніть
   *Read and write permissions* і *Allow GitHub Actions to create and
   approve pull requests* — інакше `deploy_agent.py`/`bugfix_agent.py`
   не зможуть пушити гілки й відкривати PR через `GITHUB_TOKEN`.
4. Створіть лейбл `bug` у репозиторії (Issues → Labels), якщо його
   нема — на нього зав'язаний тригер багфікс-агента.

---

## Крок 1 — MVP-каркас (задачі команди)

Було в стартовому каркасі: `bot.py`, `orchestrator.py`, `storage.py`.
Перевірено: `python3 -m py_compile` на всіх файлах, повний прогін
`pytest` і `ruff check .` — чисто. Синтетичних тестів у каркасі не
було, тож додано `tests/test_storage.py`, аби деплой-агенту й CI (крок
2-3) було що реально ганяти.

**Як перевірити:** `pip install -r requirements.txt`, заповнити `.env`,
`python bot.py`, у Telegram — `/задача`, `/задачі`, `/done <id>`.

## Крок 2 — Деплой-агент

`agents/deploy_agent.py` + `.github/workflows/deploy-agent.yml` (крон
раз на годину, і `workflow_dispatch` для ручного запуску).

Що робить: `ruff check --fix .` → якщо є diff, ганяє `pytest` →
якщо тести червоні, відкидає зміни (нічого не пушить); якщо зелені —
відкриває PR і питає Claude, чи зміна "тривіальна й некритична"
(форматування/лінт/залежності). **Лише якщо так і тести зелені —
автомерджить** (`gh pr merge --squash`). В усіх інших випадках PR
лишається відкритим, і в Telegram-чат (`TELEGRAM_ALERT_CHAT_ID`) йде
повідомлення з проханням підтвердити вручну. Це єдиний агент системи
з правом автомерджу — за рішенням команди.

**Як перевірити:**
- Локально без реального GitHub: `python agents/deploy_agent.py --dry-run`
  (застосує ruff --fix, прожене тести, надрукує план — без push/PR).
- В GitHub Actions: вкладка Actions → "Deploy agent" → "Run workflow".

## Крок 3 — Багфікс-агент

`agents/bugfix_agent.py` + `.github/workflows/bugfix-agent.yml`
(тригер: GitHub issue з лейблом `bug`) + `.github/workflows/ci.yml`
(тести/лінт на кожен push/PR).

Джерела помилок (рішення: без Sentry, напряму з логів):
1. **CI на main впав** → `ci.yml` сам відкриває issue з лейблом `bug`
   і посиланням на лог провалу (з дедупом — не плодить дублікати).
2. **Виняток у проді** → глобальний `@dp.error()` у `bot.py` пише
   повний traceback у `errors.log` і, якщо на хості налаштовані
   `GITHUB_TOKEN`/`GITHUB_REPOSITORY`, відкриває issue з лейблом `bug`
   напряму через GitHub REST API (без залежності від `gh` CLI на проді).

У обох випадках issue з лейблом `bug` тригерить `bugfix-agent.yml`:
Claude отримує текст issue + весь `.py`-код репозиторію, пропонує фікс
одного файлу, скрипт застосовує його й ганяє тести. Тести червоні —
зміни відкидаються, в issue лишається коментар для людини. Тести
зелені — відкривається PR ("Fixes #N"). **Багфікс-агент ніколи не
мерджить сам** (навіть тривіальні фікси) — завжди чекає ручного
підтвердження в Telegram, за рішенням команди.

**Як перевірити:**
- Локально: `python agents/bugfix_agent.py <номер-issue> --dry-run`
  (виведе, який файл і чому змінив би, без реальних змін).
- Наживо: створіть issue з лейблом `bug` на GitHub — запуститься workflow.
- Прод-помилки: тимчасово зробіть так, щоб хендлер бота кинув виняток
  (напр. `/задача` без обробки якогось едж-кейсу) — перевірте, що
  з'явився запис в `errors.log` і (за наявності токена) нова issue.

## Крок 4 — Презентації

Команда `/презентація` (`presentation.py`, бібліотека `python-pptx`).

`/презентація` — звіт за 7 днів (дефолт), `/презентація 30` — за 30
днів, `/презентація all` — за весь час. Дані ті самі, що й у
`/задачі` (той самий `tasks.json`, нового сховища не додано). Слайди:
титульний, підсумок (закрито/відкрито/разом), список закритих задач,
список відкритих.

**Як перевірити:** додайте кілька задач через `/задача`, закрийте
одну через `/done`, викличте `/презентація` — бот надішле `report.pptx`
у чат. Локально без Telegram: `python -c "from presentation import
build_report; print(build_report(days=30))"`.

## Крок 5 — Health-check / watchdog

`watchdog.py` — окремий процес, **не агент** (немає доступу до Claude
API, не редагує код, не відкриває PR) — свідомо, щоб право
"перезапускати все" не поєднувалось із правом самомодифікації.

`bot.py` раз на 60с оновлює `heartbeat.txt` і при старті пише свій pid
у `bot.pid`. `watchdog.py` перевіряє: чи процес з `bot.pid` живий, і чи
`heartbeat.txt` не старший за 180с (3x інтервал з запасом). Якщо ні —
перезапускає `bot.py` (`subprocess.Popen`) і шле алерт у
`TELEGRAM_ALERT_CHAT_ID`. Деплой/багфікс-агенти сюди не входять — це
не довгоживучі процеси, а GitHub Actions джоби за розкладом/подією, і
провал самої джоби видно у вкладці Actions.

Запуск (рекомендовано — системний cron, раз на 5 хв):
```
*/5 * * * * cd /path/to/repo && /path/to/venv/bin/python watchdog.py
```
або в режимі фонового циклу: `python watchdog.py --loop --interval 300`.

**Як перевірити:** запустіть `python bot.py` в одному терміналі,
переконайтесь, що з'явились `bot.pid`/`heartbeat.txt`; `kill <pid>` з
`bot.pid` і одразу `python watchdog.py` — має надіслати алерт і
перезапустити бота. Без реального `TELEGRAM_ALERT_CHAT_ID` алерт просто
друкується в консоль (`[telegram alert skipped...]`), бот не падає.

## Відомі обмеження (свідомо не рішали зараз)

- `watchdog.py` розрахований на один довгоживучий процес бота на
  одному хості (без HA/декількох реплік) — достатньо для команди 2-5 людей.
- Багфікс-агент за один прогін чіпає рівно один файл — свідоме
  спрощення, щоб не городити diff/patch-логіку; для складніших багів
  він чесно скаже "не можу визначити фікс" і лишить issue людині.
- Деплой-агент сканує й відкриває PR лише в цьому самому репозиторії
  (боті), не в репозиторії мобільного застосунку — за рішенням команди.
