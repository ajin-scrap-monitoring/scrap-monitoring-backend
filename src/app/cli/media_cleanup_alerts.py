"""Read-only media status observer; operational delivery needs explicit --send."""

import argparse
import asyncio
import fcntl
import json
import os
import stat
import time
from pathlib import Path

from src.app.operations.disk_alerts import load_config
from src.app.operations.media_cleanup_alerts import check_cleanup, observe
from src.app.services.gmail_sender import GmailSender
from src.app.services.media_client import MediaClient


async def execute(args: argparse.Namespace) -> None:
    sender, client = GmailSender(), MediaClient()
    try:
        while True:
            config = load_config(args.config)
            status = await observe(client)
            result = await check_cleanup(
                config, args.state, sender, time.time(), status, send=args.send
            )
            print(
                json.dumps(
                    {
                        "deliveryEnabled": args.send and config.enabled,
                        "samples": [
                            {"channel": n.channel, "active": n.active, "reason": n.reason}
                            for n in result
                        ],
                    },
                    allow_nan=False,
                ),
                flush=True,
            )
            if not args.loop:
                break
            await asyncio.sleep(args.interval_seconds)
    finally:
        await client.close()
        await sender.close()


def interval(value: str) -> int:
    seconds = int(value)
    if not 5 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("Interval must be 5..3600 seconds")
    return seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=interval, default=30)
    args = parser.parse_args()
    descriptor = None
    try:
        if args.send:
            if args.state.parent.is_symlink() or args.state.parent.stat().st_mode & 0o077:
                raise ValueError("Private state directory required")
            descriptor = os.open(
                str(args.state) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Private regular lock required")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(execute(args))
    except Exception:
        # No upstream bodies, credentials, operator addresses or private paths in logs.
        print(json.dumps({"error": "MEDIA_CLEANUP_ALERT_CHECK_FAILED"}), flush=True)
        raise SystemExit(1) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    main()
