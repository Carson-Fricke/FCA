"""Semantic embedding providers for knowledge search.

Thesis knowledge search uses ``Xenova/all-MiniLM-L6-v2`` through
``onnxruntime`` with mean pooling, L2 normalization, and 384 dimensions
to rank results with a real sentence encoder.

The model is downloaded once from the Hugging Face hub and cached locally.
Loading is lazy: nothing
touches the network until the first ``embed`` call, and :class:`FallbackEmbedder`
degrades to the dependency-free lexical baseline if the model or runtime cannot
load, so a knowledge search never hard-fails on an unavailable model.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from threading import Lock, RLock
from typing import Protocol, cast

from thesis.knowledge.index import EmbeddingPort, embedding_identity
from thesis.knowledge.search import HashingEmbedder

#: The sentence encoder used for semantic knowledge search.
MODEL_ID = "Xenova/all-MiniLM-L6-v2"
#: The sentence encoder's output width.
MODEL_DIMENSIONS = 384

MODEL_REVISION = "751bff37182d3f1213fa05d7196b954e230abad9"
_MODEL_BASE_URL = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}"
# Hugging Face /api/models/Xenova/all-MiniLM-L6-v2/revision/{MODEL_REVISION}?blobs=true.
# LFS artifacts use SHA-256; small files use the Git blob SHA-1 (header included).
_ARTIFACT_DIGESTS = {
    "onnx/model.onnx": "759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e",
    "onnx/model_bnb4.onnx": "106c4807d85ceaf3590d9da9c07b3a22ded29bcc0d47333da0b1b741410eb128",
    "onnx/model_fp16.onnx": "2cdb5e58291813b6d6e248ed69010100246821a367fa17b1b81ae9483744533d",
    "onnx/model_int8.onnx": "afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1",
    "onnx/model_q4.onnx": "ac0f989345605769651a18bb796fda6235d921a37bd4ac065cb51781da44d079",
    "onnx/model_q4f16.onnx": "eb08a666c46109637e0b6cb04f6052a68efd59bb0252d4e0438d28fb6b2d853d",
    "onnx/model_quantized.onnx": "afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1",
    "onnx/model_uint8.onnx": "57097fb267dcffb2a9f12fba945201c76ae99b7640ed1c6389cc9e0341c71ee6",
    "tokenizer.json": "c17ed520ed8438736732a54957a69306b8822215",
    "tokenizer_config.json": "37fca74771bc76a8e01178ce3a6055a0995f8093",
}
_LOAD_LOCK = RLock()
_DEFAULT_ONNX_FILE = "onnx/model_quantized.onnx"
_DEFAULT_MAX_LENGTH = 512
_CACHE_LIMIT = 8192


class EmbeddingModelError(RuntimeError):
    """Raised when the semantic embedding model or runtime cannot be loaded."""


class _InferenceSession(Protocol):
    """The minimal onnxruntime session surface this module uses."""

    def run(
        self, output_names: object, input_feed: dict[str, object]
    ) -> list[object]: ...

    def get_inputs(self) -> list[object]: ...

    def get_outputs(self) -> list[object]: ...


class _Encoding(Protocol):
    """One tokenized sequence from the tokenizers library."""

    ids: list[int]
    attention_mask: list[int]
    type_ids: list[int]


class _Tokenizer(Protocol):
    """The minimal tokenizers surface this module uses."""

    def enable_truncation(self, max_length: int) -> None: ...

    def encode(self, sequence: str) -> _Encoding: ...


def _mean_pool_normalize(hidden: object, mask: object) -> list[float]:
    """Mean-pool token vectors under the attention mask and L2-normalize."""
    import numpy as np

    vectors = np.asarray(hidden, dtype=np.float64)
    if vectors.ndim == 3:
        vectors = vectors[0]
    weights = np.asarray(mask, dtype=np.float64).reshape(-1, 1)
    if weights.shape[0] != vectors.shape[0]:
        weights = np.ones((vectors.shape[0], 1), dtype=np.float64)
    total = float(weights.sum())
    summed = (vectors * weights).sum(axis=0)
    pooled = summed / total if total else summed
    norm = float(np.linalg.norm(pooled))
    if norm == 0.0:
        return [0.0] * len(pooled)
    return [float(value) for value in (pooled / norm).tolist()]


def _default_cache_dir() -> Path:
    configured = (os.environ.get("THESIS_EMBEDDING_DIR") or "").strip()
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "thesis" / "embeddings"


def _verified(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    if len(digest) == 64:
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
    else:
        hasher = hashlib.sha1()  # noqa: S324 - Hugging Face Git blob identity
        hasher.update(f"blob {path.stat().st_size}\0".encode("ascii"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
        actual = hasher.hexdigest()
    return actual == digest


def _http_download(url: str, destination: Path, digest: str) -> None:
    import httpx

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        if not _verified(partial, digest):
            raise EmbeddingModelError(f"downloaded model file {url} failed checksum")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def _ensure_file(directory: Path, relative: str) -> Path:
    digest = _ARTIFACT_DIGESTS.get(relative)
    if digest is None:
        raise EmbeddingModelError(f"unsupported pinned model artifact: {relative}")
    path = directory / relative
    with _LOAD_LOCK:
        if _verified(path, digest):
            return path
        url = f"{_MODEL_BASE_URL}/{relative}"
        try:
            _http_download(url, path, digest)
            if not _verified(path, digest):
                raise EmbeddingModelError("model artifact failed checksum")
        except Exception as error:  # noqa: BLE001 - model failures trigger fallback
            raise EmbeddingModelError(f"could not download {url}: {error}") from error
    return path


def _max_length_from(config_path: Path | None) -> int | None:
    if config_path is None:
        return None
    import json

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("model_max_length")
    if isinstance(value, int) and 1 <= value <= _DEFAULT_MAX_LENGTH:
        return value
    return None


class MiniLmEmbedder:
    """The MiniLM sentence encoder, run locally through onnxruntime."""

    def __init__(
        self,
        *,
        session: _InferenceSession,
        tokenizer: _Tokenizer,
        model_id: str = MODEL_ID,
        max_length: int = _DEFAULT_MAX_LENGTH,
        artifact: str = _DEFAULT_ONNX_FILE,
    ) -> None:
        if max_length < 1:
            raise EmbeddingModelError("max_length must be positive")
        self._session = session
        self._tokenizer = tokenizer
        self._model_id = model_id
        self._max_length = max_length
        self._artifact = artifact
        self._lock = Lock()
        self._cache: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()
        tokenizer.enable_truncation(max_length=max_length)

    @property
    def model_id(self) -> str:
        """The embedding model identity recorded with search results."""
        return self._model_id

    @property
    def dimensions(self) -> int:
        """The fixed vector width."""
        return MODEL_DIMENSIONS

    @property
    def cache_identity(self) -> str:
        return f"{self.model_id}@{MODEL_REVISION}:{self._artifact}:{self._max_length}"

    @classmethod
    def load(
        cls,
        *,
        model_dir: Path | None = None,
        onnx_file: str | None = None,
        max_length: int | None = None,
    ) -> MiniLmEmbedder:
        """Download if needed, then load the ONNX session and tokenizer."""
        with _LOAD_LOCK:
            return cls._load(
                model_dir=model_dir, onnx_file=onnx_file, max_length=max_length
            )

    @classmethod
    def _load(
        cls,
        *,
        model_dir: Path | None,
        onnx_file: str | None,
        max_length: int | None,
    ) -> MiniLmEmbedder:
        directory = (model_dir if model_dir is not None else _default_cache_dir()) / (
            MODEL_REVISION
        )
        directory.mkdir(parents=True, exist_ok=True)
        onnx_relative = (
            onnx_file
            or (
                os.environ.get("THESIS_EMBEDDING_ONNX_FILE") or _DEFAULT_ONNX_FILE
            ).strip()
        )
        onnx_path = _ensure_file(directory, onnx_relative)
        tokenizer_path = _ensure_file(directory, "tokenizer.json")
        config_path = _ensure_file(directory, "tokenizer_config.json")
        resolved_max_length = (
            max_length or _max_length_from(config_path) or _DEFAULT_MAX_LENGTH
        )
        try:
            ort = importlib.import_module("onnxruntime")
            tokenizers = importlib.import_module("tokenizers")
            session = cast(
                _InferenceSession,
                ort.InferenceSession(
                    str(onnx_path), providers=["CPUExecutionProvider"]
                ),
            )
            tokenizer = cast(
                _Tokenizer,
                tokenizers.Tokenizer.from_file(str(tokenizer_path)),
            )
        except (ImportError, OSError, ValueError) as error:
            raise EmbeddingModelError(
                "onnxruntime and tokenizers are required for semantic knowledge "
                f"search: {error}"
            ) from error
        return cls(
            session=session,
            tokenizer=tokenizer,
            max_length=resolved_max_length,
            artifact=onnx_relative,
        )

    def embed(self, text: str) -> list[float]:
        """Return the 384-dimension normalized embedding of ``text``."""
        if not isinstance(text, str) or not text.strip():
            return [0.0] * MODEL_DIMENSIONS
        key = (self.cache_identity, hashlib.sha256(text.encode("utf-8")).hexdigest())
        # Tokenizer use and cache publication share a lock; failures publish nothing.
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return list(cached)
            vector = self._infer(text)
            self._cache[key] = tuple(vector)
            if len(self._cache) > _CACHE_LIMIT:
                self._cache.popitem(last=False)
            return list(vector)

    def _infer(self, text: str) -> list[float]:
        import numpy as np

        encoding = self._tokenizer.encode(text)
        ids = [int(value) for value in encoding.ids]
        mask = [int(value) for value in encoding.attention_mask]
        types = [int(value) for value in encoding.type_ids]
        names = {getattr(item, "name", "") for item in self._session.get_inputs()}
        feeds: dict[str, object] = {}
        if "input_ids" in names:
            feeds["input_ids"] = np.array([ids], dtype=np.int64)
        if "attention_mask" in names:
            feeds["attention_mask"] = np.array([mask], dtype=np.int64)
        if "token_type_ids" in names:
            feeds["token_type_ids"] = np.array([types], dtype=np.int64)
        outputs = self._session.run(None, feeds)
        return _mean_pool_normalize(self._select_hidden(outputs), mask)

    def _select_hidden(self, outputs: list[object]) -> object:
        if not outputs:
            raise EmbeddingModelError("the embedding model returned no outputs")
        names = [getattr(item, "name", "") for item in self._session.get_outputs()]
        if "last_hidden_state" in names:
            return outputs[names.index("last_hidden_state")]
        return outputs[0]


class _LazyMiniLmEmbedder:
    """Defer model download and session load until the first embed call."""

    def __init__(self) -> None:
        self._inner: MiniLmEmbedder | None = None
        self._error: Exception | None = None
        self._lock = Lock()
        self._artifact = (
            os.environ.get("THESIS_EMBEDDING_ONNX_FILE") or _DEFAULT_ONNX_FILE
        ).strip()

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def dimensions(self) -> int:
        return MODEL_DIMENSIONS

    @property
    def cache_identity(self) -> str:
        with self._lock:
            if self._inner is not None:
                return self._inner.cache_identity
        return f"{MODEL_ID}@{MODEL_REVISION}:{self._artifact}:{_DEFAULT_MAX_LENGTH}"

    def embed(self, text: str) -> list[float]:
        with self._lock:
            if self._error is not None:
                raise EmbeddingModelError("semantic model initialization failed") from (
                    self._error
                )
            if self._inner is None:
                try:
                    self._inner = MiniLmEmbedder.load(
                        onnx_file=self._artifact, max_length=_DEFAULT_MAX_LENGTH
                    )
                except Exception as error:
                    self._error = error
                    raise
            inner = self._inner
        return inner.embed(text)


class _PrimaryEmbeddingError(EmbeddingModelError):
    """Distinguish inference failure from errors in the operation itself."""


class _BoundPrimary:
    """An operation's primary backend never switches vector spaces."""

    def __init__(self, backend: EmbeddingPort) -> None:
        self._backend = backend

    @property
    def model_id(self) -> str:
        return str(getattr(self._backend, "model_id", "injected-embedder"))

    @property
    def cache_identity(self) -> str:
        return embedding_identity(self._backend)

    def embed(self, text: str) -> list[float]:
        try:
            return self._backend.embed(text)
        except Exception as error:
            raise _PrimaryEmbeddingError(str(error)) from error


