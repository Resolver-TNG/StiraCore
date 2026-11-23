"""Read-only inspection of local Git repositories (#671, stage 1).

Ported from Stira-KIRO's workbench (``backend/repos.py``) without its UI, its settings
file and its fetch button. What it reports for each repository: ahead/behind its upstream,
uncommitted changes, unpushed commits, stale worktrees (prunable / locked / detached) and
how far the local default branch is behind ``origin/<default>``.

Rules (``docs/repo-inspection.md``):

* **No network, no writes.** Only the fixed argument lists in ``GIT_COMMANDS`` run, with
  ``shell=False``, a cleaned environment (no inherited ``GIT_*``) and ``-c`` overrides that
  switch off configuration-driven execution (``core.fsmonitor``, hooks, filter drivers,
  transports, pagers, signature checks). Every call has a timeout and an output cap.
  Known gap: a ``filter.<x>.clean`` written into the configuration between the driver listing
  and ``status`` can run once; a driver still present afterwards is detected and the result
  dropped, one added *and removed again* inside that window is not (see "Remaining risks").
* **Paths pass the read policy.** Every configured path, scanned directory and Git
  directory goes through ``WritePathPolicy.check_read`` (read roots, secret-path floor,
  data-directory floor). This module does not follow a configured path, a scanned entry,
  ``.git`` or ``commondir`` that is a symbolic link or junction, and pins git to the checked
  ``--git-dir``/``--work-tree``; git itself still follows links *inside* the Git directory
  (refs, objects, ``config``), which this module does not walk. It is a check at one
  instant, not a sandbox (``docs/repo-inspection.md``, "Remaining risks").
* **Allow-listed output.** Only the fields built in ``_collect``; names and URLs pass
  ``mask_secrets`` and are cut. No commit message, body or diff is read.

Configuration is the caller's: ``paths``, ``scan_roots`` and ``scan_depth`` are arguments.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat as stat_module
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from stiracore.identity_paths import WritePathPolicy
from stiracore.observability.masking import mask_secrets

GIT_TIMEOUT_SEC = 10.0
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
STATUS_TTL_SEC = 300.0
MAX_PATHS = 200
MAX_PATH_LENGTH = 1024
MAX_SCAN_DEPTH = 3
MAX_SCAN_ENTRIES = 5000
MAX_REPOSITORIES = 200
MAX_BRANCHES = 100
MAX_WORKTREES = 100
MAX_FILTER_DRIVERS = 64
MAX_NAME_LENGTH = 120
MAX_TEXT_LENGTH = 240
MAX_CACHE_ENTRIES = 512
INSPECT_ALL_DEADLINE_SEC = 60.0
MASKED = "[masked]"
OUTSIDE = "[outside read roots]"

# Configuration that would make a read-only command run a program, touch the network or
# write. Applied to every call (and inherited by any git child via GIT_CONFIG_PARAMETERS).
HARDENING_ARGS: tuple[str, ...] = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=",  # empty: older git would run a command named "false"
    "-c",
    "core.useBuiltinFSMonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.pager=cat",
    "-c",
    "core.askPass=",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.allow=never",
    "-c",
    "diff.external=",
    "-c",
    "log.showSignature=false",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
    "-c",
    "submodule.recurse=false",
    "-c",
    "status.submoduleSummary=false",
    "-c",
    "core.quotePath=false",
    "-c",
    "safe.bareRepository=explicit",
    "-c",
    "color.ui=never",
)

# The only git argument lists this module runs (``{base}``/``{ref}`` are validated refs).
GIT_COMMANDS: dict[str, tuple[str, ...]] = {
    "dirs": (
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
        "--show-toplevel",
    ),
    "filters": ("config", "--null", "--name-only", "--get-regexp", r"^filter\."),
    "status": (
        "status",
        "--porcelain=v2",
        "--branch",
        "--ignore-submodules=all",
        "--untracked-files=normal",
        "--ahead-behind",
    ),
    "origin_head": ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
    "verify": ("rev-parse", "--verify", "--quiet", "{ref}"),
    "branches": (
        "for-each-ref",
        "--format=%(refname:short)%00%(ahead-behind:{base})",
        "refs/heads",
    ),
    "worktrees": ("worktree", "list", "--porcelain", "-z"),
    "stash": ("rev-list", "--walk-reflogs", "--count", "refs/stash"),
    "last_commit": ("log", "-1", "--no-show-signature", "--format=%h%x00%cI"),
    "origin_url": ("config", "--get", "remote.origin.url"),
}

# Environment variables kept from the server's environment; every ``GIT_*`` is dropped.
_KEPT_ENV = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
    "TMP",
    "TEMP",
    "TMPDIR",
    "XDG_CONFIG_HOME",
)
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_CONTROL = re.compile(r"[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class GitOutput:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


Runner = Callable[..., GitOutput]


class GitProbeError(RuntimeError):
    """One git probe could not complete (the message is masked text)."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def clean_text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    """One line, no control or invisible characters, secrets masked, cut to ``limit``."""
    text = unicodedata.normalize("NFKC", str(value))
    text = _CONTROL.sub(" ", text).replace("<", "\u2039").replace(">", "\u203a")
    text = re.sub(r" {2,}", " ", mask_secrets(text)).strip()
    return text if len(text) <= limit else text[:limit] + "\u2026"


def clean_name(value: Any, limit: int = MAX_NAME_LENGTH) -> str:
    """A branch/ref name: ``[masked]`` as a whole if it looks like a secret, else cleaned."""
    text = str(value)
    if mask_secrets(text) != text:
        return MASKED
    return clean_text(text, limit)


def sanitize_remote_url(raw: str | None) -> str | None:
    """The remote URL without userinfo (``user:token@``), then masked and cut."""
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"  # IPv6 literal
            if parsed.port:
                host = f"{host}:{parsed.port}"
            value = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        elif "://" not in value:
            # scp-like ``user:token@host:path``: drop userinfo before the first "/".
            head = value.split("/", 1)[0]
            if "@" in head:
                value = value[head.rindex("@") + 1 :]
    except (ValueError, UnicodeError):
        return MASKED
    return clean_text(value, MAX_PATH_LENGTH)


def github_repository(remote: str | None) -> str | None:
    if not remote:
        return None
    value = remote.strip()
    if value.startswith(("git@github.com:", "github.com:")):
        host, path = "github.com", value.split(":", 1)[1]
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        host, path = (parsed.hostname or "").casefold(), parsed.path.lstrip("/")
    if host not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    repo = parts[1][:-4] if parts[1].casefold().endswith(".git") else parts[1]
    return clean_name(f"{parts[0]}/{repo}")


def git_environment(repo: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of every git call: kept variables only, no ``GIT_*`` inherited."""
    source = os.environ if base is None else base
    env = {key: source[key] for key in _KEPT_ENV if key in source}
    env.update(
        {
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_PAGER": "cat",
            "GIT_CEILING_DIRECTORIES": str(repo.parent),
        }
    )
    return env


