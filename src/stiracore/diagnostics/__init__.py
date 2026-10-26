"""Write-failure diagnosis, permission cards and ``stira doctor`` (#244).

Everything here only *reads* (``lstat``/``statvfs``/extended-attribute and ACL
queries) or, in ``stira doctor``, creates and removes one temporary file per
write root. Nothing changes a setting, a file flag, an extended attribute or a
privacy permission: fixing the cause is a human's job, described as text on the
permission card. Paths in macOS privacy-protected (TCC) locations are recognised
lexically and never touched, so no consent dialog is ever raised.
"""

from .probes import display_path, responsible_process, tcc_zone
from .write_errors import (
    CATEGORIES,
    WriteFailure,
    WriteFailureBoard,
    WriteFailureHook,
    classify_write_failure,
)

__all__ = [
    "CATEGORIES",
    "WriteFailure",
    "WriteFailureBoard",
    "WriteFailureHook",
    "classify_write_failure",
    "display_path",
    "responsible_process",
    "tcc_zone",
]
