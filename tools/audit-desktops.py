#!/usr/bin/env python3
"""Validate desktop profile combinations without building or changing the host.

This checks configuration composition and optionally declared package names in
local APT indexes. It does not solve dependencies or test a running desktop.
Use the VM checklist in docs/desktop-validation.md for runtime tests.
"""
import argparse
import json
import gzip
import lzma
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deb_dev_builder.core.config_loader import ConfigLoader
from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.runtime_config import catalog_document_paths, load_runtime


def package_names(path):
    """Read package and virtual-package names from a local APT index snapshot."""
    opener = lzma.open if path.suffix == '.xz' else gzip.open if path.suffix == '.gz' else open
    names = set()
    with opener(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            if line.startswith('Package: '):
                names.add(line.split(': ', 1)[1].strip())
            elif line.startswith('Provides: '):
                names.update(re.findall(r'(?:^|,)\s*([a-z0-9][a-z0-9+.-]*)', line.split(': ', 1)[1]))
    return names


def main():
    loader = ConfigLoader()
    documents = catalog_document_paths(loader.config_root / "catalog.json")
    desktops = sorted(path.stem for path in (loader.config_root / "desktops").glob("*.json")
                      if path.resolve() not in documents)
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--distro", action="append", help="Repeat for multiple distribution profiles")
    selection.add_argument("--all-distros", action="store_true", help="Include all distribution profiles in configs/system")
    parser.add_argument("--desktop", action="append", choices=desktops)
    parser.add_argument("--architecture", default=load_runtime()["cli"]["architecture"])
    parser.add_argument("--installer", choices=["calamares", "debian-installer"], default="calamares")
    parser.add_argument("--package-index", action="append", default=[], metavar="DISTRO=FILE",
                        help="Check declared packages against local Packages[.gz/.xz] snapshots; repeat for components/backports")
    args = parser.parse_args()
    indexes = {}
    for value in args.package_index:
        distro, separator, filename = value.partition('=')
        if not distro or not separator or not Path(filename).is_file():
            parser.error('--package-index needs DISTRO=existing-file')
        indexes.setdefault(distro, set()).update(package_names(Path(filename)))
    distros = args.distro or [load_runtime()["cli"]["distro"]]
    if args.all_distros:
        distros = sorted(path.stem for path in (loader.config_root / "system").glob("*.json")
                         if path.resolve() not in documents and loader.load_json(path).get("distro"))
    results = []
    for distro in distros:
        for desktop in args.desktop or desktops:
            build = BuildOrchestrator(
                arch=args.architecture, distro=distro, desktop=desktop,
                variant="live", mode="mock", output_format="iso",
                with_calamares=args.installer == "calamares",
                with_debian_installer=args.installer == "debian-installer",
            )
            report = build.validate()
            availability = {"checked": False, "scope": "declared package names, not dependency solving", "missing": []}
            if report['valid'] and distro in indexes:
                availability['checked'] = True
                availability['missing'] = sorted(package for package in build.config['software']
                                                 if package.split('=', 1)[0].split(':', 1)[0] not in indexes[distro])
            results.append({
                "distro": distro, "desktop": desktop, "architecture": build.arch,
                "suite": build.config.get("suite"),
                "init_system": build.init_system, "installer": args.installer,
                "display_manager": build.config.get("display_manager"),
                "session_type": build.config.get("session_type"),
                "configuration_valid": report["valid"], "errors": report["errors"],
                "package_profiles": build.package_profiles,
                "runtime_tested": False,
                "package_availability": availability,
            })
    print(json.dumps({"scope": "configuration and optional local package-index checks", "results": results}, indent=2))
    return 0 if all(row["configuration_valid"] and not row['package_availability']['missing'] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
