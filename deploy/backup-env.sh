#!/bin/bash
# Резервная копия боевого конфига /opt/audit-service/.env.
# Запускается ежедневно из /etc/cron.d/audit-env-backup.
#
# Зачем: 31.07.2026 локальный виндовый .env залили поверх боевого — пропали ключ
# freemodel, KEYSO_TOKEN, SMB-доступы, прокси и chat_id команды; аудиты падали, а
# уведомления уходили в личку. Восстанавливаться пришлось из ручной копии
# двухдневной давности, часть настроек пришлось вспоминать.
#
# Копия создаётся ТОЛЬКО если конфиг изменился с прошлого раза — иначе в каталоге
# копились бы одинаковые файлы, и история правок в них не читалась бы.
set -u

ENV_FILE=/opt/audit-service/.env
DIR=/opt/audit-service/env-backups
KEEP_DAYS=60      # удалять копии старше стольких дней…
KEEP_MIN=10       # …но всегда оставлять столько последних, даже если они старые

if [ ! -s "$ENV_FILE" ]; then
    echo "backup-env: $ENV_FILE пуст или отсутствует — копию не делаю" >&2
    exit 1
fi

mkdir -p "$DIR"
chmod 700 "$DIR"

latest=$(ls -1 "$DIR"/env-* 2>/dev/null | tail -1)
if [ -n "${latest:-}" ] && cmp -s "$ENV_FILE" "$latest"; then
    exit 0  # конфиг не менялся — новая копия не нужна
fi

dest="$DIR/env-$(date +%F_%H%M%S)"
cp -p "$ENV_FILE" "$dest"
chmod 600 "$dest"
echo "backup-env: $(date +%F\ %H:%M) сохранена копия $dest"

# Чистка старья. Последние KEEP_MIN копий не трогаем никогда: если сервис долго
# не правили, все копии окажутся старыми — и без этой оговорки удалились бы все.
total=$(ls -1 "$DIR"/env-* 2>/dev/null | wc -l)
if [ "$total" -gt "$KEEP_MIN" ]; then
    ls -1 "$DIR"/env-* | head -n -"$KEEP_MIN" | while read -r f; do
        if [ -n "$(find "$f" -mtime +"$KEEP_DAYS" 2>/dev/null)" ]; then
            rm -f "$f" && echo "backup-env: удалена копия старше $KEEP_DAYS дней — $f"
        fi
    done
fi
