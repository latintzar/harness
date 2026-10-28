"""Configuration for local history export; the operator issues thread/read only."""
import json

DISABLED_FEATURES = (
    "shell_tool", "apps", "multi_agent", "multi_agent_v2",
    "view_image", "hooks", "plugins", "remote_plugin", "plugin_sharing",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "workspace_dependencies", "memories",
    "skill_mcp_dependency_install", "skill_search", "in_app_local_automation",
    "goals", "code_mode", "code_mode_only", "tool_suggest",
    "request_permissions_tool", "executor_capability_discovery", "deferred_executor",
)
EXPORT_CONFIG = {
    **{"features." + name: False for name in DISABLED_FEATURES},
    "features.skip_host_skill_discovery": True,
    "agents.enabled": False,
    "features.multi_agent_v2.enabled": False,
    "tools.view_image": False,
    "tools.experimental_request_user_input.enabled": False,
    "tools.update_plan.enabled": False,
    "mcp_servers": {},
    "web_search": "disabled",
}


def cli_args(config=None):
    selected = EXPORT_CONFIG if config is None else config
    return [part for key, value in selected.items()
            for part in ("-c", key + "=" + json.dumps(value))]
