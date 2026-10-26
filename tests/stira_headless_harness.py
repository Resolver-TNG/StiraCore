"""The chat server's real entry point on a temporary data directory (#242/#243 E2E).

    python tests/stira_headless_harness.py DATA_DIR

Unlike ``stira_owner_harness.py`` nothing is scripted inside the process: the
agent, the classifier and the Bedrock provider are the server's own. The test
points boto3 at ``tests/fake_bedrock_endpoint.py`` through the environment, so no
cloud call is made. Only the data directory (and the config files in it) differ
from ``scripts/stira_ctl.sh start``; a fresh directory gets the fresh-install
tool rules (local writes are Ask, #269).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dashboard import stira_server
from stiracore.observability.config_api import ConfigStore


def main() -> None:
    data_dir = Path(sys.argv[1]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    stira_server.DATA_DIR = data_dir
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    stira_server.main(app_factory=lambda lc: stira_server.create_app(store, lifecycle=lc))


if __name__ == "__main__":
    main()
