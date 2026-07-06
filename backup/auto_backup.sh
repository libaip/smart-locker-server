#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=/home/ubuntu/smart-locker/backup/db
mkdir -p $BACKUP_DIR
pg_dump -U locker_admin -h 127.0.0.1 -p 6432 smart_locker | gzip > $BACKUP_DIR/backup_$DATE.sql.gz
find $BACKUP_DIR -name "backup_*.sql.gz" -mtime +30 -delete
echo [$DATE] Backup done