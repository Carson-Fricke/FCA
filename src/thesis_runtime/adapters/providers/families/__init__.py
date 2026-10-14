"""Select one pure tool adapter from the configured model's family namespace."""

from collections.abc import Callable, Mapping

from thesis_runtime.adapters.providers.families.base import (
    ModelFamilyAdapter,
    NativeToolsAdapter,
)
from thesis_runtime.adapters.providers.families.deepseek import DeepSeekAdapter

_FAMILIES: Mapping[str, Callable[[], ModelFamilyAdapter]] = {
    "deepseek": DeepSeekAdapter,
}


def model_family_adapter(model: str) -> ModelFamilyAdapter:
    """Resolve once in composition; unknown families retain shared native behavior."""
    namespace = model.partition("/")[0].lower()
    return _FAMILIES.get(namespace, NativeToolsAdapter)()


__all__ = ["DeepSeekAdapter", "ModelFamilyAdapter", "model_family_adapter"]
