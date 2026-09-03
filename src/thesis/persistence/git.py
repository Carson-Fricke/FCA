"""Process-safe, compare-and-swap fenced Git publication."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from thesis.persistence.journal import JournalEntry, content_hash

_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class GitPublicationError(RuntimeError):
    """Base class for safe Git publication failures."""


class GitFenceError(GitPublicationError):
    """Repository HEAD moved outside the publication coordinator's fence."""


class GitPublicationConflict(GitPublicationError):
    """Repository history contains a conflicting use of a publication ID."""


@dataclass(frozen=True, slots=True)
class GitReceipt:
    """Result of publishing, or recovering, one immutable manifest commit."""

    publication_id: str
    commit_hash: str
    head_before: str | None
    recovered: bool = False


class GitCoordinator:
    """Serialize publication of one canonical workspace manifest to Git."""

    def __init__(
        self,
        repository: Path,
        *,
        manifest_path: str = ".thesis/publication.json",
        lock_name: str = "thesis-publication.lock",
    ) -> None:
        self.repository = repository.resolve()
        self.manifest_path = _relative_path(manifest_path, "manifest path")
        self.lock_name = _relative_path(lock_name, "lock name")
        common_dir = Path(self._run("rev-parse", "--git-common-dir").stdout.strip())
        self._git_common_dir = (
            common_dir if common_dir.is_absolute() else self.repository / common_dir
        ).resolve()

    def head(self) -> str | None:
        """Return the current commit, or ``None`` for an unborn repository."""
        result = self._run("rev-parse", "--verify", "HEAD", check=False)
        if result.returncode == 0:
            head = result.stdout.strip()
            _validate_object_id(head)
            return head
        return None

    def publish(
        self,
        entry: JournalEntry,
        *,
        expected_head: str | None,
    ) -> GitReceipt:
        """Commit an entry iff repository HEAD still matches ``expected_head``.

        Only the configured manifest is staged. Existing unrelated worktree and
        index changes remain untouched and cannot leak into the publication.
        """
        if expected_head is not None:
            _validate_object_id(expected_head)
        manifest_bytes = entry.manifest_bytes()
        with self._publication_lock():
            recovered = self.find_publication(entry)
            if recovered is not None:
                return GitReceipt(
                    publication_id=entry.publication_id,
                    commit_hash=recovered,
                    head_before=self.parent(recovered),
                    recovered=True,
                )
            actual_head = self.head()
            if actual_head != expected_head:
                raise GitFenceError(
                    f"Git HEAD moved from {expected_head or '<unborn>'} "
                    f"to {actual_head or '<unborn>'}"
                )

            message = self._commit_message(entry, content_hash(manifest_bytes))
            commit_hash = self._create_commit(
                expected_head=expected_head,
                manifest_bytes=manifest_bytes,
                message=message,
            )
            old_value = (
                self._zero_object_id() if expected_head is None else expected_head
            )
            advanced = self._run(
                "update-ref",
                "HEAD",
                commit_hash,
                old_value,
                check=False,
            )
            if advanced.returncode != 0:
                recovered = self.find_publication(entry)
                if recovered is not None:
                    return GitReceipt(
                        publication_id=entry.publication_id,
                        commit_hash=recovered,
                        head_before=self.parent(recovered),
                        recovered=True,
                    )
                raise GitFenceError(_git_error(advanced, "Git HEAD fence was rejected"))
            destination = self.repository.joinpath(*self.manifest_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(manifest_bytes)
            self._run("add", "--", self.manifest_path.as_posix())
            if not self.commit_matches(commit_hash, entry):
                raise GitPublicationConflict(
                    f"commit {commit_hash!r} does not match publication manifest"
                )
            return GitReceipt(
                publication_id=entry.publication_id,
                commit_hash=commit_hash,
                head_before=expected_head,
            )

    def find_publication(self, entry: JournalEntry) -> str | None:
        """Find the unique reachable commit carrying this exact publication."""
        if self.head() is None:
            return None
        marker = f"Thesis-Publication-Id: {entry.publication_id}"
        result = self._run(
            "log",
            "--format=%H",
            "--fixed-strings",
            f"--grep={marker}",
            "HEAD",
        )
        commits = [line for line in result.stdout.splitlines() if line]
        for commit_hash in commits:
            _validate_object_id(commit_hash)
        matches = [
            commit_hash
            for commit_hash in commits
            if self.commit_matches(commit_hash, entry)
        ]
        if len(matches) == 1 and len(commits) == 1:
            return matches[0]
        if commits:
            raise GitPublicationConflict(
                f"publication ID {entry.publication_id!r} has conflicting Git history"
            )
        return None

    def commit_matches(self, commit_hash: str, entry: JournalEntry) -> bool:
        """Return whether a commit records the exact journal publication."""
        _validate_object_id(commit_hash)
        message = self._run("show", "-s", "--format=%B", commit_hash).stdout
        trailers = _trailers(message)
        expected_manifest_hash = content_hash(entry.manifest_bytes())
        if trailers.get("Thesis-Publication-Id") != entry.publication_id:
            return False
        if trailers.get("Thesis-Idempotency-Id") != entry.idempotency_id:
            return False
        if trailers.get("Thesis-Journal-Hash") != entry.journal_hash:
            return False
        if trailers.get("Thesis-Manifest-SHA256") != expected_manifest_hash:
            return False
        return self.manifest_matches(commit_hash, entry)

    def manifest_matches(self, commit_hash: str, entry: JournalEntry) -> bool:
        """Return whether a commit tree exposes the exact accepted manifest."""
        _validate_object_id(commit_hash)
        content = self._run(
            "show", f"{commit_hash}:{self.manifest_path.as_posix()}", check=False
        )
        return content.returncode == 0 and content.stdout.encode("utf-8") == (
            entry.manifest_bytes()
        )

    def is_ancestor(self, commit_hash: str) -> bool:
        """Return whether a commit is reachable from the current Git HEAD."""
        _validate_object_id(commit_hash)
        if self.head() is None:
            return False
        result = self._run(
            "merge-base", "--is-ancestor", commit_hash, "HEAD", check=False
        )
        if result.returncode not in {0, 1}:
            raise GitPublicationError(_git_error(result, "Git ancestry check failed"))
        return result.returncode == 0

    def parent(self, commit_hash: str) -> str | None:
        """Return a commit's first parent, or ``None`` for a root commit."""
        _validate_object_id(commit_hash)
        result = self._run("rev-parse", f"{commit_hash}^", check=False)
        if result.returncode != 0:
            return None
        parent = result.stdout.strip()
        _validate_object_id(parent)
        return parent

    @contextmanager
    def _publication_lock(self) -> Iterator[None]:
        lock = self._git_common_dir.joinpath(*self.lock_name.parts)
        lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            try:
                _lock_descriptor(descriptor)
                locked = True
            except OSError as error:
                raise GitFenceError(
                    "another Git publication holds the workspace lock"
                ) from error
            yield
        finally:
            try:
                if locked:
                    _unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)

    def _create_commit(
        self,
        *,
        expected_head: str | None,
        manifest_bytes: bytes,
        message: str,
    ) -> str:
        with self._temporary_index() as environment:
            if expected_head is None:
                self._run("read-tree", "--empty", environment=environment)
            else:
                self._run("read-tree", expected_head, environment=environment)
            blob_hash = self._run(
                "hash-object", "-w", "--stdin", input_bytes=manifest_bytes
            ).stdout.strip()
            _validate_object_id(blob_hash)
            self._run(
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob_hash,
                self.manifest_path.as_posix(),
                environment=environment,
            )
            tree_hash = self._run("write-tree", environment=environment).stdout.strip()
            _validate_object_id(tree_hash)
            arguments = ["commit-tree", tree_hash]
            if expected_head is not None:
                arguments.extend(("-p", expected_head))
            arguments.extend(("-m", message))
            commit_hash = self._run(*arguments).stdout.strip()
            _validate_object_id(commit_hash)
            return commit_hash

    @contextmanager
    def _temporary_index(self) -> Iterator[dict[str, str]]:
        descriptor, name = tempfile.mkstemp(
            prefix="thesis-index-", dir=self._git_common_dir
        )
        os.close(descriptor)
        path = Path(name)
        path.unlink()
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(path)
        try:
            yield environment
        finally:
            path.unlink(missing_ok=True)

    def _zero_object_id(self) -> str:
        object_format = self._run("rev-parse", "--show-object-format").stdout.strip()
        sizes = {"sha1": 40, "sha256": 64}
        try:
            return "0" * sizes[object_format]
        except KeyError as error:
            raise GitPublicationError(
                f"unsupported Git object format {object_format!r}"
            ) from error

    @staticmethod
    def _commit_message(entry: JournalEntry, manifest_hash: str) -> str:
        return "\n".join(
            (
                f"thesis: publish {entry.command_name}",
                "",
                f"Thesis-Publication-Id: {entry.publication_id}",
                f"Thesis-Idempotency-Id: {entry.idempotency_id}",
                f"Thesis-Journal-Hash: {entry.journal_hash}",
                f"Thesis-Manifest-SHA256: {manifest_hash}",
            )
        )

    def _run(
        self,
        *arguments: str,
        check: bool = True,
        environment: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ("git", *arguments),
                cwd=self.repository,
                capture_output=True,
                check=False,
                env=environment,
                input=input_bytes,
                stdin=subprocess.DEVNULL if input_bytes is None else None,
                timeout=30,
            )
            result = subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                completed.stdout.decode("utf-8", errors="strict"),
                completed.stderr.decode("utf-8", errors="strict"),
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
            raise GitPublicationError(f"unable to run Git: {error}") from error
        if check and result.returncode != 0:
            raise GitPublicationError(_git_error(result, "Git command failed"))
        return result


def _trailers(message: str) -> dict[str, str]:
    trailers: dict[str, str] = {}
    for line in message.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.startswith("Thesis-"):
            trailers[key] = value.strip()
    return trailers


def _relative_path(value: str, name: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{name} must be a confined relative POSIX path")
    return path


def _validate_object_id(value: str) -> None:
    if _OBJECT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Git object ID must be lowercase SHA-1 or SHA-256")


def _git_error(result: subprocess.CompletedProcess[str], prefix: str) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    return prefix if not detail else f"{prefix}: {detail}"


if os.name == "nt":
    import msvcrt

    def _lock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    def _unlock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


__all__ = [
    "GitCoordinator",
    "GitFenceError",
    "GitPublicationConflict",
    "GitPublicationError",
    "GitReceipt",
]
