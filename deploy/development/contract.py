"""Shared validation for development-preview target manifests."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import NoReturn
from urllib.parse import urlsplit

SLUG = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
HOSTNAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
FORBIDDEN_HOSTS = {"api.soyrocoto.com", "soyrocoto.com", "staging.soyrocoto.com"}
RESERVED_ROOTS = {"rocoto", "demo", "staging", "albaranes-bridge"}
RESERVED_SERVICES = {"rocoto-staging", "rocoto-kit-runtime", "albaranes-bridge"}
RESERVED_USERS = {"rocoto-demo"}
SECRET_PARTS = {".env", "secret", "secrets", "credential", "credentials", "token",
                "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def refuse(message: str) -> NoReturn:
    raise ValueError(message)


def relative_path(value: object, field: str) -> PurePosixPath:
    path = PurePosixPath(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        refuse(f"{field} must be a normalized repository-relative path")
    return path


def secret_path(path: PurePosixPath) -> bool:
    for part in path.parts:
        lowered = part.lower()
        leaf = PurePosixPath(lowered)
        if (lowered in SECRET_PARTS or lowered.startswith(".env.")
                or leaf.suffix in SECRET_SUFFIXES
                or (leaf.suffix == ".json" and leaf.stem in SECRET_PARTS)):
            return True
    return False


NATIVE_EXECUTOR_RUNS = "/run/rocoto-native-broker/runs"


def runtime_write_paths(runtime: dict) -> list[str]:
    """Named optional capability; manifests cannot add arbitrary writable paths."""
    paths = [runtime["statePath"]]
    if runtime.get("nativeExecutor") is True:
        paths.append(NATIVE_EXECUTOR_RUNS)
    return paths


def validate_manifest(value: object) -> dict:
    if not isinstance(value, dict):
        refuse("target manifest must be a JSON object")
    required = {"schema", "name", "logicalEnvironment", "physicalHost", "sourceOwner",
                "publicOrigin", "buildCommand", "artifacts", "runtime"}
    if set(value) != required or value.get("schema") != 1:
        refuse("target manifest schema or fields do not match version 1")
    if not SLUG.fullmatch(str(value["name"])):
        refuse("target name must be a lowercase slug")
    if value["logicalEnvironment"] != "development/demo":
        refuse("preview target must declare logicalEnvironment development/demo")
    if not isinstance(value["physicalHost"], str) or not HOSTNAME.fullmatch(value["physicalHost"]):
        refuse("preview target must declare one valid Tailscale hostname")
    owner = relative_path(value["sourceOwner"], "sourceOwner")
    command = value["buildCommand"]
    if (not isinstance(command, list) or any(not isinstance(part, str) or not part or "\x00" in part
                                             for part in command)):
        refuse("buildCommand must be an argv string list")
    origin = urlsplit(str(value["publicOrigin"]))
    if (origin.scheme != "https" or not origin.hostname or origin.path not in ("", "/")
            or origin.query or origin.fragment or origin.username or origin.hostname in FORBIDDEN_HOSTS):
        refuse("publicOrigin must be a dedicated HTTPS development-preview origin")
    runtime = value["runtime"]
    runtime_fields = {"service", "user", "installPath", "statePath", "environmentFile",
                      "workingDirectory", "execStart", "healthUrl"}
    if (not isinstance(runtime, dict) or set(runtime) - {"nativeExecutor"} != runtime_fields
            or "nativeExecutor" in runtime and type(runtime["nativeExecutor"]) is not bool):
        refuse("runtime fields do not match version 1")
    if (not SLUG.fullmatch(str(runtime["service"])) or runtime["service"] in RESERVED_SERVICES
            or not str(runtime["service"]).startswith("rocoto-")):
        refuse("runtime.service must name a dedicated rocoto- preview service")
    if (not SLUG.fullmatch(str(runtime["user"])) or runtime["user"] in RESERVED_USERS
            or not str(runtime["user"]).startswith("rocoto-")):
        refuse("runtime.user must name a dedicated rocoto- preview identity")
    install = PurePosixPath(str(runtime["installPath"]))
    state = PurePosixPath(str(runtime["statePath"]))
    if (not install.is_absolute() or ".." in install.parts or install.parts[:2] != ("/", "srv")
            or len(install.parts) < 4 or install.name != "current" or install.parts[2] in RESERVED_ROOTS):
        refuse("runtime.installPath must be a dedicated absolute /srv/.../current path")
    if (not state.is_absolute() or ".." in state.parts or state.parts[:3] != ("/", "var", "lib")
            or len(state.parts) < 4 or state.parts[3] in RESERVED_ROOTS):
        refuse("runtime.statePath must be a dedicated absolute /var/lib/... path")
    environment_file = PurePosixPath(str(runtime["environmentFile"]))
    working_directory = PurePosixPath(str(runtime["workingDirectory"]))
    exec_start = runtime["execStart"]
    if (not environment_file.is_absolute() or ".." in environment_file.parts
            or environment_file.parts[:3] != ("/", "etc", "rocoto")
            or len(environment_file.parts) < 4 or environment_file.suffix != ".env"
            or not working_directory.is_absolute() or ".." in working_directory.parts
            or working_directory.parts[:len(install.parts)] != install.parts):
        refuse("runtime environment and working directory must use commissioned paths")
    if (not isinstance(exec_start, list) or not exec_start
            or any(not isinstance(part, str) or not part or re.search(r"[\s;]", part)
                   for part in exec_start)
            or not any(part.startswith(str(install) + "/") for part in exec_start)):
        refuse("runtime.execStart must be unambiguous argv that executes published source")
    health = urlsplit(str(runtime["healthUrl"]))
    if (health.scheme != "http" or health.hostname not in {"127.0.0.1", "localhost"}
            or health.username or health.fragment):
        refuse("runtime.healthUrl must be a loopback HTTP URL")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        refuse("artifacts must name at least one source")
    destinations: set[str] = set()
    has_build_artifact = False
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"source", "destination", "kind"}:
            refuse("each artifact must contain source, destination and kind")
        if item["kind"] not in {"source", "build"}:
            refuse("artifact kind must be source or build")
        has_build_artifact = has_build_artifact or item["kind"] == "build"
        source = relative_path(item["source"], "artifact.source")
        destination = relative_path(item["destination"], "artifact.destination")
        if secret_path(source) or secret_path(destination):
            refuse("artifact source and destination must exclude secret-bearing paths")
        if destination.parts[0] == ".release.json":
            refuse(".release.json is reserved for verified release metadata")
        if item["kind"] == "source" and source.parts[:len(owner.parts)] != owner.parts:
            refuse("source artifacts must be owned by sourceOwner")
        if str(destination) in destinations:
            refuse("artifact destinations must be unique")
        destinations.add(str(destination))
    if has_build_artifact and not command:
        refuse("build artifacts require a non-empty buildCommand")
    return value
