"""Local Debian Installer components and live-image preseeding."""

from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
from pathlib import Path
import shutil
import shlex
import urllib.request
import re
import json

from .path_utils import resolve_from_project
from .runtime_config import load_runtime, live_user_settings, installer_storage_packages
from .initrd_editor import Entry, archive_path, customize_initrd, unpack_initrd
from .installer_lifecycle import target_header, rooted, identity_lines, live_cleanup_paths, copied_target_layout_script, target_sysfs_scope
from .installer_autologin import autologin_entries
from .installer_crypto import select_early_unlock, validate_crypto_content, validate_keyfile_content
from .installer_locale import locale_entries
from .installer_grub import grub_entries
from .installer_source import control_fields, select_udeb_records, installer_mirror, installer_udeb_suite, init_preseed, offline_package_sets
from .installer_conffiles import ucf_preservation_hooks
from .installer_cdrom import target_cdrom_hooks


def preseed_archive(content, extra_files=()):
    """A small newc archive suitable for appending to a Linux initramfs."""
    archive = bytearray()
    directories = set()
    for name, _, _ in extra_files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Invalid initramfs include path: {name}")
        directories.update(parent.as_posix() for parent in path.parents if parent != Path("."))
    entries = [(name, b"", 0o40755) for name in sorted(directories, key=lambda name: (name.count("/"), name))]
    for name, data, mode in [*entries, ("preseed.cfg", content.encode(), 0o100644), *extra_files, ("TRAILER!!!", b"", 0)]:
        encoded = name.encode() + b"\0"
        fields = [0, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
        archive.extend(b"070701" + "".join(f"{value:08x}" for value in fields).encode())
        archive.extend(encoded)
        archive.extend(b"\0" * (-len(archive) % 4))
        archive.extend(data)
        archive.extend(b"\0" * (-len(archive) % 4))
    return gzip.compress(bytes(archive), mtime=0)


def apt_source_hooks(target, policy):
    """Temporarily isolate copied APT sources while d-i registers the CD.

    Native apt-setup rewrites sources.list, but leaves sources.list.d intact.
    The live mount URI and inherited network sources must not participate in
    target package operations. Preserve normal sources for the installed OS.
    """
    paths = {}
    for key in ("source_file", "main_source_file", "sources_directory", "installer_backup_directory", "restored_marker"):
        relative = Path(policy[key])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid offline repository path: {relative}")
        paths[key] = Path(target) / relative
    backup = paths["installer_backup_directory"]
    parts = paths["sources_directory"]
    if backup == parts or parts in backup.parents:
        raise ValueError("Installer APT backup must be outside sources.list.d")
    variables = "\n".join([
        "live_source=" + shlex.quote(str(paths["source_file"])),
        "main_source=" + shlex.quote(str(paths["main_source_file"])),
        "source_parts=" + shlex.quote(str(parts)),
        "source_backup=" + shlex.quote(str(backup)),
        "source_restored=" + shlex.quote(str(paths["restored_marker"])),
    ])
    prepare = variables + '''
rm -f -- "$live_source"
rm -f -- "$source_restored"
if [ ! -e "$source_backup/.prepared" ]; then
    mkdir -p "$source_backup/parts"
    if [ -e "$main_source" ] || [ -L "$main_source" ]; then
        mv -- "$main_source" "$source_backup/main.list"
    fi
    for source in "$source_parts"/*.list "$source_parts"/*.sources; do
        if [ -e "$source" ] || [ -L "$source" ]; then
            mv -- "$source" "$source_backup/parts/"
        fi
    done
    touch "$source_backup/.prepared"
fi
'''
    finish = variables + '''
if [ -e "$source_backup/.prepared" ]; then
    rm -f -- "$main_source"
    mkdir -p "$source_parts"
    if [ -e "$source_backup/main.list" ] || [ -L "$source_backup/main.list" ]; then
        mv -- "$source_backup/main.list" "$main_source"
    fi
    for source in "$source_backup/parts"/*; do
        if [ -e "$source" ] || [ -L "$source" ]; then
            mv -- "$source" "$source_parts/"
        fi
    done
    rm -f -- "$source_backup/.prepared"
    rmdir "$source_backup/parts" "$source_backup" 2>/dev/null || true
    mkdir -p "$(dirname "$source_restored")"
    touch "$source_restored"
fi
rm -f -- "$live_source"
'''
    return prepare, finish


def target_boot_hook(target, storage, packages, *, package_operations=True, regenerate=True, failure_code=10, identity=None, required_markers=(), plymouth=None):
    """Fail closed if the copied system cannot unlock/activate its root volume.

    Runs after native live-package purge and autoremove, before the offline CD
    source is restored. All paths and commands come from the storage policy.
    """
    if target is not None and (target == "/" or not target.startswith("/") or ".." in Path(target).parts):
        raise ValueError("Invalid installer target root")
    for value in (storage["initramfs_glob"], storage["cryptsetup_config"]["path"],
                  storage["crypttab"], storage["crypttab_initramfs_path"],
                  *(path for paths in storage["required_initramfs_paths"].values() for path in paths),
                  *(path for paths in storage["initramfs_path_aliases"].values() for path in paths)):
        archive_path(value)
    header = target_header(target, failure_code)
    lines = [*header, "export DEBIAN_FRONTEND=noninteractive"]
    for marker in required_markers:
        if isinstance(marker, list):
            lines.append("{ " + " || ".join("test -f " + rooted(path) for path in marker) + "; }")
        else:
            lines.append("test -f " + rooted(marker))
    lines.extend(['mkdir -p "$(dirname ' + rooted(storage["validation_log"]) + ')"',
                  r"printf '%s\n' 'START installed target validation' >> " + rooted(storage["validation_log"])])
    if packages and package_operations:
        lines.extend([
            "chroot \"$target\" " + shlex.join(["apt-get", "--yes", "--no-remove", "install", *packages]),
            "chroot \"$target\" " + shlex.join(["apt-mark", "manual", *packages]),
        ])
    conf = storage["cryptsetup_config"]
    pattern = "".join(part if part in {"*", "?"} else shlex.quote(part)
                      for part in re.split(r"([*?])", storage["initramfs_glob"]) if part)
    if identity is not None:
        lines.extend(identity_lines(identity))
    if regenerate:
        lines.extend(select_early_unlock(storage))
        lines.extend([
        "crypto_conf=" + rooted(conf["path"]),
        'mkdir -p "$(dirname "$crypto_conf")"',
        "if ! grep -Fqx -- " + shlex.quote(conf["line"]) + ' "$crypto_conf" 2>/dev/null; then',
        r"    printf '\n%s\n' " + shlex.quote(conf["line"]) + ' >> "$crypto_conf"',
        "fi",
        "chroot \"$target\" " + shlex.join(storage["initramfs_update"]),
        ])
    lines.extend([
        "found=false",
        'for image in "$target"/' + pattern + "; do",
        '    [ -f "$image" ] || continue',
    ])
    excluded = ["".join(part if part in {"*", "?"} else shlex.quote(part)
                        for part in re.split(r"([*?])", value) if part)
                for value in storage["initramfs_excludes"]]
    if excluded:
        lines.append('    case "${image##*/}" in ' + '|'.join(excluded) + ') continue ;; esac')
    lines.extend([
        "    found=true",
        '    guest_image="/${image#"$target"/}"',
        "    listing=$(chroot \"$target\" " + shlex.join(storage["initramfs_list"]) + ' "$guest_image")',
    ])
    for package, paths in storage["required_initramfs_paths"].items():
        if package in packages:
            for path in paths:
                patterns = shlex.join([argument for value in [path, *storage["initramfs_path_aliases"].get(path, [])]
                                       for argument in ("-e", value)])
                lines.extend([
                    r"    if ! printf '%s\n' " + '\"$listing\" | grep -Fxq ' + patterns + "; then",
                    "        echo " + shlex.quote(f"Installed initramfs lacks {path}") + ' >&2; exit 1',
                    "    fi",
                ])
    if plymouth is not None:
        lines.append('    if [ -x ' + rooted(plymouth['binary']) + ' ] && [ -f ' + rooted(plymouth['directory'] + '/' + plymouth['name'] + '.plymouth') + ' ]; then')
        for path in plymouth['required_initramfs_paths']:
            patterns = shlex.join([arg for value in [path, *plymouth['initramfs_path_aliases'].get(path, [])] for arg in ('-e', value)])
            lines.extend([r'''        printf '%s\n' "$listing" | grep -Fxq ''' + patterns + ' || { echo ' + shlex.quote('Installed initramfs lacks Plymouth file: ' + path) + ' >&2; exit 1; }'])
        for pattern in plymouth['required_initramfs_patterns']:
            lines.append(r'''        printf '%s\n' "$listing" | grep -Eq -- ''' + shlex.quote(pattern) + ' || { echo "Installed initramfs lacks a Plymouth plugin" >&2; exit 1; }')
        lines.append('    fi')
    lines.extend([
        "    if [ -f " + rooted(storage["crypttab"]) + " ] && "
        + "grep -Eq '^[[:space:]]*[^#[:space:]]' " + rooted(storage["crypttab"]) + "; then",
        r"        if ! printf '%s\n' " + '\"$listing\" | grep -Fxq -- ' + shlex.quote(storage["crypttab_initramfs_path"]) + "; then",
        "            echo 'Installed initramfs lacks encrypted-volume configuration' >&2; exit 1",
        "        fi",
        *validate_crypto_content(storage),
        *validate_keyfile_content(storage),
        "    fi",
        "done",
        '[ "$found" = true ] || { echo "No installed initramfs found" >&2; exit 1; }',
    ])
    for path in storage["required_boot_files"]:
        lines.append('test -s ' + rooted(path) + ' || { echo ' + shlex.quote(f"Installed boot file missing: {path}") + ' >&2; exit 1; }')
    forbidden = " || ".join('$i == ' + json.dumps(option) for option in storage["forbidden_kernel_options"])
    program = '$1 ~ /^linux(efi|16)?$/ { seen=1; for (i=2;i<=NF;i++) if (' + (forbidden or "0") + ') exit 1 } END { if (!seen) exit 1 }'
    lines.append('awk ' + shlex.quote(program) + ' ' + rooted(storage["grub_config"]) + ' || { echo "Invalid installed GRUB kernel entries" >&2; exit 1; }')
    lines.append(r"printf '%s\n' 'OK installed target validation' >> " + rooted(storage["validation_log"]))
    if regenerate:
        lines = [*header, *target_sysfs_scope(lines[len(header):])]
    return ("\n".join(lines) + "\n").encode()


class LiveInstallerMedia:
    def __init__(self, config, policy):
        self.config = config
        self.policy = policy
        self.suite = config["suite"]
        self.arch = config.get("dpkg_arch", config.get("architecture"))
        self.mirror = installer_mirror(config, policy)

    def _pool_path(self, fields, filename, pool):
        if Path(pool).is_absolute() or ".." in Path(pool).parts:
            raise ValueError("Invalid installer pool directory")
        section = fields.get("Section", "")
        component = section.split("/", 1)[0] if "/" in section else self.policy["base_components"][0]
        source = fields.get("Source", fields["Package"]).split()[0]
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", value) or value in {".", ".."} for value in (component, source)):
            raise ValueError("Invalid package component or source name")
        prefix = source[:4] if source.startswith("lib") else source[0]
        return Path(pool) / component / prefix / source / filename, component

    @staticmethod
    def _archive_tool(toolchain, staging, arguments, output):
        if toolchain is None:
            raise ValueError("Installer repository metadata requires the isolated toolchain")
        command = f"cd {shlex.quote(str(staging))} && {shlex.join(['apt-ftparchive', *arguments])} > {shlex.quote(str(output))}"
        toolchain.run_in_build_host(["/bin/sh", "-c", command], check=True)

    def prepare(self, staging, toolchain):
        """Supply cdrom-retriever with local udebs, including live-installer."""
        package_sets = offline_package_sets(self.config, self.policy)
        required = {package for group in package_sets for package in group}
        if package_sets:
            required.update(installer_storage_packages(self.config))
        repository = Path(self.config.get("offline_repo_dir") or staging / "absent-repo")
        index_file = repository / "Packages"
        components = set(self.policy["base_components"])
        if required:
            if not index_file.is_file():
                raise ValueError("Live Debian Installer requires a prepared offline package repository")
            indexed = [control_fields(paragraph) for paragraph in index_file.read_text().strip().split("\n\n") if paragraph.strip()]
            missing = required - {fields.get("Package") for fields in indexed}
            if missing:
                raise ValueError("Offline installer repository is missing packages: " + ", ".join(sorted(missing)))
            for fields in indexed:
                relative = Path(fields["Filename"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"Unsafe offline repository filename: {relative}")
                archive = repository / relative
                if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != fields["SHA256"]:
                    raise ValueError(f"Offline repository package missing or corrupt: {relative}")
                destination, component = self._pool_path(fields, archive.name, self.policy["deb_pool"])
                components.add(component)
                (staging / destination).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archive, staging / destination)
        udeb_suite = installer_udeb_suite(self.config, self.policy)
        index_path = self.policy["udeb_index"].format(suite=udeb_suite, architecture=self.arch)
        bundle_path = self.config.get("_installer_bundle")
        bundle = None
        if bundle_path:
            from .installer_bundle import load_bundle
            bundle_path = Path(bundle_path)
            bundle = load_bundle(bundle_path)
            index = (bundle_path.parent / "Packages").read_text()
        else:
            with urllib.request.urlopen(f"{self.mirror}/{index_path}", timeout=self.policy["download_timeout"]) as response:
                index = gzip.decompress(response.read()).decode()
        # The module directory in the original initrd identifies the actual
        # installer kernel, including architectures whose Image header has no
        # printable version. Never select modules for a different ABI.
        releases = set()
        initrd = staging / "install/initrd.gz"
        if initrd.is_file():
            for name in unpack_initrd(initrd.read_bytes()):
                match = re.match(r"^(?:usr/)?lib/modules/([^/]+)(?:/|$)", name)
                if match:
                    releases.add(match.group(1))
        if len(releases) > 1:
            raise ValueError("Installer initrd contains multiple kernel releases")
        release = next(iter(releases), None)
        if bundle and release != bundle["kernel"]:
            raise ValueError("Staged installer kernel does not match the frozen bundle")
        paragraphs = select_udeb_records(index, release, self.policy)
        cache = resolve_from_project(self.policy["cache_directory"].format(suite=self.suite, architecture=self.arch))
        if not bundle:
            cache.mkdir(parents=True, exist_ok=True)

        def download(entry):
            _, fields = entry
            relative = Path(fields["Filename"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe installer package path: {relative}")
            expected = fields["SHA256"]
            if bundle:
                cached = bundle_path.parent / bundle["packages"][fields["Package"]]
                if hashlib.sha256(cached.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Frozen installer package checksum mismatch: {relative}")
            else:
                cached = cache / relative.name
            if not bundle and (not cached.is_file() or hashlib.sha256(cached.read_bytes()).hexdigest() != expected):
                pending = cached.with_suffix(".partial")
                try:
                    with urllib.request.urlopen(f"{self.mirror}/{relative.as_posix()}", timeout=self.policy["download_timeout"]) as response, pending.open("wb") as output:
                        shutil.copyfileobj(response, output)
                    if hashlib.sha256(pending.read_bytes()).hexdigest() != expected:
                        raise ValueError(f"Installer package checksum mismatch: {relative}")
                    pending.replace(cached)
                finally:
                    pending.unlink(missing_ok=True)
            relative, _ = self._pool_path(fields, relative.name, self.policy["udeb_pool"])
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, destination)

        with ThreadPoolExecutor(max_workers=self.policy["download_workers"]) as executor:
            list(executor.map(download, paragraphs))
        local_index = self.policy["udeb_index"].format(suite=self.suite, architecture=self.arch)
        index_dir = staging / Path(local_index).parent
        index_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for paragraph, fields in paragraphs:
            relative, _ = self._pool_path(fields, Path(fields["Filename"]).name, self.policy["udeb_pool"])
            records.append("\n".join("Filename: " + relative.as_posix() if line.startswith("Filename: ") else line for line in paragraph.splitlines()))
        content = "\n\n".join(records) + "\n"
        (index_dir / "Packages").write_text(content)
        (index_dir / "Packages.gz").write_bytes(gzip.compress(content.encode(), mtime=0))
        # Publish the resolved offline dependency closure for cdrom-retriever
        # and target APT, including the GRUB metapackages (not just *-bin).
        for component in sorted(components):
            binary = staging / "dists" / self.suite / component / f"binary-{self.arch}"
            binary.mkdir(parents=True, exist_ok=True)
            pool = Path(self.policy["deb_pool"]) / component
            if (staging / pool).exists():
                self._archive_tool(toolchain, staging, ["packages", pool.as_posix()], binary / "Packages")
            else:
                (binary / "Packages").write_text("")
            (binary / "Packages.gz").write_bytes(gzip.compress((binary / "Packages").read_bytes(), mtime=0))
            relative = self.policy["component_release_path"].format(suite=self.suite, component=component, architecture=self.arch)
            with urllib.request.urlopen(f"{self.mirror}/{relative}", timeout=self.policy["download_timeout"]) as response:
                release = response.read()
            fields = control_fields(release.decode())
            if fields.get("Architecture") != self.arch or fields.get("Component") != component:
                raise ValueError(f"Invalid component Release metadata: {relative}")
            (binary / "Release").write_bytes(release)
        release_root = staging / "dists" / self.suite
        context = {"suite": self.suite, "architecture": self.arch, "components": " ".join(sorted(components))}
        options = []
        for key, value in self.policy["release_fields"].items():
            options.extend(["-o", f"APT::FTPArchive::Release::{key}={value.format(**context)}"])
        self._archive_tool(toolchain, staging, [*options, "release", "dists/" + self.suite], release_root / "Release")
        disk = staging / ".disk"
        disk.mkdir(exist_ok=True)
        (disk / "base_installable").touch()
        (disk / "cd_type").write_text(self.policy["disk_type"] + "\n")
        (disk / "base_components").write_text("\n".join(self.policy["base_components"]) + "\n")
        (disk / "udeb_include").write_text("\n".join(self.policy["live_modules"]) + "\n")
        live = staging / "live"
        live.mkdir(exist_ok=True)
        fallback = self.policy.get("live_remove_packages", load_runtime(self.config)["build"]["packages"]["live_only"])
        (live / "filesystem.packages-remove").write_text("\n".join(self.config.get("live_remove_packages", fallback)) + "\n")
        # cdrom-detect and APT may use distribution aliases on removable media.
        for alias in ("stable", "testing", "unstable", "sid"):
            target = staging / "dists" / alias
            if not target.exists() and not target.is_symlink():
                target.symlink_to(self.suite)
        debian = staging / "debian"
        if not debian.exists() and not debian.is_symlink():
            debian.symlink_to(".")

    def embed_preseed(self, install_dir, extra="", generated_extra=(), source_root=None):
        content = extra + "\n" + "\n".join(self.policy["live_preseed"]) + "\n" + init_preseed(self.config, self.policy)
        runtime = load_runtime(self.config)
        target = self.policy["target_root"]
        if target == "/" or not target.startswith("/") or ".." in Path(target).parts:
            raise ValueError("Invalid live installer target root")
        user = live_user_settings(self.config, runtime)["name"]
        if user == "root" or not user or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in user):
            raise ValueError("Invalid live user in installer cleanup")
        calamares = runtime["customization"]["calamares"]
        paths = live_cleanup_paths(runtime)
        paths.extend(calamares[key] for key in ("autostart_launcher", "helper_script", "application_launcher", "polkit_rule"))
        commands = ["#!/bin/sh", "set -eu", f"test -d {shlex.quote(target + '/etc')}"]
        commands.append(f"if chroot {shlex.quote(target)} getent passwd {shlex.quote(user)} >/dev/null; then chroot {shlex.quote(target)} userdel --remove -- {shlex.quote(user)}; fi")
        for path in paths:
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError(f"Invalid live cleanup path: {path}")
            commands.append("rm -f -- " + shlex.quote(str(Path(target) / path)))
        marker = rooted(self.policy["cleanup_marker"])
        # The live-installer runner ignores hook failures. Record successful
        # cleanup so the final gate can refuse to finish after such a failure.
        commands.extend(['target=' + shlex.quote(target),
                         'mkdir -p "$(dirname ' + marker + ')"', 'touch -- ' + marker])
        apt_config = self.policy["offline_apt_config"]
        apt_path = Path(apt_config["path"])
        if apt_path.is_absolute() or ".." in apt_path.parts:
            raise ValueError("Invalid offline APT configuration path")
        target_apt_path = Path(target) / apt_path
        prepare_sources, restore_sources = apt_source_hooks(target, runtime["customization"]["offline_repository"])
        prepare_ucf, restore_ucf = ucf_preservation_hooks(target, self.policy["ucf_preservation"])
        prepare_cdrom, release_cdrom = target_cdrom_hooks(target, self.policy["target_cdrom"])
        apt_prepare = "\n".join([
            "#!/bin/sh", "set -eu", f"test -d {shlex.quote(target + '/etc')}",
            "mkdir -p -- " + shlex.quote(str(target_apt_path.parent)),
            "printf '%s' " + shlex.quote(apt_config["content"]) + " > " + shlex.quote(str(target_apt_path)),
            prepare_sources,
            prepare_ucf,
            prepare_cdrom,
        ]).encode()
        script = ("\n".join(commands) + "\n").encode()
        finish = ("\n".join(target_header(target, 10)) + "\n" + restore_sources + "\n" + restore_ucf + "\nrm -f -- " + shlex.quote(str(target_apt_path)) + "\n").encode()
        generated = [Entry("preseed.cfg", content.encode(), 0o100644), *autologin_entries(self.config, runtime), *locale_entries(self.config, runtime), *grub_entries(self.config, runtime, regenerate=False), *generated_extra]
        layout_markers = []
        if source_root is not None:
            generated.append(Entry(self.policy['layout_hook'], copied_target_layout_script(source_root, target, self.policy), 0o100755))
            layout_markers.append(self.policy['layout_marker'])
        media_markers = []
        if release_cdrom:
            generated.append(Entry(self.policy["target_cdrom"]["release_hook"],
                                   ("\n".join(target_header(target, 10)) + "\n" + release_cdrom).encode(), 0o100755))
            media_markers.append(self.policy["target_cdrom"]["state_directory"] + "/.ready")
        generated.extend(Entry(name, data, mode) for name, data, mode in [
            (self.policy["offline_apt_prepare_hook"], apt_prepare, 0o100755),
            (self.policy["cleanup_hook"], script, 0o100755),
            (self.policy["offline_apt_cleanup_hook"], finish, 0o100755),
            (self.policy["target_boot_hook"], target_boot_hook(target, runtime["build"]["installer_storage"],
                                                             installer_storage_packages(self.config, runtime),
                                                             identity=runtime["customization"]["identity"],
                                                             plymouth=runtime["customization"]["plymouth"],
                                                             required_markers=[self.policy["cleanup_marker"], *layout_markers, *media_markers,
                                                                 [runtime["customization"]["offline_repository"]["installer_backup_directory"] + "/.prepared",
                                                                  runtime["customization"]["offline_repository"]["restored_marker"]],
                                                                 [self.policy["ucf_preservation"]["backup_directory"] + "/.ready",
                                                                  self.policy["ucf_preservation"]["restored_marker"]]]), 0o100755),
        ])
        for frontend, initrd in (("text", install_dir / "initrd.gz"), ("gtk", install_dir / "gtk" / "initrd.gz")):
            if initrd.is_file():
                customize_initrd(initrd, self.policy["customization"], frontend, generated=generated,
                                 epoch=runtime["build"]["reproducibility"]["source_date_epoch"])
