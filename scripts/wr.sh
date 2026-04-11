#!/usr/bin/env bash
# StreamWrangler wrapper — runs wrangle from any directory.
# Usage: wr <command> [args]   e.g. wr status, wr report --group sports
cd /home/geoffrey/IPTVEditor
exec .venv/bin/wrangle "$@"
