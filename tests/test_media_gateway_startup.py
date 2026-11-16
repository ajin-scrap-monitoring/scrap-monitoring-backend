"""Unsafe gateway settings must stop before rendering or starting Nginx."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "mode,token,code",
    [
        ("bearer", "", "MEDIA_GATEWAY_INVALID_BEARER_CONFIGURATION"),
        ("bearer", 'synthetic-secret"', "MEDIA_GATEWAY_INVALID_BEARER_CONFIGURATION"),
        ("bearer", "synthetic-secret\n", "MEDIA_GATEWAY_INVALID_BEARER_CONFIGURATION"),
        ("bearer", "synthetic=secret", "MEDIA_GATEWAY_INVALID_BEARER_CONFIGURATION"),
        ("none", "synthetic-secret", "MEDIA_GATEWAY_INVALID_AUTH_MODE"),
    ],
)
def test_gateway_rejects_invalid_auth_configuration_without_echoing_token(mode, token, code):
    script = Path(__file__).resolve().parents[1] / "deploy/media-gateway-entrypoint.sh"
    result = subprocess.run(
        ["/bin/sh", str(script)],
        env={"PATH": os.defpath, "MEDIA_AUTH_MODE": mode, "MEDIA_API_TOKEN": token},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == code
    assert "synthetic-secret" not in result.stderr