def _pump(stream, buffer: bytearray, cap: int, over: Callable[[], None], drain: bool) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            room = cap - len(buffer)
            buffer += chunk[: max(room, 0)]
            if len(chunk) > room:
                over()
                if not drain:
                    return
    except (OSError, ValueError):
        return


def default_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    limit: int,
) -> GitOutput:
    """Run ``argv`` (no shell); stop it past ``limit`` stdout bytes or ``timeout`` seconds."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        shell=False,
        close_fds=True,
    )
    out, err = bytearray(), bytearray()
    truncated = threading.Event()

    def stop() -> None:
        truncated.set()
        try:
            proc.kill()
        except OSError:
            pass

    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, out, limit, stop, False), daemon=True),
        threading.Thread(
            target=_pump,
            args=(proc.stderr, err, MAX_STDERR_BYTES, lambda: None, True),  # drop the rest
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1.0)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return GitOutput(
        code,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        truncated.is_set(),
    )


def _is_link(path: Path) -> bool:
    """Symlinks, Windows junctions and other reparse points; an error counts as a link."""
    try:
        info = path.lstat()
    except OSError:
        return True
    if stat_module.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    try:
        return bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def _is_network_path(value: str) -> bool:
    normalized = value.replace("/", "\\")
    return normalized.startswith("\\\\") or normalized.casefold().startswith("\\\\?\\unc\\")


def _read_small_text(path: Path) -> str:
    if _is_link(path) or not path.is_file() or path.stat().st_size > 4096:
        raise OSError("invalid Git metadata file")
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read(4097)


def _allowed(policy: WritePathPolicy, path: Path) -> Path | None:
    decision = policy.check_read(str(path))
    return Path(decision.path) if decision.allowed and decision.path else None


_ABS_PATH_RE = re.compile(r"(?<![\w.~/\\-])(?:[A-Za-z]:[\\/]|[\\/])[^\s'\"`]+")


def scrub_outside_paths(policy: WritePathPolicy, text: str) -> str:
    """Absolute paths in ``text`` (git's stderr) that the read policy refuses → ``OUTSIDE``."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        path = raw.rstrip(".,:;)]")
        tail = raw[len(path) :]
        try:
            return raw if _allowed(policy, Path(path)) is not None else OUTSIDE + tail
        except (OSError, ValueError, RuntimeError):
            return OUTSIDE + tail

    return _ABS_PATH_RE.sub(replace, text)


def git_dirs(repo: Path, policy: WritePathPolicy) -> tuple[Path, Path]:
    """``(git dir, common dir)`` of ``repo``; both must pass the read policy."""
    marker = repo / ".git"
    if _is_link(marker):
        raise OSError("the .git entry is missing or a link")
    if marker.is_dir():
        git_dir = marker.resolve(strict=True)
    else:
        text = _read_small_text(marker)
        if not text.startswith("gitdir:"):
            raise OSError("invalid .git file")
        target = Path(text.split(":", 1)[1].strip())
        git_dir = (target if target.is_absolute() else repo / target).resolve(strict=True)
    common = git_dir
    commondir = git_dir / "commondir"
    if os.path.lexists(commondir):
        if _is_link(commondir) or not commondir.is_file():
            raise OSError("the commondir entry is a link or not a file")
        candidate = Path(_read_small_text(commondir).strip())
        common = (candidate if candidate.is_absolute() else git_dir / candidate).resolve(
            strict=True
        )
    for path in {git_dir, common}:
        if _allowed(policy, path) is None:
            raise OSError("the Git directory is refused by the read policy")
    return git_dir, common


def _is_repository(path: Path) -> bool:
    marker = path / ".git"
    try:
        return not _is_link(marker) and marker.exists()
    except OSError:
        return False


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def repository_id(path: Path) -> str:
    return "local-" + hashlib.sha256(_key(path).encode("utf-8")).hexdigest()[:12]


def check_configured_path(policy: WritePathPolicy, value: Any) -> tuple[Path | None, str | None]:
    """``(resolved directory, None)`` or ``(None, problem code)`` for one configured path.

    Codes: ``invalid``, ``network_path``, ``not_absolute``, ``refused`` (the read policy),
    ``link``, ``missing``, ``not_directory``. Nothing is opened before the policy allows it.
    """
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        return None, "invalid"
    if len(value) > MAX_PATH_LENGTH:
        return None, "invalid"
    text = value.strip()
    if _is_network_path(text):
        return None, "network_path"
    try:
        path = Path(text).expanduser()
    except (OSError, RuntimeError):
        return None, "invalid"
    if _is_network_path(str(path)):
        return None, "network_path"
    if not path.is_absolute():
        return None, "not_absolute"
    resolved = _allowed(policy, path)
    if resolved is None:
        return None, "refused"
    try:
        if not os.path.lexists(path):
            return None, "missing"
        if _is_link(path):
            return None, "link"
        if not resolved.is_dir():
            return None, "not_directory"
    except OSError:
        return None, "invalid"
    return resolved, None


def discover_repositories(
    policy: WritePathPolicy,
    *,
    paths: Iterable[Any] = (),
    scan_roots: Iterable[Any] = (),
    scan_depth: int = 1,
) -> tuple[list[tuple[Path, Path | None]], list[dict[str, Any]]]:
    """Repositories (with their scan root; ``None`` for an explicit path) and problems.

    Explicit ``paths`` come first. Below a scan root, directories up to ``scan_depth``
    levels are visited (links skipped, a repository is not descended into, directories the
    read policy refuses are skipped). Problems are ``{field, index, code}`` only.
    """
    if isinstance(scan_depth, bool) or not isinstance(scan_depth, int):
        raise TypeError("scan_depth must be an integer")
    if not 1 <= scan_depth <= MAX_SCAN_DEPTH:
        raise ValueError(f"scan_depth must be between 1 and {MAX_SCAN_DEPTH}")
    lists = {"paths": list(paths), "scanRoots": list(scan_roots)}
    for field, items in lists.items():
        if len(items) > MAX_PATHS:
            raise ValueError(f"{field} must contain at most {MAX_PATHS} paths")
    found: dict[str, tuple[Path, Path | None]] = {}
    problems: list[dict[str, Any]] = []

    def add(repo: Path, root: Path | None) -> None:
        if len(found) < MAX_REPOSITORIES:
            found.setdefault(_key(repo), (repo, root))

    for index, raw in enumerate(lists["paths"]):
        repo, code = check_configured_path(policy, raw)
        if repo is not None and not _is_repository(repo):
            repo, code = None, "not_repository"
        if repo is None:
            problems.append({"field": "paths", "index": index, "code": code})
            continue
        add(repo, None)

    budget = [MAX_SCAN_ENTRIES]

    def walk(directory: Path, root: Path, depth: int) -> None:
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:  # never more than the remaining budget is listed
                    if len(entries) >= max(budget[0], 0):
                        break
                    entries.append(entry)
        except OSError:
            return
        entries.sort(key=lambda e: e.name.casefold())
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                return
            try:
                if entry.name == ".git" or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            child = Path(entry.path)
            if _is_link(child):
                continue
            allowed = _allowed(policy, child)
            if allowed is None:
                continue
            if _is_repository(allowed):
                add(allowed, root)
            elif depth > 1:
                walk(allowed, root, depth - 1)

    for index, raw in enumerate(lists["scanRoots"]):
        root, code = check_configured_path(policy, raw)
        if root is None:
            problems.append({"field": "scanRoots", "index": index, "code": code})
            continue
        walk(root, root, scan_depth)
    return list(found.values()), problems


def _parse_status(text: str) -> dict[str, Any]:
    branch = ""
    upstream: str | None = None
    ahead = behind = staged = unstaged = untracked = conflicted = 0
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head ") :].strip()
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream ") :].strip() or None
        elif line.startswith("# branch.ab "):
            parts = line.split()
            try:
                ahead = int(parts[-2].removeprefix("+"))
                behind = abs(int(parts[-1].removeprefix("-")))
            except (ValueError, IndexError):
                ahead = behind = 0
        elif line.startswith("? "):
            untracked += 1
        elif line.startswith("u "):
            conflicted += 1
        elif line[:2] in {"1 ", "2 "}:
            xy = line.split(" ", 2)[1] if " " in line[2:] else ".."
            staged += int(xy[:1] not in {"", "."})
            unstaged += int(xy[1:2] not in {"", "."})
    detached = branch in {"", "(detached)"}
    return {
        "branch": "(detached)" if detached else clean_name(branch),
        "detached": detached,
        "upstream": clean_name(upstream) if upstream else None,
        "ahead": ahead,
        "behind": behind,
        "changes": {
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "conflicted": conflicted,
            "total": staged + unstaged + untracked + conflicted,
        },
    }


def _parse_worktrees(
    text: str, repo: Path, inside: Callable[[str], bool] = lambda _path: True
) -> list[dict[str, Any]]:
    """``git worktree list --porcelain -z`` → allow-listed records (paths masked, cut;
    a path the read policy refuses is shown as ``OUTSIDE``)."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*text.split("\0"), ""]:
        if not line:
            if current.get("path"):
                records.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current.get("path"):  # a record without its empty terminator
                records.append(current)
                current = {}
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in {"detached", "prunable", "locked", "bare"}:
            current[key] = True
    repo_key = _key(Path(os.path.abspath(str(repo))))
    out: list[dict[str, Any]] = []
    for record in records[:MAX_WORKTREES]:
        path = str(record["path"])
        branch = record.get("branch")
        head = str(record.get("head", ""))
        main = _key(Path(os.path.abspath(path))) == repo_key
        out.append(
            {
                "path": clean_text(path, MAX_PATH_LENGTH) if main or inside(path) else OUTSIDE,
                "branch": clean_name(branch) if branch is not None else None,
                "head": head[:7] if re.fullmatch(r"[0-9a-f]{4,64}", head) else "",
                "detached": bool(
                    record.get("detached") or (branch is None and not record.get("bare"))
                ),
                "prunable": bool(record.get("prunable")),
                "locked": bool(record.get("locked")),
                "main": main,
            }
        )
    return out


def _worktree_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    linked = [w for w in items if not w["main"]]
    return {
        "total": len(items),
        "prunable": sum(w["prunable"] for w in items),
        "locked": sum(w["locked"] for w in linked),
        "detached": sum(w["detached"] for w in linked),
    }


def _issues(data: Mapping[str, Any]) -> list[str]:
    """Fixed codes for what needs a human (the desk's "!" list in a later stage)."""
    worktrees = data["worktrees"]
    checks = (
        ("behind_upstream", data["behind"] > 0),
        ("unpushed_commits", data["ahead"] > 0),
        ("uncommitted_changes", data["changes"]["total"] > 0),
        ("default_branch_behind", data["defaultBranchBehind"] > 0),
        ("prunable_worktree", worktrees["prunable"] > 0),
        ("locked_worktree", worktrees["locked"] > 0),
        ("detached_worktree", worktrees["detached"] > 0),
    )
    return [code for code, hit in checks if hit]


def _status_colour(data: Mapping[str, Any]) -> str:
    worktrees = data["worktrees"]
    if data["behind"] > 0 or data["defaultBranchBehind"] > 0 or worktrees["prunable"] > 0:
        return "red"
    if (
        data["changes"]["total"] > 0
        or data["ahead"] > 0
        or data["branch"] != data["defaultBranch"]
        or data["stashCount"] > 0
        or worktrees["locked"] > 0
        or worktrees["detached"] > 0
    ):
        return "yellow"
    return "green"


class RepositoryInspector:
    """Inspect repositories under a read policy; results cached for ``ttl`` seconds.

    ``exec_git`` and ``clock`` are injectable (tests use fakes; no wall clock is read for the
    cache). ``environ`` is the environment the kept variables are taken from.
    """

    def __init__(
        self,
        policy: WritePathPolicy,
        *,
        exec_git: Runner = default_runner,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = STATUS_TTL_SEC,
        timeout: float = GIT_TIMEOUT_SEC,
        max_output: int = MAX_OUTPUT_BYTES,
        git: str = "git",
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if policy is None or not callable(getattr(policy, "check_read", None)):
            raise TypeError("a read policy (WritePathPolicy) is required")
        self.policy = policy
        self.exec_git = exec_git
        self.clock = clock
        self.ttl = float(ttl)
        self.timeout = float(timeout)
        self.max_output = int(max_output)
        self.git = git
        self.environ = environ
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _run(
        self,
        target: tuple[Path, Path],
        name: str,
        overrides: Sequence[str],
        *,
        required: bool = True,
        allow: Sequence[int] = (),
        **fields: str,
    ) -> str | None:
        repo, git_dir = target
        args = (
            [part.format(**fields) for part in GIT_COMMANDS[name]]
            if fields
            else list(GIT_COMMANDS[name])
        )
        # The checked paths on the command line win over ``core.worktree`` and discovery.
        argv = [
            self.git,
            *HARDENING_ARGS,
            *overrides,
            "-C",
            str(repo),
            f"--git-dir={git_dir}",
            f"--work-tree={repo}",
            *args,
        ]
        try:
            result = self.exec_git(
                argv,
                env=git_environment(repo, self.environ),
                timeout=self.timeout,
                limit=self.max_output,
            )
        except FileNotFoundError as err:
            raise GitProbeError("git executable not found") from err
        except subprocess.TimeoutExpired as err:
            raise GitProbeError(f"git {name} timed out") from err
        except OSError as err:
            raise GitProbeError(f"git {name} could not start ({type(err).__name__})") from err
        if result.truncated:
            raise GitProbeError(f"git {name} output exceeded {self.max_output} bytes")
        if result.returncode in allow:
            return result.stdout  # an exit code the caller names as a legitimate answer
        if result.returncode != 0:
            if not required:
                return None
            detail = result.stderr or f"exit {result.returncode}"
            detail = scrub_outside_paths(self.policy, detail)
            raise GitProbeError(f"git {name} failed: {clean_text(detail)}")
        return result.stdout

    def _check_git_view(self, target: tuple[Path, Path], common: Path) -> None:
        """git's own view (after it resolved everything) must match the checked paths."""
        raw = self._run(target, "dirs", ())
        lines = (raw or "").splitlines()
        if len(lines) != 3:
            raise GitProbeError("git rev-parse gave an unexpected answer")
        expected = (target[1], common, target[0])
        for text, want in zip(lines, expected, strict=True):
            seen = _allowed(self.policy, Path(text))
            if seen is None or _key(seen.resolve()) != _key(want.resolve()):
                raise GitProbeError("git resolved a directory other than the checked one")

    def _drivers(self, target: tuple[Path, Path]) -> list[str]:
        # ``git config --get-regexp`` exits 1 when nothing matches: that is the only exit besides 0
        # that means "no filter drivers". Any other failure (a bad config line, a timeout) cannot
        # be read as "none": it refuses the repository (fail closed, #836 review).
        raw = self._run(target, "filters", (), allow=(1,)) or ""
        drivers: list[str] = []
        for name in raw.split("\0"):
            name = name.strip("\n")
            if not name.startswith("filter.") or name.count(".") < 2:
                continue
            driver = name[len("filter.") : name.rindex(".")]
            if not driver or "=" in driver or _CONTROL.search(driver):
                raise GitProbeError("a filter driver name cannot be overridden")
            if driver not in drivers:
                drivers.append(driver)
        if len(drivers) > MAX_FILTER_DRIVERS:
            raise GitProbeError("too many filter drivers")
        return drivers

    @staticmethod
    def _filter_overrides(drivers: Sequence[str]) -> list[str]:
        """``-c filter.<driver>.*=`` for every configured filter driver (no clean/smudge
        program runs during ``status``). An unusable driver name refuses the repository."""
        out: list[str] = []
        for driver in drivers:
            for key, value in (
                ("clean", ""),
                ("smudge", ""),
                ("process", ""),
                ("required", "false"),
            ):
                out += ["-c", f"filter.{driver}.{key}={value}"]
        return out

    def _default_branch(
        self, target: tuple[Path, Path], overrides: Sequence[str]
    ) -> tuple[str, str]:
        raw = self._run(target, "origin_head", overrides, required=False) or ""
        name = raw.strip().removeprefix("origin/")
        if name and _REF_RE.fullmatch(name) and ".." not in name:
            return name, "origin/HEAD"
        return "main", "fallback"

    def _compare_base(
        self, target: tuple[Path, Path], default: str, overrides: Sequence[str]
    ) -> str:
        found = self._run(
            target, "verify", overrides, required=False, ref=f"refs/remotes/origin/{default}"
        )
        return f"origin/{default}" if found and found.strip() else default

    def _branches(
        self, target: tuple[Path, Path], base: str, overrides: Sequence[str]
    ) -> list[dict[str, Any]] | None:
        raw = self._run(target, "branches", overrides, required=False, base=base)
        if raw is None:
            return None  # git older than 2.41 has no ahead-behind atom
        out = []
        for line in raw.splitlines()[:MAX_BRANCHES]:
            try:
                name, counts = line.split("\x00", 1)
                ahead, behind = (int(v) for v in counts.split())
            except (ValueError, TypeError):
                return None
            out.append({"name": clean_name(name), "ahead": ahead, "behind": behind})
        return out

    def _collect(self, repo: Path) -> dict[str, Any]:
        resolved = None if _is_link(repo) else _allowed(self.policy, repo)
        if resolved is None:
            raise OSError("the repository path is refused by the read policy")
        repo = resolved  # git only ever sees the resolved, checked path
        git_dir, common = git_dirs(repo, self.policy)
        target = (repo, git_dir)
        self._check_git_view(target, common)
        drivers = self._drivers(target)
        overrides = self._filter_overrides(drivers)
        status = _parse_status(self._run(target, "status", overrides) or "")
        # Detect (not prevent) a filter driver that is *still there* after ``status``: a driver added and
        # removed again inside the ``status`` window is not seen (docs/repo-inspection.md, "Remaining
        # risks"; decision 2026-11-22: detect-and-discard, the plumbing split is a follow-up).
        if set(self._drivers(target)) - set(drivers):
            raise GitProbeError("filter drivers changed during inspection")
        default, source = self._default_branch(target, overrides)
        base = self._compare_base(target, default, overrides)
        branches = self._branches(target, base, overrides)
        worktrees = _parse_worktrees(
            self._run(target, "worktrees", overrides) or "",
            repo,
            lambda path: _allowed(self.policy, Path(path)) is not None,
        )
        stash = (self._run(target, "stash", overrides, required=False) or "0").strip()
        commit = self._run(target, "last_commit", overrides, required=False)
        origin = sanitize_remote_url(self._run(target, "origin_url", overrides, required=False))
        default_behind = 0
        for branch in branches or []:
            if branch["name"] == default:
                default_behind = branch["behind"]
        data: dict[str, Any] = {
            "id": repository_id(repo),
            "name": clean_name(repo.name),
            "path": clean_text(str(repo), MAX_PATH_LENGTH),
            **status,
            "defaultBranch": clean_name(default),
            "defaultBranchSource": source,
            "compareBase": clean_name(base),
            "defaultBranchBehind": default_behind,
            "branches": branches,
            "worktrees": _worktree_summary(worktrees),
            "worktreeList": worktrees,
            "stashCount": int(stash) if stash.isdigit() else 0,
            "lastCommit": _last_commit(commit),
            "originUrl": origin,
            "github": github_repository(origin),
            "fetchHeadUpdatedAt": _fetch_updated_at(git_dir, common),
        }
        data["issues"] = _issues(data)
        data["status"] = _status_colour(data)
        return data

    def inspect(self, repo: Path, *, use_cache: bool = True) -> dict[str, Any]:
        """One repository (a resolved, policy-checked directory). A failure is data."""
        key = _key(repo)
        now = self.clock()
        # The read policy is asked first, also for a cached answer.
        if use_cache and _allowed(self.policy, repo) is not None:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None and 0 <= now - cached[0] < self.ttl:
                return copy.deepcopy(cached[1])
        try:
            payload = self._collect(repo)
        except (GitProbeError, OSError, ValueError) as err:
            first = err.args[0] if err.args else None
            # Our own messages are fixed texts; an OS error is named by its type only.
            message = first if isinstance(first, str) else type(err).__name__
            payload = _failure(repo, message)
        if use_cache:
            with self._lock:
                if key not in self._cache and len(self._cache) >= MAX_CACHE_ENTRIES:
                    stale = [
                        k for k, (at, _) in self._cache.items() if not 0 <= now - at < self.ttl
                    ]
                    for old in stale or [min(self._cache, key=lambda k: self._cache[k][0])]:
                        del self._cache[old]
                self._cache[key] = (now, copy.deepcopy(payload))
        return payload

    def inspect_all(
        self,
        *,
        paths: Iterable[Any] = (),
        scan_roots: Iterable[Any] = (),
        scan_depth: int = 1,
        use_cache: bool = True,
        deadline: float = INSPECT_ALL_DEADLINE_SEC,
    ) -> dict[str, Any]:
        """Discover (under the policy) and inspect: ``{repositories, problems}``.

        Past ``deadline`` seconds (on the injected clock) the remaining repositories are
        reported as ``inspection_failed`` with the error ``deadline`` (not cached)."""
        started = self.clock()
        found, problems = discover_repositories(
            self.policy, paths=paths, scan_roots=scan_roots, scan_depth=scan_depth
        )
        repositories = []
        for repo, root in found:
            if self.clock() - started >= deadline:
                item = _failure(repo, "deadline")
            else:
                item = self.inspect(repo, use_cache=use_cache)
            item["root"] = clean_text(str(root), MAX_PATH_LENGTH) if root is not None else None
            repositories.append(item)
        return {"repositories": repositories, "problems": problems}


def _failure(repo: Path, message: str) -> dict[str, Any]:
    return {
        "id": repository_id(repo),
        "name": clean_name(repo.name),
        "path": clean_text(str(repo), MAX_PATH_LENGTH),
        "status": "red",
        "issues": ["inspection_failed"],
        "error": clean_text(message),
    }


def _last_commit(text: str | None) -> dict[str, str] | None:
    """Short hash and committer date only (no subject, body or diff is read)."""
    if not text:
        return None
    parts = text.strip().split("\x00")
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{4,64}", parts[0]):
        return None
    return {"hash": parts[0][:12], "date": clean_text(parts[1], 40)}


def _fetch_updated_at(git_dir: Path, common: Path) -> str | None:
    """When ``FETCH_HEAD`` last changed (a file time; this module never fetches)."""
    for path in (common / "FETCH_HEAD", git_dir / "FETCH_HEAD"):
        try:
            if not _is_link(path) and path.is_file():
                return _iso(path.stat().st_mtime)
        except OSError:
            continue
    return None
