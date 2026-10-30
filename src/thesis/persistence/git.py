"""Process-safe, compare-and-swap fenced Git publication."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from thesis.persistence.journal import JournalEntry, content_hash
from thesis_runtime.storage.ownership import assert_owned

_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _git_blob_oid(payload: bytes) -> str:
    """Return the Git object id a regular blob payload hashes to."""
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


class GitPublicationError(RuntimeError):
    """Base class for safe Git publication failures."""


class GitBusyError(GitPublicationError):
    """Publication did not start before the bounded lock deadline."""

    execution_certainty = "not_started"

    @property
    def failure(self) -> Mapping[str, object]:
        return {
            "kind": "publication_busy",
            "message": str(self),
            "retry_safe": True,
            "retryGuidance": "Retry the same operation; publication did not start.",
        }


class GitFenceError(GitPublicationError):
    """Repository HEAD moved outside the publication coordinator's fence."""


class GitPublicationConflict(GitPublicationError):
    """Repository history contains a conflicting use of a publication ID."""


class GitMaterializationError(GitPublicationError):
    """The commit exists but its accepted files could not be materialized."""

    def __init__(self, commit_hash: str) -> None:
        super().__init__("program Git materialization failed")
        self.commit_hash = commit_hash


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
        artifacts: Mapping[str, bytes] | None = None,
    ) -> GitReceipt:
        """Commit an entry iff repository HEAD still matches ``expected_head``.

        Only the configured manifest and accepted artifact bytes are staged. Unrelated
        index changes remain untouched and cannot leak into the publication.
        """
        if expected_head is not None:
            _validate_object_id(expected_head)
        manifest_bytes = entry.manifest_bytes()
        accepted = self._accepted_artifacts(entry, artifacts)
        with self._publication_lock():
            self._check_destinations(accepted)
            recovered = self.find_publication(entry, artifacts=artifacts)
            if recovered is not None:
                self._materialize(manifest_bytes, accepted)
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
            if artifacts is not None:
                message += "\nThesis-Artifacts-SHA256: " + content_hash(
                    {path: content_hash(payload) for path, payload in accepted.items()}
                )
            commit_hash = self._create_commit(
                expected_head=expected_head,
                manifest_bytes=manifest_bytes,
                message=message,
                artifacts=accepted,
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
                recovered = self.find_publication(entry, artifacts=artifacts)
                if recovered is not None:
                    return GitReceipt(
                        publication_id=entry.publication_id,
                        commit_hash=recovered,
                        head_before=self.parent(recovered),
                        recovered=True,
                    )
                raise GitFenceError(_git_error(advanced, "Git HEAD fence was rejected"))
            self._materialize(manifest_bytes, accepted)
            if not self.commit_matches(commit_hash, entry, artifacts=artifacts):
                raise GitPublicationConflict(
                    f"commit {commit_hash!r} does not match publication manifest"
                )
            return GitReceipt(
                publication_id=entry.publication_id,
                commit_hash=commit_hash,
                head_before=expected_head,
            )

    def find_publication(
        self, entry: JournalEntry, *, artifacts: Mapping[str, bytes] | None = None
    ) -> str | None:
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
            if self.commit_matches(commit_hash, entry, artifacts=artifacts)
        ]
        if len(matches) == 1 and len(commits) == 1:
            return matches[0]
        if commits:
            raise GitPublicationConflict(
                f"publication ID {entry.publication_id!r} has conflicting Git history"
            )
        return None

    def commit_matches(
        self,
        commit_hash: str,
        entry: JournalEntry,
        *,
        artifacts: Mapping[str, bytes] | None = None,
    ) -> bool:
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
        accepted = self._accepted_artifacts(entry, artifacts)
        artifact_hash = trailers.get("Thesis-Artifacts-SHA256")
        if artifact_hash is not None and (
            artifacts is None
            or artifact_hash
            != content_hash(
                {path: content_hash(payload) for path, payload in accepted.items()}
            )
        ):
            return False
        if artifacts is not None and artifact_hash is None:
            return False
        return self.manifest_matches(commit_hash, entry) and all(
            self.file_matches(commit_hash, path, payload)
            for path, payload in accepted.items()
        )

    def manifest_matches(self, commit_hash: str, entry: JournalEntry) -> bool:
        """Return whether a commit tree exposes the exact accepted manifest."""
        _validate_object_id(commit_hash)
        return self.file_matches(
            commit_hash, self.manifest_path.as_posix(), entry.manifest_bytes()
        )

    def file_matches(self, commit_hash: str, path: str, payload: bytes) -> bool:
        """Verify exact regular-file bytes in a commit without text decoding."""
        _validate_object_id(commit_hash)
        relative = _relative_path(path, "artifact path").as_posix()
        blob = self._run("hash-object", "--stdin", input_bytes=payload).stdout.strip()
        tree = self._run("ls-tree", "-z", commit_hash, "--", relative).stdout
        return tree == f"100644 blob {blob}\t{relative}\0"

    def tracked_paths(self, prefix: str) -> tuple[str, ...]:
        """Return regular-file paths in HEAD beneath a confined prefix."""
        relative = _relative_path(prefix, "tracked prefix").as_posix()
        if self.head() is None:
            return ()
        result = self._run("ls-tree", "-r", "-z", "--name-only", "HEAD", "--", relative)
        return tuple(path for path in result.stdout.split("\0") if path)

    def commit_order(self) -> tuple[str, ...]:
        """Return reachable commits oldest-first in topological, not clock order."""
        if self.head() is None:
            return ()
        return tuple(
            self._run(
                "rev-list", "--topo-order", "--reverse", "HEAD"
            ).stdout.splitlines()
        )

    def path_revisions(self, prefix: str) -> tuple[tuple[str, str], ...]:
        """Return (commit, commit time) touching a confined prefix, newest first."""
        relative = _relative_path(prefix, "tracked prefix").as_posix()
        if self.head() is None:
            return ()
        output = self._run("log", "--format=%H%x09%cI", "--", relative).stdout
        revisions: list[tuple[str, str]] = []
        for line in output.splitlines():
            commit, separator, committed_at = line.partition("\t")
            if separator and commit and committed_at:
                revisions.append((commit, committed_at))
        return tuple(revisions)

    def path_revisions_many(
        self, prefixes: Sequence[str]
    ) -> dict[str, tuple[tuple[str, str], ...]]:
        """Return per-prefix (commit, commit time) history in one ``git log`` pass."""
        if not prefixes:
            return {}
        relative = [
            _relative_path(prefix, "tracked prefix").as_posix() for prefix in prefixes
        ]
        if self.head() is None:
            return {prefix: () for prefix in relative}
        output = self._run(
            "log", "--format=%x00%H%x09%cI", "--name-only", "--", *relative
        ).stdout
        collected: dict[str, list[tuple[str, str]]] = {
            prefix: [] for prefix in relative
        }
        for chunk in output.split("\0"):
            if not chunk:
                continue
            lines = chunk.splitlines()
            commit, separator, committed_at = lines[0].partition("\t")
            if not separator or not commit or not committed_at:
                continue
            for path in lines[1:]:
                for prefix in relative:
                    if path.startswith(prefix + "/"):
                        collected[prefix].append((commit, committed_at))
        return {prefix: tuple(entries) for prefix, entries in collected.items()}

    def recover_artifacts(
        self, commit_hash: str, artifacts: Mapping[str, bytes]
    ) -> None:
        """Restore missing accepted files from a reachable commit without a journal.

        Verify every path and byte before writing under the process fence. Existing
        differing files are never replaced; unrelated index state is untouched.
        """
        _validate_object_id(commit_hash)
        accepted = {
            _relative_path(path, "artifact path").as_posix(): payload
            for path, payload in artifacts.items()
        }
        folded = sorted(path.casefold() for path in accepted)
        if len(set(folded)) != len(folded) or any(
            right.startswith(left + "/")
            for index, left in enumerate(folded)
            for right in folded[index + 1 :]
        ):
            raise GitPublicationConflict("recovery paths overlap")
        with self._publication_lock():
            if not self.is_ancestor(commit_hash) or not all(
                isinstance(payload, bytes)
                and self.file_matches(commit_hash, path, payload)
                for path, payload in accepted.items()
            ):
                raise GitPublicationConflict(
                    "recovery bytes do not match reachable commit"
                )
            self._check_destinations(accepted)
            self._write_missing_artifacts(accepted)
            if accepted:
                self._run("add", "--", *accepted)

    def read_file(self, commit_hash: str, path: str) -> bytes:
        """Read a regular UTF-8 artifact from Git, independent of the checkout."""
        _validate_object_id(commit_hash)
        relative = _relative_path(path, "tracked path").as_posix()
        result = self._run("show", f"{commit_hash}:{relative}")
        payload = result.stdout.encode("utf-8")
        if not self.file_matches(commit_hash, relative, payload):
            raise GitPublicationConflict("tracked artifact is not a regular file")
        return payload

    def read_blobs(self, commit_hash: str, paths: Sequence[str]) -> dict[str, bytes]:
        """Read several regular blobs in one verified ``cat-file`` batch.

        Every path must exist at the commit as a regular (mode ``100644``)
        blob; each payload is checked against the object id the batch resolved
        so a truncated or mismatched response can never be projected.
        """
        _validate_object_id(commit_hash)
        if not paths:
            return {}
        relative = [_relative_path(path, "tracked path").as_posix() for path in paths]
        tree = self._run("ls-tree", "-z", commit_hash, "--", *relative).stdout
        modes: dict[str, str] = {}
        for record in tree.split("\0"):
            if not record:
                continue
            meta, _, tree_path = record.partition("\t")
            parts = meta.split(" ")
            if len(parts) == 3 and parts[1] == "blob":
                modes[tree_path] = parts[0]
        for path in relative:
            if modes.get(path) != "100644":
                raise GitPublicationError(
                    f"tracked artifact is not a regular file: {path}"
                )
        request = "".join(f"{commit_hash}:{path}\n" for path in relative).encode(
            "utf-8"
        )
        output = self._run("cat-file", "--batch", input_bytes=request).stdout.encode(
            "utf-8"
        )
        blobs: dict[str, bytes] = {}
        position = 0
        for path in relative:
            header_end = output.find(b"\n", position)
            if header_end < 0:
                raise GitPublicationError("git cat-file batch response is truncated")
            header = output[position:header_end].decode("utf-8")
            parts = header.split(" ")
            if len(parts) != 3 or parts[1] != "blob":
                raise GitPublicationError(f"tracked artifact is not a blob: {path}")
            oid, _object_type, size_text = parts
            size = int(size_text)
            content_start = header_end + 1
            payload = output[content_start : content_start + size]
            if len(payload) != size or _git_blob_oid(payload) != oid:
                raise GitPublicationError(
                    "tracked artifact bytes do not match the commit"
                )
            if output[content_start + size : content_start + size + 1] != b"\n":
                raise GitPublicationError("git cat-file batch response is malformed")
            blobs[path] = payload
            position = content_start + size + 1
        return blobs

    def has_staged_changes(self, paths: Sequence[str]) -> bool:
        """Return whether any selected path has staged index changes."""
        relative = [_relative_path(path, "artifact path").as_posix() for path in paths]
        if not relative:
            return False
        result = self._run("diff", "--cached", "--name-only", "--", *relative)
        return bool(result.stdout.strip())

    def commit_exact_files(
        self,
        head: str | None,
        files: Mapping[str, bytes],
        destinations: Mapping[str, Path],
        message: str,
    ) -> str:
        """Commit these bytes from a temporary index, then materialize them.

        Unrelated staged and dirty files stay untouched. A materialization
        failure raises :class:`GitMaterializationError` after the commit exists
        so the caller can retain the known local commit for reconciliation.
        """
        accepted = {
            _relative_path(path, "artifact path").as_posix(): payload
            for path, payload in files.items()
        }
        if set(accepted) != set(destinations):
            raise GitPublicationError("commit destinations do not match files")
        if head is not None:
            _validate_object_id(head)
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(("GIT_", "GCM_"))
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "FCA Thesis",
                "GIT_AUTHOR_EMAIL": "thesis@localhost",
                "GIT_COMMITTER_NAME": "FCA Thesis",
                "GIT_COMMITTER_EMAIL": "thesis@localhost",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )

        def run(*arguments: str, payload: bytes | None = None) -> str:
            try:
                result = subprocess.run(
                    ("git", *arguments),
                    cwd=self.repository,
                    env=environment,
                    input=payload,
                    stdin=subprocess.DEVNULL if payload is None else None,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise GitPublicationError(
                    "program Git publication outcome is unknown"
                ) from error
            if result.returncode:
                raise GitPublicationError("program Git publication failed")
            return result.stdout.decode().strip()

        git_directory = Path(run("rev-parse", "--absolute-git-dir"))
        with tempfile.TemporaryDirectory(
            prefix="program-index-", dir=git_directory
        ) as temporary:
            environment["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
            run("read-tree", head or "--empty")
            blobs: dict[str, str] = {}
            for relative, payload in accepted.items():
                blob = run("hash-object", "-w", "--stdin", payload=payload)
                blobs[relative] = blob
                run("update-index", "--add", "--cacheinfo", "100644", blob, relative)
            tree = run("write-tree")
            commit = run(
                "commit-tree",
                tree,
                *(["-p", head] if head else []),
                "-m",
                "thesis: " + message,
            )
            run("update-ref", "HEAD", commit, head or "0" * len(commit))
            environment.pop("GIT_INDEX_FILE")
            try:
                for relative, payload in accepted.items():
                    path = destinations[relative]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, name = tempfile.mkstemp(
                        prefix=".program-", dir=path.parent
                    )
                    staged = Path(name)
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(payload)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(staged, path)
                    finally:
                        staged.unlink(missing_ok=True)
                run(
                    "update-index",
                    "--index-info",
                    payload="".join(
                        f"100644 {blobs[relative]}\t{relative}\n"
                        for relative in accepted
                    ).encode(),
                )
            except (OSError, GitPublicationError) as error:
                raise GitMaterializationError(commit) from error
        return commit

    def find_artifact_publication(
        self, publication_id: str, artifacts: Mapping[str, bytes]
    ) -> str | None:
        """Locate and verify an authored commit using tracked identifying data only."""
        if re.fullmatch(r"pub-[0-9a-f]{48}", publication_id) is None:
            raise ValueError("invalid publication ID")
        if self.head() is None:
            return None
        commits = self._run(
            "log",
            "--format=%H",
            "--fixed-strings",
            f"--grep=Thesis-Publication-Id: {publication_id}",
            "HEAD",
        ).stdout.splitlines()
        if not commits:
            return None
        if len(commits) != 1:
            raise GitPublicationConflict("publication ID has conflicting Git history")
        commit = commits[0]
        trailers = _trailers(self._run("show", "-s", "--format=%B", commit).stdout)
        expected = content_hash(
            {path: content_hash(payload) for path, payload in artifacts.items()}
        )
        if (
            trailers.get("Thesis-Publication-Id") != publication_id
            or trailers.get("Thesis-Artifacts-SHA256") != expected
            or not all(
                self.file_matches(commit, path, payload)
                for path, payload in artifacts.items()
            )
        ):
            raise GitPublicationConflict(
                "authored artifact commit does not match tracked manifest"
            )
        return commit

    def commit_time(self, commit_hash: str) -> str:
        """Return the immutable commit timestamp for clone-stable projections."""
        _validate_object_id(commit_hash)
        return self._run("show", "-s", "--format=%cI", commit_hash).stdout.strip()

    @contextmanager
    def publication_lock(self) -> Iterator[None]:
        """Hold the configured process fence for a caller-owned transaction.

        Do not nest publish on this instance; use a distinct lock name for a
        broader authoring transaction around the Git publication fence.
        """
        with self._publication_lock():
            yield

    def _accepted_artifacts(
        self, entry: JournalEntry, artifacts: Mapping[str, bytes] | None
    ) -> dict[str, bytes]:
        if artifacts is None:
            return {}
        accepted = {
            _relative_path(path, "artifact path").as_posix(): payload
            for path, payload in artifacts.items()
        }
        references = {ref.uri: ref for ref in entry.artifacts}
        if accepted.keys() != references.keys():
            raise GitPublicationConflict(
                "artifact mapping must match journal references"
            )
        paths = [self.manifest_path.as_posix(), *accepted]
        folded = sorted(path.casefold() for path in paths)
        if len(set(folded)) != len(folded) or any(
            right.startswith(left + "/")
            for index, left in enumerate(folded)
            for right in folded[index + 1 :]
        ):
            raise GitPublicationConflict("publication paths overlap")
        for path, payload in accepted.items():
            ref = references[path]
            if (
                not isinstance(payload, bytes)
                or len(payload) != ref.size_bytes
                or content_hash(payload) != ref.sha256
            ):
                raise GitPublicationConflict(
                    "artifact bytes differ from accepted journal"
                )
        return accepted

    def _check_destinations(self, artifacts: Mapping[str, bytes]) -> None:
        for relative in (self.manifest_path.as_posix(), *artifacts):
            current = self.repository
            parts = _relative_path(relative, "publication path").parts
            for position, part in enumerate(parts):
                if current.is_dir() and any(
                    child.name.casefold() == part.casefold() and child.name != part
                    for child in current.iterdir()
                ):
                    raise GitPublicationConflict(
                        "publication path has a case collision"
                    )
                current = current / part
                if current.is_symlink() or (
                    current.exists()
                    and (
                        current.is_dir()
                        if position == len(parts) - 1
                        else not current.is_dir()
                    )
                ):
                    raise GitPublicationConflict(
                        "publication path is not a confined file"
                    )
            if relative in artifacts and current.exists():
                if current.read_bytes() != artifacts[relative]:
                    raise GitPublicationConflict(
                        "immutable artifact path already differs"
                    )

    def _materialize(self, manifest: bytes, artifacts: Mapping[str, bytes]) -> None:
        self._check_destinations(artifacts)
        self._write_missing_artifacts(artifacts)
        destination = self.repository.joinpath(*self.manifest_path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(manifest)
        # Refresh only publication paths; unrelated index entries remain untouched.
        self._run("add", "--", self.manifest_path.as_posix(), *artifacts)

    def _write_missing_artifacts(self, artifacts: Mapping[str, bytes]) -> None:
        for relative, payload in artifacts.items():
            destination = self.repository / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                with destination.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())

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
                deadline = time.monotonic() + 30.0
                while True:
                    assert_owned()
                    try:
                        _lock_descriptor(descriptor)
                        locked = True
                        break
                    except OSError as contention:
                        if contention.errno not in (
                            errno.EACCES,
                            errno.EAGAIN,
                            errno.EDEADLK,
                        ):
                            raise
                        if time.monotonic() >= deadline:
                            raise GitBusyError(
                                "Workspace publication remained busy for 30 seconds; no publication started. Retry the same operation."
                            ) from contention
                        time.sleep(0.025)
            except OSError as error:
                raise GitPublicationError(
                    f"Unable to acquire the workspace publication lock: {error}"
                ) from error
            assert_owned()
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
        artifacts: Mapping[str, bytes],
    ) -> str:
        with self._temporary_index() as environment:
            if expected_head is None:
                self._run("read-tree", "--empty", environment=environment)
            else:
                self._run("read-tree", expected_head, environment=environment)
            for path, payload in {
                self.manifest_path.as_posix(): manifest_bytes,
                **artifacts,
            }.items():
                blob_hash = self._run(
                    "hash-object", "-w", "--stdin", input_bytes=payload
                ).stdout.strip()
                _validate_object_id(blob_hash)
                self._run(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob_hash,
                    path,
                    environment=environment,
                )
            tree_hash = self._run("write-tree", environment=environment).stdout.strip()
            _validate_object_id(tree_hash)
            arguments = ["commit-tree", tree_hash]
            if expected_head is not None:
                arguments.extend(("-p", expected_head))
            arguments.extend(("-m", message))
            commit_hash = self._run(*arguments, environment=environment).stdout.strip()
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
        environment = _git_environment()
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
                env=_git_environment() if environment is None else environment,
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


def _git_environment() -> dict[str, str]:
    """Isolate repository selection, Git configuration, credentials and identity."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("GIT_", "GCM_"))
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "FCA Thesis",
            "GIT_AUTHOR_EMAIL": "thesis@localhost",
            "GIT_COMMITTER_NAME": "FCA Thesis",
            "GIT_COMMITTER_EMAIL": "thesis@localhost",
            "GIT_LITERAL_PATHSPECS": "1",
        }
    )
    return environment


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
        or any(
            part in {"", ".", ".."}
            or part.casefold() == ".git"
            or ":" in part
            or any(ord(character) < 32 for character in part)
            for part in value.split("/")
        )
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
