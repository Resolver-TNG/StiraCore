"""Artifact layer: independent 4th memory layer for versioned generated content.

Artifacts persist as JSON files under
<base_path>/agents/<agent_id>/artifacts/<slug>/v<N>.json. The highest version
number in a slug directory is the current artifact; up to MAX_VERSIONS are kept.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

MAX_VERSIONS = 50
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class ArtifactKind(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"
    HTML = "html"
    SVG = "svg"
    TEXT = "text"


@dataclass
class Artifact:
    slug: str
    version: int
    kind: ArtifactKind
    content: str
    metadata: dict
    created_at: str
    updated_at: str


class ArtifactStore:
    """Versioned artifacts of one agent.

    #601 (C3–C7): ``agent_id`` is one safe name (never a path), a version file appears
    complete or not at all (written to a temporary file, then linked under its final name,
    which never replaces an existing version), an unreadable newest version falls back to
    the newest readable one, and an invalid ``kind`` is refused before anything is created.
    """

    def __init__(self, base_path: str, agent_id: str) -> None:
        if not isinstance(agent_id, str) or not _SLUG_RE.fullmatch(agent_id):
            raise ValueError("agent_id must be a single name ([A-Za-z0-9_-])")
        self.root = Path(base_path) / "agents" / agent_id / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def _slug_dir(self, slug: str) -> Path:
        if not _SLUG_RE.match(slug):
            raise ValueError(f"invalid slug: {slug}")
        path = self.root / slug
        if path.exists() and path.is_symlink():
            raise ValueError(f"refusing symlinked artifact dir: {slug}")
        return path

    def _versions(self, dir_: Path) -> list[int]:
        return sorted(int(p.stem[1:]) for p in dir_.glob("v*.json") if p.stem[1:].isdigit())

    def _write(self, dir_: Path, artifact: Artifact) -> None:
        """Publish ``v<N>.json`` atomically; ``FileExistsError`` if that version exists."""
        path = dir_ / f"v{artifact.version}.json"
        if path.is_symlink():
            raise ValueError("refusing symlinked artifact file")
        data = {
            "slug": artifact.slug,
            "version": artifact.version,
            "kind": ArtifactKind(artifact.kind).value,
            "content": artifact.content,
            "metadata": artifact.metadata,
            "created_at": artifact.created_at,
            "updated_at": artifact.updated_at,
        }
        text = json.dumps(data, ensure_ascii=False, indent=2)
        fd, temporary = tempfile.mkstemp(dir=dir_, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)  # never replaces an existing version
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)

    def _latest(self, dir_: Path, versions: list[int]) -> Artifact:
        """The newest version that loads (a torn/unreadable file is skipped)."""
        error: Exception | None = None
        for version in reversed(versions):
            try:
                return self._load(dir_, version)
            except (OSError, ValueError, TypeError, KeyError) as problem:
                error = error or problem
        raise error if error is not None else FileNotFoundError(dir_.name)

    def _load(self, dir_: Path, version: int) -> Artifact:
        data = json.loads((dir_ / f"v{version}.json").read_text(encoding="utf-8"))
        data["kind"] = ArtifactKind(data["kind"])
        return Artifact(**data)

    def create(self, slug: str, kind: str, content: str, metadata: dict | None = None) -> Artifact:
        dir_ = self._slug_dir(slug)
        if dir_.exists():
            raise FileExistsError(f"artifact exists: {slug}")
        now = datetime.now(UTC).isoformat()
        artifact = Artifact(slug, 1, ArtifactKind(kind), content, metadata or {}, now, now)
        json.dumps(artifact.metadata)  # unserializable metadata fails before the mkdir
        dir_.mkdir(parents=True)
        try:
            self._write(dir_, artifact)
        except BaseException:
            shutil.rmtree(dir_, ignore_errors=True)  # the directory this call created
            raise
        return artifact

    def update(self, slug: str, content: str, metadata: dict | None = None) -> Artifact:
        dir_ = self._slug_dir(slug)
        if not dir_.exists():
            raise FileNotFoundError(f"no such artifact: {slug}")
        latest = self.get(slug)
        now = datetime.now(UTC).isoformat()
        meta = metadata if metadata is not None else latest.metadata
        version = max(latest.version, *self._versions(dir_)) + 1
        while True:
            artifact = Artifact(slug, version, latest.kind, content, meta, latest.created_at, now)
            try:
                self._write(dir_, artifact)
                break
            except FileExistsError:  # another update took this number: the next one
                version += 1
        for old in self._versions(dir_)[:-MAX_VERSIONS]:
            (dir_ / f"v{old}.json").unlink(missing_ok=True)
        return artifact

    def get(self, slug: str, version: int | None = None) -> Artifact:
        dir_ = self._slug_dir(slug)
        versions = self._versions(dir_)
        if not versions:
            raise FileNotFoundError(f"no such artifact: {slug}")
        if version is not None:
            return self._load(dir_, version)
        return self._latest(dir_, versions)

    def list_artifacts(self, limit: int = 50) -> list[Artifact]:
        artifacts = []
        for dir_ in self.root.iterdir():
            if dir_.is_dir() and not dir_.is_symlink():
                versions = self._versions(dir_)
                if versions:
                    with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                        artifacts.append(self._latest(dir_, versions))
        artifacts.sort(key=lambda a: a.updated_at, reverse=True)
        return artifacts[:limit]

    def search(self, query: str) -> list[Artifact]:
        q = query.lower()
        return [
            a
            for a in self.list_artifacts(limit=10_000)
            if q in a.slug.lower() or q in a.content.lower()
        ]

    def delete(self, slug: str) -> None:
        dir_ = self._slug_dir(slug)
        if dir_.exists() and not dir_.is_symlink():
            shutil.rmtree(dir_)
