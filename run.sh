#!/usr/bin/env bash
# Start the bot on the host (real laptop shell + system monitoring).
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python bot.py
