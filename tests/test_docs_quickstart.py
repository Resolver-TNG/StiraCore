"""Execute the runnable Python blocks in docs/quickstart.md."""

import re
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[1] / "docs" / "quickstart.md"
BLOCK_RE = re.compile(r"^```python([^\n]*)\n(.*?)^```", re.DOTALL | re.MULTILINE)


def _blocks() -> list[tuple[bool, str]]:
    text = DOC.read_text(encoding="utf-8")
    return [("no-run" in info, body) for info, body in BLOCK_RE.findall(text)]


def test_quickstart_has_runnable_blocks() -> None:
    blocks = _blocks()
    assert any(not no_run for no_run, _ in blocks)


def test_quickstart_no_run_blocks_compile() -> None:
    for index, (no_run, body) in enumerate(_blocks()):
        if no_run:
            compile(body, f"quickstart-no-run-{index}", "exec")


def test_quickstart_runnable_blocks_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    namespace: dict[str, object] = {"__name__": "quickstart_doc"}
    ran = 0
    for index, (no_run, body) in enumerate(_blocks()):
        if no_run:
            continue
        exec(compile(body, f"quickstart-block-{index}", "exec"), namespace)  # noqa: S102
        ran += 1
    assert ran >= 1
