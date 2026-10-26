"""Server side of the UI i18n catalogs (#184): embed ja/en catalogs into templates."""

from __future__ import annotations

import json
from pathlib import Path

LANGS = ("ja", "en")


def load_catalogs(catalog_dir: Path) -> dict[str, dict[str, str]]:
    """Read catalog.<lang>.json files; every key must exist in both languages."""
    catalogs: dict[str, dict[str, str]] = {}
    for lang in LANGS:
        path = Path(catalog_dir) / f"catalog.{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError(f"{path.name} must be a flat string map")
        catalogs[lang] = data
    missing = set(catalogs["ja"]) ^ set(catalogs["en"])
    if missing:
        raise ValueError(f"catalog keys differ between languages: {sorted(missing)[:5]}")
    return catalogs


def install_i18n(app, catalog_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Expose `i18n_catalog` (both languages) and `ja(key)` to Jinja templates."""
    directory = Path(catalog_dir) if catalog_dir else Path(app.static_folder) / "i18n"
    catalogs = load_catalogs(directory)
    app.extensions["stira_i18n"] = catalogs

    @app.context_processor
    def _i18n_context():
        return {"i18n_catalog": catalogs, "ja": lambda key: catalogs["ja"].get(key, key)}

    return catalogs
