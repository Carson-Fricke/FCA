from importlib import import_module
from pathlib import Path

import harness

CAPABILITIES = (
    "configuration",
    "core",
    "data",
    "indicators",
    "screening",
    "strategy",
    "testing",
)
IMPLEMENTED_CAPABILITIES = set(CAPABILITIES)


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


def test_no_sourceless_engine_caches_remain() -> None:
    """Deleted engine modules must not leave source-less ``__pycache__`` trees.

    After the Rust entrypoint flip, the Python engine sources were deleted, but
    compiled-only ``__pycache__`` directories can linger and silently satisfy
    namespace-package imports. Every harness cache must still have Python source
    beside it; Rust and build output trees are out of scope.
    """
    harness_root = Path(__file__).parents[2] / "src" / "harness"
    build_dirs = frozenset({"rust", "target"})

    stale = sorted(
        str(cache.parent)
        for cache in harness_root.rglob("__pycache__")
        if not build_dirs.intersection(cache.parts)
        and not any(cache.parent.glob("*.py"))
    )

    assert stale == [], f"source-less harness caches remain: {stale}"
