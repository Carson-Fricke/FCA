from importlib import import_module
from pathlib import Path

import harness

CAPABILITIES = (
    "configuration",
    "core",
    "data",
    "fills",
    "oms",
    "portfolio",
    "results",
    "risk",
    "runtime",
    "strategy",
    "testing",
    "venues",
)
IMPLEMENTED_CAPABILITIES = {"configuration", "core", "oms"}


def test_product_and_capability_facades_import() -> None:
    modules = [import_module(f"harness.{name}") for name in CAPABILITIES]

    assert harness.__all__ == ()
    assert [module.__name__ for module in modules] == [
        f"harness.{name}" for name in CAPABILITIES
    ]
    exports = dict(
        zip(CAPABILITIES, (module.__all__ for module in modules), strict=True)
    )
    assert all(exports[name] for name in IMPLEMENTED_CAPABILITIES)
    assert all(
        exports[name] == () for name in set(CAPABILITIES) - IMPLEMENTED_CAPABILITIES
    )


def test_strategy_and_configuration_namespaces_exist() -> None:
    repository_root = Path(__file__).parents[2]

    assert (repository_root / "strategies" / "__init__.py").is_file()
    assert (repository_root / "strategies" / "harness_examples").is_dir()
    assert (repository_root / "config" / "harness").is_dir()
