"""Validate release identity and prepare only explicitly public release assets."""

import argparse
import hashlib
import json
import posixpath
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
IMAGE = re.compile(r"ghcr\.io/ajin-scrap-monitoring/scrap-monitoring-backend@sha256:[0-9a-f]{64}")
PUBLIC_ASSETS = (".env.example", "docs/CONFIGURATION.md", "docs/RELEASING.md")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def identity(root: Path, tag: str) -> dict[str, str]:
    if not VERSION.fullmatch(tag):
        raise ValueError("Release tag must match vMAJOR.MINOR.PATCH without leading zeroes")
    version = tag[1:]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if project["version"] != version:
        raise ValueError("Release tag must match project.version")
    commit = git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if git(root, "rev-parse", "HEAD") != commit:
        raise ValueError("Checked-out source must match the release tag")
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("Release commit must be in origin/main history")
    return {"version": version, "commit": commit}


def prepare_assets(root: Path, destination: Path, reference: str, audit: Path) -> None:
    if not IMAGE.fullmatch(reference):
        raise ValueError("Expected this repository's GHCR image with a sha256 digest")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Release asset directory must be empty")
    # Read only fixed tracked public inputs, never arbitrary env/config paths or globs.
    project_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not VERSION.fullmatch("v" + project_version):
        raise ValueError("Project version must be a supported release version")
    documents: dict[str, bytes] = {}
    for name in PUBLIC_ASSETS:
        content = (root / name).read_text()
        if name.endswith(".md"):
            # Release assets are downloaded flat; pin their relative doc links to this tag.
            def link(match: re.Match[str], source_name: str = name) -> str:
                target = match.group(1)
                if ":" in target or target.startswith(("#", "/")):
                    return match.group(0)
                relative = posixpath.normpath(str(Path(source_name).parent / target))
                return (
                    "](https://github.com/ajin-scrap-monitoring/scrap-monitoring-backend"
                    f"/blob/v{project_version}/{relative})"
                )

            content = re.sub(r"\]\(([^\s)]+)\)", link, content)
        # GitHub rewrites leading-dot upload names; keep downloadable names/checksums stable.
        asset_name = "env.example" if name == ".env.example" else Path(name).name
        documents[asset_name] = content.encode()
    notices = json.loads(audit.read_text())
    if not notices.get("packages") or not notices.get("notices"):
        raise ValueError("Runtime dependency inventory and notices are required")
    from src.app.core.config import Settings

    schema = Settings.model_json_schema()
    for name, field in Settings.model_fields.items():
        if field.default_factory is list:
            schema["properties"][name]["default"] = []
    documents.update(
        {
            "settings.schema.json": (
                json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
            ).encode(),
            "image-reference.txt": (reference + "\n").encode(),
            "runtime-dependencies.json": (json.dumps(notices, indent=2) + "\n").encode(),
        }
    )
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in documents.items():
        (destination / name).write_bytes(content)
    checksums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(documents.items())
    )
    (destination / "SHA256SUMS").write_text(checksums)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-tag")
    verify.add_argument("tag")
    verify.add_argument("--github-output", type=Path)
    assets = commands.add_parser("assets")
    assets.add_argument("destination", type=Path)
    assets.add_argument("reference")
    assets.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify-tag":
        values = identity(ROOT, args.tag)
        if args.github_output:
            with args.github_output.open("a") as output:
                for key, value in values.items():
                    output.write(f"{key}={value}\n")
        print(json.dumps(values))
    else:
        prepare_assets(ROOT, args.destination, args.reference, args.audit)


if __name__ == "__main__":
    main()
