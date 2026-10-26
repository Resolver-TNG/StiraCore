"""docs/providers*.md stay in sync with the provider registry and remain runnable."""

import re
from pathlib import Path

import pytest

from stiracore.providers import PROVIDERS, ModelSpec

DOCS = Path(__file__).resolve().parents[1] / "docs"
BLOCK_RE = re.compile(r"^```python([^\n]*)\n(.*?)^```", re.DOTALL | re.MULTILINE)


@pytest.mark.parametrize("name", ["providers.md", "providers_ja.md"])
def test_provider_docs_list_every_provider_and_compile(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    for provider in PROVIDERS:
        assert f"`{provider}`" in text, provider
    blocks = BLOCK_RE.findall(text)
    assert blocks
    for index, (_, body) in enumerate(blocks):
        compile(body, f"{name}-{index}", "exec")


@pytest.mark.parametrize("name", ["providers.md", "providers_ja.md"])
def test_documented_specs_are_valid(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    for provider, model_id in re.findall(r'ModelSpec\("([a-z_]+)", "([^"]+)"', text):
        assert ModelSpec(provider, model_id).provider == provider


def test_llamacpp_example_builds_offline():
    text = (DOCS / "providers.md").read_text(encoding="utf-8")
    body = next(b for _, b in BLOCK_RE.findall(text) if '"llamacpp"' in b)
    construction = "\n".join(line for line in body.splitlines() if "print(" not in line)
    namespace: dict = {}
    exec(compile(construction, "providers-llamacpp", "exec"), namespace)  # noqa: S102
    assert namespace["agent"].provider == "llamacpp"
