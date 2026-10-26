"""Issue #243 (wave 24): name aliases the floor's comparison key did not see.

Measured on macOS 26 (2026-10-23) with disk images (``hdiutil``):

* HFS+ (still a supported macOS volume format, e.g. external disks) ignores the
  code points U+200C–U+200F, U+202A–U+202E, U+206A–U+206F and U+FEFF when it
  compares names: ``SO\\u200cUL.md`` *is* ``SOUL.md`` there. The floor's key kept
  them, so a new ``memory/proce\\u200cdural/x.md`` (protected directory not yet
  created) or ``sub/SO\\u200cUL.md`` passed. APFS, case-sensitive APFS, NTFS and
  ext4 keep them distinct (dropping them in a deny key only over-denies).
* ``casefold()`` leaves U+0131 (dotless i) alone while its simple uppercase is
  ASCII ``I`` — the one code point (exhaustive search below) where a volume that
  compares upper-cased names could see ``\\u0131PD.md`` as ``IPD.md``.
* Windows 8.3 short names (``PROCED~1``): ``realpath`` expands them for existing
  paths; a short-shaped component that is still there after resolving is refused
  (fail closed; unverified on a real Windows machine).

Not aliases on any measured volume, so allowed (no over-deny): full-width letters
and look-alike letters from other scripts (``ｐｒｏｃｅｄｕｒａｌ``, Cyrillic ``о``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from stiracore import identity_paths
from stiracore.identity_paths import WritePathPolicy, _fold, _identity_name, path_key

# Listed independently of the implementation (Apple TN1150, "ignorable" code points):
# all 16 must fold away and be refused.
HFS_IGNORABLE = [
    "\u200c", "\u200d", "\u200e", "\u200f",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u206a", "\u206b", "\u206c", "\u206d", "\u206e", "\u206f",
    "\ufeff",
]  # fmt: skip


def test_the_hfs_list_has_16_code_points() -> None:
    assert len(set(HFS_IGNORABLE)) == 16


def _policy(tmp_path: Path, **kwargs) -> WritePathPolicy:
    base = tmp_path.resolve()
    (base / "memory").mkdir()
    (base / "sub").mkdir()
    (base / "SOUL.md").write_text("soul", encoding="utf-8")
    return WritePathPolicy(
        base_dir=base,
        protected_dirs=["memory/procedural"],
        write_roots=["@workspace"],
        **kwargs,
    )


@pytest.mark.parametrize("mark", HFS_IGNORABLE)
def test_hfs_ignorable_code_points_fold_away(mark: str) -> None:
    assert path_key(f"/w/memory/proce{mark}dural/x") == path_key("/w/memory/procedural/x")
    assert _identity_name(f"SO{mark}UL.md")
    assert _identity_name(f"{mark}IPD.md")


@pytest.mark.parametrize("mark", HFS_IGNORABLE)
def test_hfs_ignorable_spellings_are_refused(tmp_path: Path, mark: str) -> None:
    policy = _policy(tmp_path)
    # The protected directory does not exist yet: only the key can see the alias.
    assert policy.check(f"memory/proce{mark}dural/x.md").code == "floor"
    assert policy.check(f"sub/SO{mark}UL.md").code == "floor"


def test_dotless_i_is_an_alias_of_i(tmp_path: Path) -> None:
    assert _identity_name("\u0131PD.md")
    assert _fold("\u0131") == _fold("I") == "i"
    assert _policy(tmp_path).check("sub/\u0131pd.md").code == "floor"


def test_every_single_character_case_mapping_folds_to_the_same_key() -> None:
    # A volume may compare upper- or lower-cased names (NTFS: an upcase table).
    misses = []
    for code in range(0x110000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        char = chr(code)
        for mapped in (char.upper(), char.lower()):
            if len(mapped) == 1 and _fold(mapped) != _fold(char):
                misses.append(hex(code))
    assert misses == []


@pytest.mark.parametrize(
    "path",
    [
        "memory/\uff50\uff52\uff4f\uff43\uff45\uff44\uff55\uff52\uff41\uff4c/x.md",  # full width
        "memory/pr\u043ecedural/x.md",  # Cyrillic o
        "sub/\uff33\uff2f\uff35\uff2c.md",  # full-width SOUL
        "memory/proce\u200bdural/x.md",  # ZWSP: not ignored by HFS+ (measured)
    ],
)
def test_distinct_look_alike_names_stay_writable(tmp_path: Path, path: str) -> None:
    assert _policy(tmp_path).check(path).allowed


@pytest.mark.parametrize(
    ("name", "short"),
    [
        ("PROCED~1", True),
        ("procedural~1", False),  # base longer than 8 characters
        ("SOUL~1.MD", True),
        ("PR3F2D~1", True),
        ("PR3F2D~12", False),
        ("A~1.TXT", True),
        ("notes~", False),
        ("~1", False),
        ("x~1.long", False),
        ("plain", False),
    ],
)
def test_short_name_shape(name: str, short: bool) -> None:
    assert identity_paths.windows_short_name(name) is short


def test_a_literal_short_shaped_name_stays_usable_before_and_after_creation(
    tmp_path: Path,
) -> None:
    # Review of #590: a real long name ``NOTES~1`` was refused once it existed.
    policy = _policy(tmp_path, windows=True)
    target = "memory/NOTES~1/x.md"
    assert policy.check(target).allowed
    (tmp_path / "memory" / "NOTES~1").mkdir()
    (tmp_path / "memory" / "NOTES~1" / "x.md").write_text("x", encoding="utf-8")
    assert policy.check(target).allowed
    assert policy.check_read(target).allowed
    assert policy.check("memory/NOTES~1/y.md").allowed
    assert policy.check("memory/notes~/x.md").allowed


def _hide_from_listing(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Simulate a Windows 8.3 alias: the name opens (``lstat``) but is not listed."""
    real = identity_paths._entry_names

    def listed(directory: Path) -> frozenset[str]:
        return real(directory) - {name}

    monkeypatch.setattr(identity_paths, "_entry_names", listed)


