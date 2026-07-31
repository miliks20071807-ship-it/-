#!/usr/bin/env bash
# Спільний скрипт для сповіщень у Telegram напряму з GitHub Actions
# через curl до Bot API — без python, щоб прогрес агента був видимий,
# навіть якщо python-скрипт кроку впаде чи ще не встиг відпрацювати.
#
# Потребує env TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID (з repo secrets).
# Ніколи не падає з ненульовим кодом — збій сповіщення не має зупиняти
# основний пайплайн деплою/багфіксу.
#
# Використання:
#   telegram_notify.sh text "повідомлення"
#   telegram_notify.sh approve "повідомлення" "<номер PR>"

mode="${1:-}"
api="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
  echo "[telegram_notify] TELEGRAM_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID не задані, пропускаю" >&2
  exit 0
fi

case "$mode" in
  text)
    text="${2:-}"
    curl -sS -X POST "$api" \
      --data-urlencode "chat_id=${TELEGRAM_ALERT_CHAT_ID}" \
      --data-urlencode "text=${text}" \
      -o /dev/null || echo "[telegram_notify] curl не вдався" >&2
    ;;
  approve)
    text="${2:-}"
    pr_number="${3:-}"
    reply_markup=$(printf '{"inline_keyboard":[[{"text":"✅ Затвердити і замерджити","callback_data":"pr:approve:%s"},{"text":"❌ Відхилити","callback_data":"pr:reject:%s"}]]}' "$pr_number" "$pr_number")
    curl -sS -X POST "$api" \
      --data-urlencode "chat_id=${TELEGRAM_ALERT_CHAT_ID}" \
      --data-urlencode "text=${text}" \
      --data-urlencode "reply_markup=${reply_markup}" \
      -o /dev/null || echo "[telegram_notify] curl не вдався" >&2
    ;;
  *)
    echo "[telegram_notify] невідомий режим: $mode" >&2
    ;;
esac

exit 0
