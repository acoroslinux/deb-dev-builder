"""Machine-readable build evidence; contains selected public fields only."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .package_policy import installed_packages


def file_digest(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_build_report(artifact, config, runtime, root, mode, toolchain=None):
    policy = runtime["build"]["report"]
    if not policy["enabled"]:
        return
    artifact = Path(artifact)
    versions = {}
    if mode != "mock" and toolchain is not None:
        for command in policy["tool_commands"]:
            try:
                result = toolchain.run_in_build_host(command, check=False, capture_output=True, text=True)
                versions[command[0]] = {"exit_code": result.returncode, "output": ((result.stdout or "") + (result.stderr or ""))[:4096]}
            except (OSError, TypeError) as exc:
                versions[command[0]] = {"error": str(exc)}
    inventory = installed_packages(root)
    report = {
        "schema_version": 1,
        "artifact": {"name": artifact.name, "bytes": artifact.stat().st_size, "sha256": file_digest(artifact)},
        "mode": mode,
        "source_date_epoch": runtime["build"]["reproducibility"]["source_date_epoch"],
        "configuration": {key: config[key] for key in policy["configuration_fields"] if key in config},
        "packages": inventory,
        "system_packages": config.get("package_inventory_system", {}),
        "tools": versions,
        "validation": {"artifact_created": True, "boot_tested": False, "installation_tested": False},
    }
    destination = artifact.with_name(artifact.name + policy["suffix"])
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=artifact.parent,
                                         prefix=".build-report-", delete=False) as stream:
            pending = Path(stream.name)
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(pending, destination)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
