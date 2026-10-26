"""The desktop host confirmation on macOS (ADR 0012 §8b (ii), Open (12)).

One ``osascript -l JavaScript`` process per operation shows a native dialog with the
fixed text of :func:`host_channel.describe` (passed as an argument, never spliced into
the script) and the buttons ``拒否 / Deny`` (default) and ``確認 / Confirm``. On
"Confirm" the same process asks for an **OS authentication** (LocalAuthentication,
``deviceOwnerAuthentication``: Touch ID or the account password). Only a successful
authentication is a confirmation; Deny, a closed dialog, a timeout, a failed or
cancelled authentication and any error are a **denial** (fail closed).

Measured on macOS 26.5.2 (2026-10-18): the plain dialog appears from the server's user
session and its timeout path answers ``timeout``; ``canEvaluatePolicy`` for the OS
authentication answers yes without showing anything. **Not measured:** a person
completing Touch ID / the password inside this flow (an authentication requested by a
background process with no dialog of its own was cancelled by the system, error -4) — if
it does not complete on a machine, every confirmation there is a denial and the operator
sets ``STIRA_YOLO_HOST_CONFIRM=command``. Linux / Windows dialogs are not measured: the
command channel is used there.

Tests never show a dialog: the runner is injected (:class:`DialogConfirmer`).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable

from .host_channel import describe

__all__ = ["CHECK_SCRIPT", "DIALOG_SCRIPT", "DialogConfirmer", "dialog_ready", "run_osascript"]

logger = logging.getLogger(__name__)

# argv[0] = the fixed text, argv[1] = timeout seconds. Prints confirm | deny | timeout.
DIALOG_SCRIPT = r"""
ObjC.import('LocalAuthentication');
ObjC.import('Foundation');
function run(argv) {
  var text = String(argv[0]);
  var timeout = Math.max(5, Math.min(180, parseInt(argv[1], 10) || 60));
  var app = Application.currentApplication();
  app.includeStandardAdditions = true;
  var answer;
  try {
    answer = app.displayDialog(text, {
      withTitle: 'Stira',
      buttons: ['拒否 / Deny', '確認 / Confirm'],
      defaultButton: '拒否 / Deny',
      cancelButton: '拒否 / Deny',
      withIcon: 'caution',
      givingUpAfter: timeout
    });
  } catch (e) {
    return 'deny';
  }
  if (answer.gaveUp) return 'timeout';
  if (answer.buttonReturned !== '確認 / Confirm') return 'deny';
  var ctx = $.LAContext.alloc.init;
  var state = {result: null};
  ctx.evaluatePolicyLocalizedReasonReply(2, 'Stira: ' + text.split('\n')[0], function (ok, err) {
    state.result = ok ? 'confirm' : 'deny';
  });
  var deadline = Date.now() + timeout * 1000;
  while (state.result === null && Date.now() < deadline) {
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
  }
  if (state.result === null) {
    ctx.invalidate;
    return 'timeout';
  }
  return state.result;
}
"""

# Shows nothing: can this session ask for an OS authentication at all?
CHECK_SCRIPT = r"""
ObjC.import('LocalAuthentication');
function run(argv) {
  var ctx = $.LAContext.alloc.init;
  return ctx.canEvaluatePolicyError(2, Ref()) ? 'can' : 'cannot';
}
"""

Runner = Callable[[list[str], float], tuple[int, str]]


def run_osascript(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run ``osascript`` (killed after ``timeout``); ``(returncode, stdout)``."""
    try:
        done = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, done.stdout.strip()


def dialog_ready(runner: Runner = run_osascript, platform: str | None = None) -> bool:
    """macOS and an OS authentication available (no UI is shown by this check)."""
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return False
    code, out = runner(["-e", CHECK_SCRIPT], 10.0)
    return code == 0 and out == "can"


class DialogConfirmer:
    """Shows the dialog for each new operation and reports the answer to the desk.

    ``decide(id, incarnation=, decision=, principal=, channel=)`` is the desk's; the
    principal is the server's own account (the person at its desktop).
    """

    def __init__(
        self,
        decide: Callable[..., object],
        *,
        incarnation: str,
        principal: str,
        runner: Runner = run_osascript,
        background: bool = True,
    ) -> None:
        self._decide = decide
        self._incarnation = incarnation
        self._principal = principal
        self._runner = runner
        self._background = background

    def __call__(self, view: dict) -> None:
        if self._background:
            threading.Thread(
                target=self.ask, args=(dict(view),), name="yolo-host-dialog", daemon=True
            ).start()
        else:
            self.ask(view)

    def ask(self, view: dict) -> str:
        text = "\n".join(describe(view))
        timeout = view.get("expires_in")
        timeout = timeout if type(timeout) is int and timeout > 0 else 60
        code, out = self._runner(["-e", DIALOG_SCRIPT, text, str(timeout)], timeout + 15.0)
        decision = "confirm" if code == 0 and out == "confirm" else "deny"
        try:
            self._decide(
                view["id"],
                incarnation=self._incarnation,
                decision=decision,
                principal=self._principal,
                channel="dialog",
            )
        except Exception as error:  # noqa: BLE001 - expired / cancelled meanwhile: nothing to do
            logger.info("host dialog answer not applied (%s)", type(error).__name__)
        return decision
