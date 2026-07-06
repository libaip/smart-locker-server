#!/bin/bash
BACKUP_DIR="/home/ubuntu/smart-locker/backup/db"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d_%H%M%S)
FILENAME="smart_locker_${DATE}.sql.gz"

PGPASSWORD=locker_pass_2024 pg_dump -h 127.0.0.1 -U locker_admin smart_locker | gzip > "${BACKUP_DIR}/${FILENAME}"

# 保留最近30天的备份
find "$BACKUP_DIR" -name "smart_locker_*.sql.gz" -mtime +30 -delete

echo "$(date): Backup ${FILENAME} done, size=$(du -h ${BACKUP_DIR}/${FILENAME} | cut -f1)"
