from stiracore.core.engine import StiraConfig
from stiracore.tools.tool_registry import WRITE_TOOLS


def test_all_registered_write_tools_are_blocked_by_default():
    # Optional imports must not make this safety check pass vacuously.
    assert WRITE_TOOLS, "No write tools registered; invariant was not exercised"

    blocked_names = StiraConfig().blocked_tool_names
    missing_names = set(WRITE_TOOLS) - set(blocked_names)

    assert not missing_names, (
        "Registered write tools missing from StiraConfig.blocked_tool_names: "
        f"{', '.join(sorted(missing_names))}. "
        "Keep the registry and engine write-blocking policy synchronized."
    )
