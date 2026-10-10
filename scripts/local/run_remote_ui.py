"""Run the loopback UI proxy against authenticated Azure Thesis backends."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def azure_json(*arguments: str) -> object:
    """Capture Azure responses in process memory, including secret responses."""
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required; sign in with az login first")
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Azure lookup failed; check az login and resource permissions"
        )
    return json.loads(result.stdout)


def main() -> int:
    """Resolve endpoint and bearer privately, then start a loopback-only proxy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-group", default="fca-rg-westus2")
    parser.add_argument("--app", default="fca-ml-dev-thesis-api")
    parser.add_argument("--vault", default="fca-ml-dev-kv-obj3kww")
    parser.add_argument("--port", type=int, default=5174)
    arguments = parser.parse_args()
    endpoint = azure_json(
        "containerapp",
        "show",
        "--resource-group",
        arguments.resource_group,
        "--name",
        arguments.app,
        "--query",
        "properties.configuration.ingress.fqdn",
    )
    token = azure_json(
        "keyvault",
        "secret",
        "show",
        "--vault-name",
        arguments.vault,
        "--name",
        "thesis-api-bearer-token",
        "--query",
        "value",
    )
    if not isinstance(endpoint, str) or not endpoint or ".internal." in endpoint:
        raise RuntimeError("Thesis requires authenticated external HTTPS ingress")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Thesis API bearer is missing from Key Vault")
    environment = os.environ.copy()
    # Never expose an inherited local development bearer through Vite's PUBLIC_ prefix.
    environment.pop("PUBLIC_THESIS_API_BEARER_TOKEN", None)
    environment.update(
        {
            "THESIS_API_PROXY_TARGET": f"https://{endpoint}",
            "THESIS_API__BEARER_TOKEN": token,
            "PUBLIC_THESIS_API_BASE": "/api",
            "PUBLIC_THESIS_UI_FIXTURES": "false",
        }
    )
    executable = shutil.which("npm")
    if executable is None:
        raise RuntimeError("Node.js and npm are required; run npm ci in src/ui first")
    print(f"Remote Thesis UI: http://127.0.0.1:{arguments.port}", flush=True)
    print(
        "Azure backends; credentials remain in the loopback proxy process.", flush=True
    )
    return subprocess.call(
        [
            executable,
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(arguments.port),
            "--strictPort",
        ],
        cwd=Path(__file__).resolve().parents[2] / "src" / "ui",
        env=environment,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
