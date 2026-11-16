"""Validate a private replacement before an operator atomically activates it."""

import argparse
import json
from pathlib import Path

from src.app.core.config import get_settings
from src.app.core.height_reference_file import ReferenceFileError, read_reference_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = read_reference_file(args.file, get_settings())
    except ReferenceFileError:
        print(json.dumps({"valid": False, "reason": "HEIGHT_REFERENCE_FILE_INVALID"}))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "heightReferences": len(document.height_references),
                "simulationHeightReferences": len(document.simulation_height_references),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