def test_an_unexpanded_short_name_is_refused_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path, windows=True)
    (tmp_path / "memory" / "PROCED~1").mkdir()
    _hide_from_listing(monkeypatch, "PROCED~1")
    assert policy.check("memory/PROCED~1/x.md").code == "invalid"
    assert policy.check_read("memory/PROCED~1/x.md").code == "invalid"


def test_an_unexpanded_short_alias_of_the_protected_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path, windows=True)
    (tmp_path / "memory" / "procedural").mkdir()
    # Stand-in for the short name of ``procedural``: a second name of the same directory.
    (tmp_path / "memory" / "PROCED~1").symlink_to("procedural")
    assert policy.check("memory/PROCED~1/x.md").code == "floor"
    (tmp_path / "memory" / "PROCED~1").unlink()
    (tmp_path / "memory" / "PROCED~1").mkdir()
    _hide_from_listing(monkeypatch, "PROCED~1")
    assert policy.check("memory/PROCED~1/x.md").code == "invalid"


@pytest.mark.parametrize("error", [PermissionError, OSError, RuntimeError])
def test_an_error_while_checking_a_short_name_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    policy = _policy(tmp_path, windows=True)
    (tmp_path / "memory" / "NOTES~1").mkdir()

    def broken(directory: Path) -> frozenset[str]:
        raise error("listing failed")

    monkeypatch.setattr(identity_paths, "_entry_names", broken)
    assert policy.check("memory/NOTES~1/x.md").code == "invalid"
    assert policy.check_read("memory/NOTES~1/x.md").code == "invalid"


def test_short_names_are_not_judged_off_windows(tmp_path: Path) -> None:
    policy = _policy(tmp_path, windows=False)
    (tmp_path / "memory" / "PROCED~1").mkdir()
    assert policy.check("memory/PROCED~1/x.md").allowed


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("STIRA_FS_IMAGE_TESTS") != "1",
    reason="opt-in (STIRA_FS_IMAGE_TESTS=1): attaches an HFS+ disk image with hdiutil",
)
def test_on_a_real_hfs_volume(tmp_path: Path) -> None:
    hdiutil = shutil.which("hdiutil")
    assert hdiutil
    image = tmp_path / "hfs.dmg"
    mount = tmp_path / "mnt"
    name = f"stira{uuid.uuid4().hex[:6]}"
    subprocess.run(
        [hdiutil, "create", "-quiet", "-size", "8m", "-fs", "HFS+", "-volname", name, image],
        check=True,
    )
    subprocess.run(
        [hdiutil, "attach", "-quiet", "-nobrowse", "-mountpoint", mount, image], check=True
    )
    try:
        policy = _policy(mount)
        base = mount.resolve()
        # The alias is real on HFS+ ...
        assert (base / "SO\u200cUL.md").exists()
        assert (base / "memory").samefile(base / "mem\u200dory")
        # ... and refused, also where nothing exists yet.
        assert policy.check("SO\u200cUL.md").code == "floor"
        assert policy.check("sub/SO\u200cUL.md").code == "floor"
        assert policy.check("memory/proce\u200cdural/x.md").code == "floor"
    finally:
        subprocess.run([hdiutil, "detach", "-quiet", mount], check=False)
