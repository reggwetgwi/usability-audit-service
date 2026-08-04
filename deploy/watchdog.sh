#!/bin/bash
# Сторож сервиса аудитов: раз в 10 минут (cron) проверяет живость и диск.
# Токен и чат берёт из .env сервиса.
TELEGRAM_BOT_TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" /opt/audit-service/.env | cut -d= -f2-)
TELEGRAM_CHAT_ID=$(grep "^TELEGRAM_CHAT_ID=" /opt/audit-service/.env | cut -d= -f2-)
TG="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"

alert() {
  curl -s -G "$TG" --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
       --data-urlencode "text=$1" > /dev/null
}

ok() { curl -s -o /dev/null --max-time 10 http://127.0.0.1:8080/; }

if ! ok; then
  sleep 15   # не дёргаем рестарт по единичному чиху
  if ! ok; then
    if pgrep -u audit -f 'claude -p' > /dev/null; then
      # рестарт убьёт работающий аудит — при живом claude только сигналим
      alert "⚠️ Сервис аудитов не отвечает на проверку, но идёт активный аудит — не перезапускаю, чтобы не убить работу. Обычно это нагрузка при съёмке, само пройдёт."
    else
      systemctl restart audit-service
      sleep 8
      if ok; then
        alert "⚠️ Сервис аудитов не отвечал — перезапущен сторожем, снова в строю."
      else
        alert "🔴 Сервис аудитов упал и НЕ поднялся после перезапуска — нужно вмешательство (ssh <сервер>, journalctl -u audit-service)."
      fi
    fi
  fi
fi

# диск: тревога при 90%+, не чаще раза в сутки
USE=$(df / --output=pcent | tail -1 | tr -d ' %')
MARKER=/tmp/.disk-alert-sent
if [ "$USE" -ge 90 ]; then
  if [ ! -f "$MARKER" ] || [ -n "$(find $MARKER -mmin +1440)" ]; then
    alert "⚠️ Диск сервера аудитов заполнен на ${USE}% — новые аудиты могут падать. Автоочистка работает, но место кончается."
    touch "$MARKER"
  fi
fi
