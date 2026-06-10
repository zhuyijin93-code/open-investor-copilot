#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import pathlib
import sys
import urllib.error

from monitor import detect_group_webhook, send_lark_group_webhook, send_lark_outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a custom message to configured Lark outputs.")
    parser.add_argument("--group-only", action="store_true", help="Send only to the configured Lark group webhook.")
    parser.add_argument("--title", help="Optional title line prepended to the message.")
    parser.add_argument("--file", type=pathlib.Path, help="Read message content from a file.")
    return parser.parse_args()


def read_message(args: argparse.Namespace) -> str:
    if args.file:
        return args.file.read_text(encoding="utf-8").strip()
    return sys.stdin.read().strip()


def main() -> int:
    args = parse_args()
    body = read_message(args)
    if not body:
        print("Message body is empty.", file=sys.stderr)
        return 1

    message = body if not args.title else f"{args.title}\n\n{body}"
    try:
        if args.group_only:
            if not detect_group_webhook():
                raise RuntimeError(
                    "No Lark group webhook configured. Set LARK_GROUP_WEBHOOK or "
                    "fill .cache/local_settings.json field lark_group_webhook."
                )
            send_lark_group_webhook(message)
        else:
            send_lark_outputs(message)
    except Exception as exc:
        print(f"Lark send failed: {format_send_error(exc)}", file=sys.stderr)
        return 1

    print("Message sent to Lark.")
    return 0


def format_send_error(exc: Exception) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
    if isinstance(reason, socket.gaierror):
        return (
            f"{exc}. DNS lookup failed for the Lark webhook host. "
            "If this is running in Codex sandbox/network-restricted mode, rerun the same command with network approval."
        )
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
