#!/bin/bash
#
# 💾 BACKUP SCRIPT - Automatic backup of bot data
#
# Usage: ./backup.sh [destination_folder]
#
# Cron setup for daily backup at 00:00:
#   crontab -e
#   0 0 * * * /home/marcin/monad_engine/backup.sh >> /home/marcin/monad_engine/logs/backup.log 2>&1
#

set -e

# Configuration
BOT_DIR="${HOME}/monad_engine"
BACKUP_BASE="${1:-${HOME}/backups/monad_bot}"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_BASE}/${DATE}"
KEEP_DAYS=7

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}💾 Monad Bot Backup - ${DATE}${NC}"
echo "================================"

# Create backup directory
mkdir -p "${BACKUP_DIR}"

# Backup critical files
echo -e "${YELLOW}Backing up positions...${NC}"
cp "${BOT_DIR}/positions.json" "${BACKUP_DIR}/" 2>/dev/null || echo "  No positions.json"

echo -e "${YELLOW}Backing up trades...${NC}"
cp "${BOT_DIR}/trades.json" "${BACKUP_DIR}/" 2>/dev/null || echo "  No trades.json"

echo -e "${YELLOW}Backing up environment...${NC}"
cp "${BOT_DIR}/.env" "${BACKUP_DIR}/env.backup" 2>/dev/null || echo "  No .env"

echo -e "${YELLOW}Backing up config...${NC}"
cp "${BOT_DIR}/agents/config.py" "${BACKUP_DIR}/" 2>/dev/null || echo "  No config.py"

echo -e "${YELLOW}Backing up memories...${NC}"
cp -r "${BOT_DIR}/agents/memory/data" "${BACKUP_DIR}/memory_data" 2>/dev/null || echo "  No memory data"

echo -e "${YELLOW}Backing up logs (last 1000 lines)...${NC}"
tail -1000 "${BOT_DIR}/bot.log" > "${BACKUP_DIR}/bot.log" 2>/dev/null || echo "  No bot.log"

# Create stats summary
echo -e "${YELLOW}Creating summary...${NC}"
cat > "${BACKUP_DIR}/summary.txt" << EOF
Monad Bot Backup Summary
========================
Date: ${DATE}
Hostname: $(hostname)

Bot Status: $(pgrep -f "agents.orchestrator" > /dev/null && echo "RUNNING" || echo "STOPPED")

Positions: $(cat "${BOT_DIR}/positions.json" 2>/dev/null | grep -c '"token"' || echo "0")
Trades: $(cat "${BOT_DIR}/trades.json" 2>/dev/null | grep -c '"action"' || echo "0")

Backup Size: $(du -sh "${BACKUP_DIR}" | cut -f1)
EOF

# Compress backup
echo -e "${YELLOW}Compressing backup...${NC}"
cd "${BACKUP_BASE}"
tar -czf "${DATE}.tar.gz" "${DATE}"
rm -rf "${DATE}"

# Cleanup old backups
echo -e "${YELLOW}Cleaning old backups (keeping ${KEEP_DAYS} days)...${NC}"
find "${BACKUP_BASE}" -name "*.tar.gz" -mtime +${KEEP_DAYS} -delete

# Done
BACKUP_SIZE=$(du -h "${BACKUP_BASE}/${DATE}.tar.gz" | cut -f1)
echo ""
echo -e "${GREEN}✅ Backup completed!${NC}"
echo "   Location: ${BACKUP_BASE}/${DATE}.tar.gz"
echo "   Size: ${BACKUP_SIZE}"
echo ""

# List recent backups
echo "Recent backups:"
ls -lh "${BACKUP_BASE}"/*.tar.gz 2>/dev/null | tail -5
