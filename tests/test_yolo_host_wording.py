"""ADR 0012 §8b (phase 2): every refusal code a page can receive from a host-confirmed
operation has page wording (ja/en) and is mapped on every page that sends such a request.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from stiracore.observability.scheduler_api import STATUS_CODES
from stiracore.yolo.host_ops import CODES

ROOT = Path(__file__).resolve().parents[1]
HOST_SIDE_ONLY = {"host_operation_decided"}
PAGE_CODES = sorted(set(CODES) - HOST_SIDE_ONLY)
PAGES = {
    "dashboard/static/js/approvals.js": '{code}: "host.code_{code}"',
    "dashboard/static/js/settings_hub.js": '{code}: () => t("host.code_{code}")',
    "dashboard/static/js/schedules_page.js": '{code}: "host.code_{code}"',
    "dashboard/stage/js/approval.js": "{code}: 'host.code_{code}'",
}


def _catalog(lang):
    return json.loads((ROOT / f"dashboard/static/i18n/catalog.{lang}.json").read_text("utf-8"))


def test_every_page_code_has_ja_and_en_wording():
    ja, en = _catalog("ja"), _catalog("en")
    for code in PAGE_CODES:
        key = f"host.code_{code}"
        assert ja.get(key) and en.get(key), key
        assert ja[key] != en[key], key
    for key in (
        "host.cancel",
        "host.waiting_command",
        "host.waiting_dialog",
        "stage.host_confirm_then_retry",
    ):
        assert ja.get(key) and en.get(key), key
    for key in ("host.waiting_command", "host.waiting_dialog"):
        for lang in (ja, en):
            assert set(re.findall(r"{(\w+)}", lang[key])) >= {"code", "seconds"}, key


def test_every_page_that_sends_a_host_confirmed_request_maps_every_code():
    for path, pattern in PAGES.items():
        text = (ROOT / path).read_text("utf-8")
        for code in PAGE_CODES:
            assert pattern.format(code=code) in text, (path, code)


def test_the_scheduler_api_answers_every_code_with_its_status():
    for code, status in CODES.items():
        assert STATUS_CODES[code] == status, code


def test_the_shared_fetch_wrapper_maps_every_host_outcome_to_a_page_code():
    text = (ROOT / "dashboard/static/js/api.js").read_text("utf-8")
    block = text.split("const OUTCOME_CODES = {", 1)[1].split("};", 1)[0]
    mapped = set(re.findall(r'"([a-z_]+)"', block))
    assert mapped <= set(PAGE_CODES), mapped - set(PAGE_CODES)
    states = set(re.findall(r"(\w+):", block))
    assert states == {"denied", "cancelled", "expired", "used", "stale", "unknown"}


def test_every_browser_refusal_is_worded_on_all_four_pages():
    """Review of #432 (minor 4): what ``/confirm`` can answer — ``api.js`` passes exactly the
    codes of ``BROWSER_REFUSALS`` to the page — is worded on every page. ``tool_call_running``
    on /schedules uses the scheduler's own wording (the scheduler API answers it too)."""
    text = (ROOT / "dashboard/static/js/api.js").read_text("utf-8")
    block = text.split("const BROWSER_REFUSALS = new Set([", 1)[1].split("]);", 1)[0]
    codes = set(re.findall(r'"([a-z_]+)"', block))
    assert "tool_call_running" in codes and "host_not_acknowledged" in codes
    own = {
        ("dashboard/static/js/schedules_page.js", "tool_call_running"): (
            '{code}: "schedules.refused_{code}"'
        )
    }
    ja, en = _catalog("ja"), _catalog("en")
    for path, pattern in PAGES.items():
        page = (ROOT / path).read_text("utf-8")
        for code in codes:
            expected = own.get((path, code), pattern).format(code=code)
            assert expected in page, (path, code)
            key = expected.split('"')[1] if '"' in expected else expected.split("'")[1]
            assert ja.get(key) and en.get(key), key
