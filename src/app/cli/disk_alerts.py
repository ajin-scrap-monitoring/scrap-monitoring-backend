"""Standalone capacity checks using private operator configuration; default is inspection only."""

import argparse
import asyncio
import fcntl
import json
import os
import stat
import time
from pathlib import Path

from src.app.operations.disk_alerts import MonitorState, check, load_config, read_private
from src.app.services.gmail_sender import GmailSender


async def execute(args: argparse.Namespace) -> None:
    sender = GmailSender()
    try:
        while True:
            config = load_config(args.config)
            samples = await check(config, args.state, sender, time.time(), send=args.send)
            statuses: dict[str, int] = {}
            if args.state.exists():
                state = MonitorState.model_validate_json(read_private(args.state))
                for disk in state.disks.values():
                    for delivery in disk.deliveries.values():
                        statuses[delivery.status] = statuses.get(delivery.status, 0) + 1
            print(
                json.dumps(
                    {
                        "deliveryEnabled": args.send and config.enabled,
                        "recordedDeliveryStatuses": statuses,
                        "samples": [s.model_dump() for s in samples],
                    },
                    allow_nan=False,
                ),
                flush=True,
            )
            if not args.loop:
                break
            await asyncio.sleep(config.interval_seconds)
    finally:
        await sender.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--loop", action="store_true")
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
                raise ValueError("Private lock required")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(execute(args))
    except Exception:
        # Validation, file and transport diagnostics can contain private addresses or paths.
        print(json.dumps({"error": "DISK_ALERT_CHECK_FAILED"}), flush=True)
        raise SystemExit(1) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    main()
