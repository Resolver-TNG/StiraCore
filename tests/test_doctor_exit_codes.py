"""#267: ``stira doctor`` exit codes, the classifier check and the settings diagnostics tab.

Exit status (documented in docs/diagnostics.md): 0 = nothing wrong or warnings only,
1 = at least one error, 2 = the configuration is unusable (unreadable file, or the write
policy cannot be built). The classifier check never changes the exit status by itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from stiracore.diagnostics import doctor
from stiracore.diagnostics.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "dashboard" / "static" / "i18n"


def _config(tmp_path: Path, **extra) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"workspace_dir": str(work), **extra}), encoding="utf-8")
    return path


def _no_sokudan(monkeypatch):
    monkeypatch.setattr(doctor, "_sokudan_installed", lambda: False)


def _with_sokudan(monkeypatch):
    monkeypatch.setattr(doctor, "_sokudan_installed", lambda: True)


# --- exit codes -------------------------------------------------------------------


def test_exit_0_for_a_healthy_setup_and_for_warnings_only(tmp_path, capsys, monkeypatch):
    _no_sokudan(monkeypatch)
    config = _config(tmp_path)
    assert main(["doctor", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report.get("config_error") is None
    # A warning (the classifier fallback) is still exit 0.
    assert [c["level"] for c in report["checks"]] == ["warning"]
    assert main(["doctor", "--config", str(config)]) == 0


def test_exit_1_when_a_root_has_an_error(tmp_path, capsys, monkeypatch):
    _with_sokudan(monkeypatch)
    config = _config(tmp_path)
    plain_file = tmp_path / "a-file"
    plain_file.write_text("x", encoding="utf-8")
    assert main(["doctor", "--config", str(config), "--json", "--root", str(plain_file)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report.get("config_error") is None


def test_exit_2_when_the_write_policy_cannot_be_built(tmp_path, capsys, monkeypatch):
    """Used to be a per-root warning and exit 0: an invalid setting must stop a script."""
    _with_sokudan(monkeypatch)
    config = _config(tmp_path, write_roots=["/"])  # too broad without the opt-in
    assert main(["doctor", "--config", str(config), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["config_error"] == "policy_unbuildable" and report["ok"] is False
    assert report["roots"][0]["write_test"] == {"ok": None, "skipped": "no_policy"}
    assert main(["doctor", "--config", str(config)]) == 2
    text = capsys.readouterr().out
    assert "CONFIGURATION ERROR" in text


def test_exit_2_for_an_unreadable_config(tmp_path):
    broken = tmp_path / "agent.json"
    broken.write_text("{not json", encoding="utf-8")
    assert main(["doctor", "--config", str(broken)]) == 2


def test_a_report_with_a_config_error_is_never_ok(tmp_path):
    report = doctor.run_doctor([], workspace=tmp_path, policy=doctor.NO_POLICY, write_test=False)
    assert report["config_error"] == "policy_unbuildable" and report["ok"] is False
    assert doctor.run_doctor([], workspace=tmp_path, write_test=False).get("config_error") is None


def test_exit_code_table_is_in_the_docs():
    table = (ROOT / "docs" / "diagnostics.md").read_text(encoding="utf-8")
    assert "| Exit | Meaning |" in table
    for code in ("0", "1", "2"):
        assert f"| `{code}` |" in table
    for name in ("operations.md", "operations_ja.md"):
        assert "diagnostics" in (ROOT / "docs" / name).read_text(encoding="utf-8")


# --- classifier check -------------------------------------------------------------


def test_rule_only_is_a_warning_with_the_install_hint(tmp_path, monkeypatch):
    _no_sokudan(monkeypatch)
    report = doctor.run_doctor([], workspace=tmp_path, write_test=False)
    (check,) = report["checks"]
    assert (check["code"], check["level"]) == ("classifier_rule_only", "warning")
    assert check["mode"] == "rule_only" and check["remedy"] == "install_jev"
    assert 'pip install -e ".[jev]"' in doctor.render_text(report)
    assert report["ok"] is True  # a warning never makes the report fail


def test_an_explicit_rule_choice_is_not_a_warning(tmp_path, monkeypatch):
    _no_sokudan(monkeypatch)
    report = doctor.run_doctor([], workspace=tmp_path, write_test=False, classifier_choice="rule")
    (check,) = report["checks"]
    assert (check["code"], check["level"], check["mode"]) == (
        "classifier_rule_chosen",
        "info",
        "rule_only",
    )


def test_sokudan_installed_is_ok_and_explicit_sokudan_without_it_warns(tmp_path, monkeypatch):
    _with_sokudan(monkeypatch)
    (check,) = doctor.run_doctor([], workspace=tmp_path, write_test=False)["checks"]
    assert (check["code"], check["level"], check["mode"]) == (
        "classifier_sokudan",
        "info",
        "sokudan",
    )
    _no_sokudan(monkeypatch)
    (check,) = doctor.run_doctor(
        [], workspace=tmp_path, write_test=False, classifier_choice="sokudan"
    )["checks"]
    assert (check["code"], check["level"]) == ("classifier_missing", "warning")


def test_the_check_does_not_import_sokudan(monkeypatch):
    import importlib
    import sys

    monkeypatch.delitem(sys.modules, "sokudan", raising=False)
    seen = []
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda n, *a: seen.append(n) or real(n, *a))
    doctor._sokudan_installed()
    assert "sokudan" not in seen and "sokudan" not in sys.modules


def test_the_summary_counts_check_warnings(tmp_path, monkeypatch):
    _no_sokudan(monkeypatch)
    report = doctor.run_doctor([], workspace=tmp_path, write_test=False)
    summary = doctor.summarize(report)
    assert summary["warnings"] == 1 and "classifier_rule_only" in summary["codes"]
    assert summary["ok"] is True


# --- the settings diagnostics tab ---------------------------------------------------


def _catalogs():
    return {
        lang: json.loads((I18N / f"catalog.{lang}.json").read_text(encoding="utf-8"))
        for lang in ("ja", "en")
    }


def test_every_finding_code_has_a_label_in_both_catalogs():
    source = (ROOT / "src/stiracore/diagnostics/doctor.py").read_text(encoding="utf-8")
    used = set(re.findall(r'_finding\(\s*"([a-z_]+)"', source))
    assert used, "no finding code found: the pattern drifted"
    assert used <= set(doctor.FINDING_CODES), sorted(used - set(doctor.FINDING_CODES))
    for lang, catalog in _catalogs().items():
        missing = [c for c in doctor.FINDING_CODES if f"diagnostics.finding_{c}" not in catalog]
        assert missing == [], (lang, missing)
        for key in (
            "diagnostics.skip_no_policy",
            "diagnostics.skip_unsupported",
            "diagnostics.skip_symlink_in_path",
            "diagnostics.skip_changed",
            "diagnostics.fix_install_jev",
            "diagnostics.tab_config_error",
        ):
            assert key in catalog, (lang, key)


def test_the_settings_page_has_a_read_only_diagnostics_section():
    html = (ROOT / "dashboard/templates/settings.html").read_text(encoding="utf-8")
    assert 'href="#sec-diagnostics"' in html and 'id="sec-diagnostics"' in html
    assert "js/settings_diagnostics.js" in html
    section = html[html.index('id="sec-diagnostics"') :]
    section = section[: section.index("</section>")]
    # Nothing to edit or save here: no form fields, one button (re-run the same read-only check).
    assert "<input" not in section and "<select" not in section and "<textarea" not in section
    assert section.count("<button") == 1 and 'id="diag-rerun"' in section


def _blueprint_app(doctor_report, run_doctor):
    from flask import Flask

    from stiracore.diagnostics.write_errors import WriteFailureBoard
    from stiracore.observability.diagnostics_api import create_diagnostics_blueprint

    app = Flask(__name__)
    app.register_blueprint(
        create_diagnostics_blueprint(
            WriteFailureBoard(), doctor_report=doctor_report, run_doctor=run_doctor
        )
    )
    return app


def test_the_tab_reads_the_existing_routes(tmp_path):
    state = {"report": None}

    def rerun():
        state["report"] = doctor.run_doctor([str(tmp_path)], workspace=tmp_path)
        return state["report"]

    client = _blueprint_app(lambda: state["report"], rerun).test_client()
    assert client.get("/api/diagnostics/doctor").status_code == 404
    report = client.post("/api/diagnostics/doctor").get_json()
    assert report["version"] == doctor.REPORT_VERSION and "checks" in report
    assert client.get("/api/diagnostics/doctor").get_json()["checks"] == report["checks"]


def test_no_new_diagnostics_route_was_added():
    app = _blueprint_app(dict, dict)
    routes = sorted(
        (min(rule.methods - {"HEAD", "OPTIONS"}), rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static"
    )
    assert routes == [
        ("GET", "/api/diagnostics/doctor"),
        ("GET", "/api/diagnostics/write-failures"),
        ("POST", "/api/diagnostics/doctor"),
        ("POST", "/api/diagnostics/write-failures/<card_id>/dismiss"),
        ("POST", "/api/diagnostics/write-failures/<card_id>/retry"),
    ]


@pytest.mark.parametrize("path", ["diagnostics_api.py"])
def test_the_blueprint_stays_free_of_file_writes(path):
    text = (ROOT / "src/stiracore/observability" / path).read_text(encoding="utf-8")
    assert not re.search(r"open\(|write_text|\.write\(", text)
