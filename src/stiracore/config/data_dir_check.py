"""``python -m stiracore.config.data_dir_check``: the one ``STIRA_DATA_DIR`` check (#272).

``scripts/stira_ctl.sh`` and ``stira_ctl.ps1`` call this instead of their own
rules, so the wrappers accept and refuse exactly what the server does
(:func:`~stiracore.config.data_dir.explicit_data_dir`). Reads only the
environment; creates nothing.

- stdout, last line: ``data_dir=<resolved directory>``, or ``data_dir=`` when the
  variable is unset or blank (the default location). Exit 0.
- refused: ``Stira: <reason>`` on stderr (the variable's name, never its value). Exit 2.
"""

from __future__ import annotations

import os
import sys

from stiracore.config.data_dir import DataDirSettingError, explicit_data_dir


def main() -> int:
    try:
        resolved = explicit_data_dir(os.environ)
    except DataDirSettingError as error:
        print(f"Stira: {error}", file=sys.stderr)
        return 2
    print(f"data_dir={'' if resolved is None else resolved}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())
