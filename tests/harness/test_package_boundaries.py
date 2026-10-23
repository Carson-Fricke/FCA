"""Parsed manifests protect dependency boundaries behavior tests cannot prove."""

import tomllib
from pathlib import Path


def test_harness_policy_and_program_adaptation_have_no_host_dependencies() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "harness"
    forbidden = {
        "trading": {
            "backtest",
            "live",
            "api",
            "mcp",
            "harness-lakeclient",
            "tokio",
            "reqwest",
        },
        "execution": {
            "api",
            "mcp",
            "lake-storage",
            "axum",
            "azure_core",
            "azure_identity",
        },
        "backtest": {
            "live",
            "api",
            "mcp",
            "harness-lakeclient",
            "harness-strategy-jit",
        },
        "live": {"backtest"},
        "strategy-jit": {
            "backtest",
            "harness-research",
            "harness-lakeclient",
            "api",
            "mcp",
        },
    }
    for crate, prohibited in forbidden.items():
        manifest = root / crate / "Cargo.toml"
        assert manifest.is_file(), f"Required capability missing: {crate}"
        contract = tomllib.loads(manifest.read_text(encoding="utf-8"))
        declarations = dict(contract.get("dependencies", {}))
        for target in contract.get("target", {}).values():
            declarations.update(target.get("dependencies", {}))
        dependencies = {
            declaration.get("package", alias)
            if isinstance(declaration, dict)
            else alias
            for alias, declaration in declarations.items()
        }
        assert not dependencies & prohibited, (crate, dependencies & prohibited)
