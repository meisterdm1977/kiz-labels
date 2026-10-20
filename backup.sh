#!/bin/sh
# Ежедневная резервная копия базы и этикеток. Запускается по расписанию (cron), см. README.
set -e
cd "$(dirname "$0")"
mkdir -p backups
stamp=$(date +%Y-%m-%d)
docker compose exec -T db pg_dump -U kiz -Fc kiz > "backups/db-$stamp.dump"
tar -czf "backups/data-$stamp.tar.gz" -C volumes data
# Храним копии за последние 14 дней.
find backups -type f -mtime +14 -delete
