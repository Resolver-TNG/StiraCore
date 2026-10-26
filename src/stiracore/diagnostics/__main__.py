"""``stira doctor`` / ``python -m stiracore.diagnostics doctor`` (#244).

Also ``stira tool-rules status|lock`` (#273, see :mod:`stiracore.tools.tool_rules_cli`)
and ``stira memory retention|forget|restore|removed|jobs|tag`` (#294, #173, :mod:`stiracore.control.memory_cli`).
``stira yolo pending|confirm|deny`` and ``stira approvals pending|decide`` are the host
confirmation of ADR 0012 §8b (:mod:`stiracore.yolo.host_cli`; only with a server
running with ``STIRA_YOLO_ALLOW``). ``stira authority status|unlatch`` shows the authority
latches and clears one with the server stopped (ADR 0011 W2, :mod:`stiracore.authority.cli`).

Reads the agent configuration (write roots, workspace) and checks every write
root. Exit status: 0 = no errors (warnings allowed), 1 = errors, 2 = bad usage
or configuration (unreadable file, or the write policy cannot be built, #267).
The configuration is only read, never written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from stiracore.authority import cli as authority_cli
from stiracore.config.data_dir import WORKSPACE_NAME, DataDirSettingError, explicit_data_dir
from stiracore.config.data_dir import build_policy as data_dir_floor_policy
from stiracore.config.models import DEFAULT_AGENT_CONFIG_PATH, AgentConfig, GatewayConfig
from stiracore.control import memory_cli
from stiracore.identity_paths import WritePathPolicy
from stiracore.observability.settings_hub import classifier_choice
from stiracore.tools import tool_rules_cli
from stiracore.yolo import host_cli

from .doctor import NO_POLICY, render_text, run_doctor
from .probes import display_path


def _same_file(left: Path, right: Path) -> bool:
    """The same config file, however it is spelled (``..``, symlinks, case on APFS)."""
    left, right = left.expanduser(), right.expanduser()
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        return False
    return left.resolve() == right.resolve()


def _classifier_choice(agent_config: Path) -> str:
    """The settings hub's classifier choice (``auto``/``rule``/``sokudan``), read-only.

    The gateway config sits next to the agent config; a missing or unreadable file means the
    default, ``auto``.
    """
    try:
        gateway = GatewayConfig.load(Path(agent_config).expanduser().parent / "gateway-config.json")
        return classifier_choice(gateway)
    except (OSError, ValueError, TypeError):
        return "auto"


def _run_checks_code(agent_config: Path) -> str:
    """``run_checks_off`` / ``run_checks_on`` / a platform code, from the gateway config
    next to the agent config (ADR 0019 §8); unreadable = off."""
    from stiracore.tools import run_checks

    try:
        gateway = GatewayConfig.load(Path(agent_config).expanduser().parent / "gateway-config.json")
        settings = run_checks.settings_from(gateway.run_checks)
    except (OSError, ValueError, TypeError):
        return "run_checks_off"
    if not settings.enabled:
        return "run_checks_off"
    return run_checks.platform_code() or "run_checks_on"


def _checks_discover(args: argparse.Namespace) -> int:
    """``stira checks discover <dir>`` (ADR 0019 phase 0): the kinds that apply and the
    argv each would run. Files only — nothing is executed."""
    from stiracore.tools import run_checks

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print("stira checks: not a directory", file=sys.stderr)
        return 2
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else directory
    report = run_checks.describe(directory, workspace)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"folder: {report['dir']}")
    for row in report["checks"]:
        print(f"  {row['kind']}: {' '.join(row['argv'])}")
    for kind, why in sorted(report["missing"].items()):
        print(f"  ({kind}: not offered — {why})")
    print("Nothing was run (discovery reads files only).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stira", description="StiraCore utilities")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="check the write environment (changes nothing)")
    doctor.add_argument("--json", action="store_true", help="print the JSON report")
    doctor.add_argument(
        "--config",
        type=Path,
        default=None,
        help="agent config file (default: the server's, in STIRA_DATA_DIR when set)",
    )
    doctor.add_argument("--workspace", help="workspace for @workspace (default: config/cwd)")
    doctor.add_argument(
        "--root", action="append", dest="roots", help="check this root instead (repeatable)"
    )
    doctor.add_argument(
        "--no-write-test", action="store_true", help="skip the temporary-file write test"
    )
    checks = commands.add_parser("checks", help="run_checks discovery (ADR 0019; runs nothing)")
    checks_sub = checks.add_subparsers(dest="checks_command", required=True)
    discover = checks_sub.add_parser("discover", help="list the kinds and argv that would run")
    discover.add_argument("directory")
    discover.add_argument("--workspace", help="the workspace (default: the directory)")
    discover.add_argument("--json", action="store_true")
    tool_rules_cli.add_parser(commands)
    memory_cli.add_parser(commands)
    host_cli.add_parsers(commands)
    authority_cli.add_parser(commands)
    args = parser.parse_args(argv)
    if args.command in ("yolo", "approvals"):
        return host_cli.run(args)
    if args.command == "authority":
        return authority_cli.run(args)
    if args.command == "checks":
        return _checks_discover(args)
    if args.command == "tool-rules":
        return tool_rules_cli.run(args)
    if args.command == "memory":
        return memory_cli.run(args)
    # The server's agent config (#272): inside STIRA_DATA_DIR when that is set.
    try:
        explicit = explicit_data_dir(os.environ)
    except DataDirSettingError as error:
        print(f"stira doctor: {error}", file=sys.stderr)
        return 2
    server_config = explicit / "agent-config.json" if explicit else DEFAULT_AGENT_CONFIG_PATH
    if args.config is None:
        args.config = server_config
    try:
        config = AgentConfig.load(args.config)
    except (OSError, ValueError, TypeError) as error:
        shown = display_path(Path(args.config).expanduser().absolute())
        print(f"stira doctor: cannot read {shown}: {type(error).__name__}", file=sys.stderr)
        return 2
    # The chat server's own config (the default path, compared after normalising): its data
    # directory is on the floor and the policy is the server's own (#271).
    stira = _same_file(Path(args.config), server_config)
    # The server's own spelling of the data directory, not the one typed here.
    data_dir = server_config.absolute().parent if stira else None
    if args.workspace:
        config.workspace_dir = args.workspace
    roots = list(args.roots or ())
    effective_roots_known = False
    # The configured policy (floor + write roots), as the server builds it; if it
    # cannot be built, no write test runs at all (#266).
    try:
        if data_dir is not None:
            policy, floor = data_dir_floor_policy(
                config, data_dir, read_only_paths=config.read_only_paths
            )
            # Diagnose the roots the server enforces, not only the configured ones.
            roots = roots or list(floor.roots)
            effective_roots_known = True
        else:
            policy = WritePathPolicy(
                config.read_only_paths,
                base_dir=Path(config.workspace_dir or str(Path.cwd())).expanduser(),
                write_roots=config.write_roots,
                outside_action=config.write_outside_action,
                allow_broad_roots=config.allow_broad_write_roots,
            )
        workspace = policy.workspace
    except (OSError, ValueError, RuntimeError):
        policy = NO_POLICY
        workspace = Path(
            config.workspace_dir
            or (str(data_dir / WORKSPACE_NAME) if data_dir is not None else str(Path.cwd()))
        ).expanduser()
    if not effective_roots_known:
        roots = roots or list(config.write_roots)
    report = run_doctor(
        roots,
        workspace=workspace,
        policy=policy,
        write_test=not args.no_write_test,
        classifier_choice=_classifier_choice(args.config),
        run_checks_code=_run_checks_code(args.config),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    if report.get("config_error"):
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
