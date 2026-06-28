#!/bin/bash
# run_forever.sh - keeps monitor.py running no matter what.
# If the bot crashes or exits for any reason, this waits a few seconds and
# starts it again. Run THIS instead of running monitor.py directly:
#
#     chmod +x run_forever.sh
#     caffeinate -i ./run_forever.sh
#
# Stop it with Ctrl+C (you may need to press it twice).

cd "$(dirname "$0")" || exit 1

# Activate the virtualenv if you used one (adjust path if needed).
if [ -d "venv" ]; then
  source venv/bin/activate
fi

while true; do
  echo "[$(date)] starting monitor.py ..."
  python monitor.py
  code=$?
  echo "[$(date)] monitor.py exited with code $code. Restarting in 15s..."
  sleep 15
done
