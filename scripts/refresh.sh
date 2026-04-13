#!/bin/bash
# StreamWrangler refresh — fetch latest feed, rebuild M3U for Dispatcharr
# Runs every 2 hours via cron. Logs to /var/log/streamwrangler.log

set -e

WRANGLE=/home/geoffrey/IPTVEditor/.venv/bin/wrangle
LOG=/home/geoffrey/logs/streamwrangler.log
WORKDIR=/home/geoffrey/IPTVEditor

cd "$WORKDIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting refresh..." >> "$LOG"

$WRANGLE ingest --force >> "$LOG" 2>&1
$WRANGLE output >> "$LOG" 2>&1
$WRANGLE epg >> "$LOG" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done." >> "$LOG"
