#!/usr/bin/env python3
"""Inspect an extracted live rootfs without booting it or modifying partitions."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deb_dev_builder.core.installer_audit import audit_calamares
from deb_dev_builder.core.runtime_config import load_runtime

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument("--policy-json", type=json.loads)
args = parser.parse_args()
print(json.dumps(audit_calamares(args.root, args.policy_json or load_runtime()["build"]["installer_audit"]), indent=2))
