"""Smoke-test an isolated image and retain installed third-party notices."""

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4

AUDIT = r"""
import importlib.metadata as metadata,json
from pathlib import Path
packages=[];notices=[]
for dist in sorted(metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
 name=dist.metadata['Name'];version=dist.version
 license_value=dist.metadata.get('License-Expression') or dist.metadata.get('License')
 classifiers=[x for x in dist.metadata.get_all('Classifier',[]) if x.startswith('License ::')]
 packages.append({'name':name,'version':version,'license':license_value,'classifiers':classifiers})
 for item in dist.files or []:
  label=str(item)
  if '.dist-info/' in label and any(x in label.lower() for x in ['license','notice','copying']):
   path=dist.locate_file(item)
   if path.is_file():
    notices.append({'package':name,'path':label,'text':path.read_text(errors='replace')})
for path in sorted(Path('/usr/share/doc').glob('*/copyright')):
 if path.is_file():
  notices.append({
   'package':'base-image','path':str(path),'text':path.read_text(errors='replace')
  })
print(json.dumps({'packages':packages,'notices':notices}))
"""

PROBE = r"""
import json,os,urllib.request,urllib.error
for path in ['/healthz','/api/v1/readyz']:
 with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=5) as r:assert r.status==200
try:urllib.request.urlopen('http://127.0.0.1:8000/api/v2/monitoring/profiles',timeout=5)
except urllib.error.HTTPError as e:assert e.code==401
else:raise AssertionError('Authentication must be required')
request=urllib.request.Request('http://127.0.0.1:8000/api/v1/session',data=json.dumps({'username':'admin','password':os.environ['ADMIN_PASSWORD']}).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=5) as r:assert r.status==200
print('PASS')
"""


def docker(*args: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, env=environment, timeout=180, check=False
    )
    if result.returncode:
        raise RuntimeError("Image verification command failed; output withheld")
    return result.stdout


def verify(image: str, revision: str, version: str, audit: Path) -> None:
    info = json.loads(docker("image", "inspect", image))[0]
    labels = info["Config"]["Labels"]
    assert info["Architecture"] == "amd64"
    assert labels["org.opencontainers.image.revision"] == revision
    assert labels["org.opencontainers.image.version"] == version
    assert info["Config"]["User"] not in ("", "0", "root")
    name = "backend-release-check-" + uuid4().hex[:12]
    environment = {**os.environ, "ADMIN_PASSWORD": secrets.token_urlsafe(24)}
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,mode=1777",
            "--env",
            "APP_ENV=test",
            "--env",
            "DATABASE_URL=sqlite+aiosqlite:////tmp/release-check.db",
            "--env",
            "GMAIL_EMAIL_ENABLED=false",
            "--env",
            "ADMIN_PASSWORD",
            image,
            environment=environment,
        )
        for _ in range(30):
            state = json.loads(docker("inspect", name))[0]
            if state["State"].get("Health", {}).get("Status") == "healthy":
                break
            if not state["State"]["Running"]:
                raise RuntimeError("Release image stopped during isolated startup")
            time.sleep(2)
        else:
            raise RuntimeError("Release image health check timed out")
        assert docker("exec", name, "/app/.venv/bin/python", "-c", PROBE).strip() == "PASS"
        inventory = json.loads(docker("exec", name, "/app/.venv/bin/python", "-c", AUDIT))
        # Runtime wheels retain their own license/notice files; publish the same notices.
        assert inventory["notices"] and inventory["packages"]
        assert all(
            item["license"]
            or item["classifiers"]
            or any(n["package"] == item["name"] for n in inventory["notices"])
            for item in inventory["packages"]
            if item["name"] != "scrap-monitoring-backend"
        )
        audit.write_text(json.dumps(inventory, indent=2) + "\n")
    finally:
        docker("rm", "-f", "-v", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("revision")
    parser.add_argument("version")
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    verify(args.image, args.revision, args.version, args.audit)
    print("Release image startup/auth/readiness and notices PASS; isolated container removed")


if __name__ == "__main__":
    main()
