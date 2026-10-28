"""One host-owned capability policy for fresh and resumed native conversations.

Vision enters through authorized attachments and scoped tool results. Native
filesystem readers, personal plugins and executable hooks have separate authority
and are disabled in the customer agent process.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


# unified_exec is normalized on by Codex 0.153.4. shell_tool and the model
# catalogue gate execution; the transport fixture verifies actual dispatch.
DISABLED_FEATURES = (
    "shell_tool", "apps", "multi_agent", "multi_agent_v2",
    "view_image", "hooks", "plugins", "remote_plugin", "plugin_sharing",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "workspace_dependencies", "memories",
    "skill_mcp_dependency_install", "skill_search", "in_app_local_automation",
    "goals", "code_mode", "code_mode_only", "tool_suggest",
    "request_permissions_tool", "executor_capability_discovery", "deferred_executor",
)
NATIVE_CONFIG = {
    **{"features." + name: False for name in DISABLED_FEATURES},
    "features.skip_host_skill_discovery": True,
    "agents.enabled": False,
    "features.multi_agent_v2.enabled": False,
    "tools.view_image": False,
    "tools.experimental_request_user_input.enabled": False,
    "tools.update_plan.enabled": False,
    "mcp_servers": {},
    "web_search": "live",
}


def cli_args(config=None):
    selected = NATIVE_CONFIG if config is None else config
    return [part for key, value in selected.items()
            for part in ("-c", key + "=" + json.dumps(value))]


def thread_params(state, instructions, tools):
    return {"cwd": str(state), "sandbox": "read-only", "approvalPolicy": "never",
            "baseInstructions": instructions, "developerInstructions": instructions,
            "dynamicTools": tools, "config": dict(NATIVE_CONFIG)}


def isolated_thread_params(instructions, tools, *, plugins=None, provider_approvals=False):
    """For a verified gVisor broker process only; never use on the host profile.

    The outer worker owns filesystem/network limits. Omit baseInstructions so
    the pinned Codex runtime supplies its native default instructions. Plugins
    and external capabilities remain separately commissioned host decisions.
    """
    config={"features.skip_host_skill_discovery":True,
        "features.multi_agent":True,"features.multi_agent_v2":True,"agents.enabled":True,
        "features.default_mode_request_user_input":True,
        "features.shell_tool":True,"features.view_image":True,"tools.view_image":True,
        "features.apps":False,"features.plugins":bool(plugins),"features.hooks":False,
        "features.skill_mcp_dependency_install":False,"plugins":plugins or {},
        "features.remote_plugin":False,"features.plugin_sharing":False,
        "features.image_generation":False,"features.computer_use":False,
        "features.browser_use":False,"features.browser_use_external":False,
        "features.browser_use_full_cdp_access":False,"features.request_permissions_tool":False,
        "web_search":"live"}
    return {"cwd":"/workspace","sandbox":"danger-full-access","approvalPolicy":provider_approval_policy() if provider_approvals else "never",
        "developerInstructions":instructions,"config":config,
        "dynamicTools":[tool for tool in tools if tool.get("name") not in {
            "delegate_investigations","wait_investigations","collect_investigations"}]}


def policy_digest(params):
    """Persist permissions with tools so old permissive sessions are migrated."""
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


def startup_policy(executable, state, env=None):
    """Remove native file tools from the installed model catalogue before launch.

    Codex 0.153.4 keeps unified_exec enabled regardless of a normal feature
    override; shell_tool is the execution gate. apply_patch has a separate
    model-catalogue gate. A thread-level model_catalog_json override is a no-op,
    so callers must pass this configuration when starting app-server.
    The installed-runtime exposure test verifies both advertised tools and
    rejection of unsolicited local-tool calls, without a provider request.
    """
    try:
        result = subprocess.run(
            [str(executable), "debug", "models", "--bundled"],
            capture_output=True, text=True, check=True, timeout=15, env=env,
            cwd=str(state),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("The installed Codex model catalogue could not be loaded") from error
    try:
        catalog = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise ValueError("The installed Codex model catalogue is invalid") from error
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list) or not models:
        raise ValueError("The installed Codex model catalogue is unavailable")
    seen = set()
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str) or not model["slug"]:
            raise ValueError("The installed Codex model catalogue has an invalid model")
        if model["slug"] in seen:
            raise ValueError("The installed Codex model catalogue has duplicate models")
        seen.add(model["slug"])
        if "shell_type" not in model or "apply_patch_tool_type" not in model:
            raise ValueError("The installed Codex model catalogue has changed tool fields")
        # Preserve model instructions, context limits, vision and reasoning data.
        model["shell_type"] = "disabled"
        model["apply_patch_tool_type"] = None
        # This host uses ordinary conversational questions; its app-server
        # bridge does not implement the optional asynchronous question channel.
        if "experimental_supported_tools" in model:
            extra = model["experimental_supported_tools"]
            if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
                raise ValueError("The installed Codex model catalogue has invalid extra tools")
            model["experimental_supported_tools"] = [item for item in extra if item not in {
                "request_user_input_async", "send_user_message_async", "send_message_to_user_async"}]

    encoded = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    directory = Path(state) / "native-policy"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    destination = directory / ("models-" + digest + ".json")
    # Atomic publication also covers concurrent independent chat starts.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".models-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {**NATIVE_CONFIG, "model_catalog_json": str(destination)}


def provider_approval_policy():
    """Native MCP approval UI only; outer worker permissions remain unchanged."""
    return {"granular":{"sandbox_approval":False,"rules":False,"mcp_elicitations":True,
        "request_permissions":False,"skill_approval":False}}

# Historical policy callers share the current host effect classification.
from provider_tool_policy import provider_prompt_policy
