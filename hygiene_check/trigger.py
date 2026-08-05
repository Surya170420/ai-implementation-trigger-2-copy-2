"""Run hygiene_check.pipeline every 4 hours and send alert emails.

Usage:
    uv run -m hygiene_check.trigger
    python -m hygiene_check.trigger
"""

from __future__ import annotations

import datetime
import sys
import time

from hygiene_check.pipeline import main

# 4 hours in seconds (4 * 60 * 60)
INTERVAL_SECONDS = 14400
PIPELINE_ARGS = ["--send-email"]


if __name__ == "__main__":
    print("Hygiene-check scheduler started (every 4 hours, with --send-email).")

    while True:
        print(f"[{datetime.datetime.now()}] Triggering hygiene check pipeline...")

        try:
            exit_code = main(PIPELINE_ARGS)
            if exit_code != 0:
                print(f"Pipeline exited with code {exit_code}", file=sys.stderr)
        except Exception as e:
            print(f"Error occurred during execution: {e}", file=sys.stderr)

        print(f"[{datetime.datetime.now()}] Task finished. Sleeping for 4 hours...\n")
        time.sleep(INTERVAL_SECONDS)