#!/bin/bash
# Ежедневный бэкап в Telegram: база заявок (история + file_id отчётов),
# правила команды и сценарий аудита. Архив крошечный, облако TG — хранилище.
TELEGRAM_BOT_TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" /opt/audit-service/.env | cut -d= -f2-)
TELEGRAM_CHAT_ID=$(grep "^TELEGRAM_CHAT_ID=" /opt/audit-service/.env | cut -d= -f2-)
ARCHIVE="/tmp/audit-backup-$(date +%Y%m%d).tar.gz"

tar czf "$ARCHIVE" \
  -C /opt/audit-service data/queue.db \
  -C /home/audit/.claude/skills/usability-audit SKILL.md CHANGELOG.md \
  -C /home/audit/.claude/skills/usability-audit/references custom-rules.md \
  2>/dev/null

curl -s -F "chat_id=${TELEGRAM_CHAT_ID}" \
     -F "document=@${ARCHIVE}" \
     -F "caption=🗄 Ежедневный бэкап: база заявок + сценарий + правила ($(date +%d.%m.%Y)). Восстановление — распаковать по тем же путям." \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" > /dev/null
rm -f "$ARCHIVE"
