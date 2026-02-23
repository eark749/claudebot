#!/usr/bin/env python3
"""
Standalone worker that runs stream_chat in an isolated process.
Reads JSON from stdin, writes events as newline-delimited JSON to stdout.
Used to avoid claude-agent-sdk's anyio cancel scope bug that poisons the event loop.
"""

import asyncio
import json
import sys
from pathlib import Path

# Load .env from project root (parent of backend/)
from dotenv import load_dotenv
_root = Path(__file__).resolve().parent.parent
load_dotenv(_root / ".env")

from claude_service import stream_chat


def main() -> None:
    try:
        input_json = json.load(sys.stdin)
        prompt = input_json.get("prompt", "")
        claude_session_id = input_json.get("claude_session_id") or None
        system_prompt = input_json.get("system_prompt") or None
    except (json.JSONDecodeError, KeyError) as e:
        sys.stderr.write(f"Worker input error: {e}\n")
        sys.exit(1)

    async def run() -> None:
        try:
            async for event_type, data in stream_chat(
                prompt=prompt,
                claude_session_id=claude_session_id,
                system_prompt=system_prompt,
            ):
                line = json.dumps({"event": event_type, "data": data}) + "\n"
                sys.stdout.write(line)
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"Worker error: {e}\n")
            sys.stdout.write(json.dumps({"event": "error", "data": str(e)}) + "\n")
            sys.stdout.flush()

    asyncio.run(run())


if __name__ == "__main__":
    main()
