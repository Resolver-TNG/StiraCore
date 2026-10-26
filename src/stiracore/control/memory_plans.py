"""Plan files of memory jobs (ADR 0018 phase 0) under deletes and retention.

``<memory_dir>/memory-plans/<kind>/<run_id>.json`` hold the keys of the entries a job looked
at and, for ``memory_consolidate``, a summary derived from them. Two rules keep a plan file
from outliving the memory it describes:

* **a delete request** (``stira memory forget``) removes every plan file that names a
  forgotten entry (its key as a JSON string anywhere in the file) or holds the requested
  text, as one more reached store of the delete job: the same inventory, digest check,
  step-by-step state file and residual check as checkpoints. The whole file goes (a plan
  is a proposal; the next run writes a new one), and retention copies of it in the archive
  are purged like every other archived item;
* **retention**: ``stira memory retention`` lists plan files older than
  ``memory_retention.memory_plans.max_age_days`` (default :data:`PLAN_MAX_AGE_DAYS_DEFAULT`,
  the age is the file's modification time); only ``--apply`` removes them, after archiving
  them in one removal record (``stira memory restore <id>`` brings them back).

This module only names places and matches keys; it reads no plan content into memory beyond
the bytes the caller already holds, and imports nothing from the job executor.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = ["PLANS_DIRNAME", "PLAN_MAX_AGE_DAYS_DEFAULT", "names_any", "plans_dir"]

PLANS_DIRNAME = "memory-plans"
# Conservative: a plan is read by a person within days; three months is far longer than any
# undo window of ADR 0018 (30 days) and still bounds how long a key outlives its review.
PLAN_MAX_AGE_DAYS_DEFAULT = 90


def plans_dir(layout: Any) -> Path:
    """``<memory_dir>/memory-plans`` of a :class:`~stiracore.control.memory_layout.DataLayout`."""
    return Path(layout.memory_dir) / PLANS_DIRNAME


def names_any(text: str, keys: Iterable[str]) -> bool:
    """Whether ``text`` (a plan file) names one of ``keys`` as a JSON string.

    Plan files are written with ``ensure_ascii=False``; both spellings are checked so a file
    written otherwise is found too. A key quoted inside a summary is escaped (``\\"``) and so
    does not count as a name.
    """
    for key in keys:
        if not key:
            continue
        for spelling in {json.dumps(key, ensure_ascii=False), json.dumps(key)}:
            if spelling in text:
                return True
    return False