class FallbackEmbedder:
    """Use the semantic model until it fails, then degrade to lexical hashing."""

    def __init__(self, primary: EmbeddingPort, fallback: EmbeddingPort) -> None:
        self._primary = primary
        self._fallback = fallback
        self._active: EmbeddingPort = primary
        self._lock = Lock()

    @property
    def model_id(self) -> str:
        """Identity of the backend currently serving embeddings."""
        with self._lock:
            return str(getattr(self._active, "model_id", "injected-embedder"))

    @property
    def using_fallback(self) -> bool:
        """Whether the lexical baseline has replaced the semantic model."""
        with self._lock:
            return self._active is self._fallback

    def embed(self, text: str) -> list[float]:
        return self.run_operation(lambda backend: backend.embed(text))

    def run_operation[T](self, operation: Callable[[EmbeddingPort], T]) -> T:
        """Retry all work on fallback after an inference failure, exactly once."""
        with self._lock:
            active = self._active
        if active is self._fallback:
            return operation(active)
        try:
            return operation(_BoundPrimary(active))
        except _PrimaryEmbeddingError as error:
            with self._lock:
                changed = self._active is not self._fallback
                self._active = self._fallback
            if changed:
                sys.stderr.write(
                    "knowledge semantic embeddings unavailable; falling back to the "
                    f"lexical baseline: {error}\n"
                )
            return operation(self._fallback)


def default_embedder() -> EmbeddingPort:
    """Return the serving embedder: MiniLM with a lexical fallback.

    ``THESIS_EMBEDDING_DISABLE=1`` forces the lexical baseline.  Otherwise the
    model loads on first use and degrades automatically if it cannot.
    """
    if (os.environ.get("THESIS_EMBEDDING_DISABLE") or "").strip() == "1":
        return HashingEmbedder()
    return FallbackEmbedder(_LazyMiniLmEmbedder(), HashingEmbedder())


__all__ = [
    "MODEL_DIMENSIONS",
    "MODEL_ID",
    "EmbeddingModelError",
    "FallbackEmbedder",
    "MiniLmEmbedder",
    "default_embedder",
]
