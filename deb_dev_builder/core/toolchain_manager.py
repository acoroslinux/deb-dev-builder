import os
import json
import platform
import shutil
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from pathlib import PurePosixPath
from typing import Dict, Any, Optional, List, Union
import logging
import hashlib
import re
import tarfile

logger = logging.getLogger("toolchain_manager")

_HOST_ARCH = platform.machine().lower()
_DEVUAN_KEYRING_URL = "https://pkgmaster.devuan.org/devuan/pool/main/d/devuan-keyring/devuan-keyring_2026.01.13_all.deb"
_DEVUAN_KEYRING_SHA256 = "c53429b645bea3a6edd427d70b7fb49b629a99fc9b9915260026a300540400f2"

class ToolchainManagerError(Exception):
    pass

class ToolchainManager:
    """
    Manages an isolated secondary chroot (build_host), containing all
    build and ISO creation tools (mmdebstrap, debootstrap, mksquashfs, grub-mkstandalone, xorriso, mtools, qemu-utils).
    This ensures deb-dev-builder is 100% host distribution agnostic.
    """

    def __init__(
        self,
        workdir_base: Path,
        mode: str = "mock",
        force_isolated: bool = False,
        target_arch: str = "amd64",
        distro: str = "debian-12",
        required_tools: Optional[List[str]] = None,
        toolchain_config: Optional[Dict[str, Any]] = None,
    ):
        self.workdir_base = Path(workdir_base).resolve()
        self.mode = mode.lower()
        self.force_isolated = force_isolated
        self.target_arch = target_arch.lower()
        self.distro = distro
        self.required_tools = required_tools or ["mksquashfs", "xorriso", "grub-mkstandalone", "mcopy", "mmd", "mkfs.vfat"]
        self.toolchain_config = toolchain_config or {}
        self.build_host_dir = self.workdir_base.parent / "build_host"
        # Reusable state lives outside the disposable architecture workdir.
        # Derive this from the supplied workdir so alternate project roots and
        # tests remain self-contained.
        self.cache_dir = self.workdir_base.parents[1] / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.is_mounted = False
        self.use_isolated = False
        self.backend = "mock" if self.mode == "mock" else "isolated-oci"
        self.workdir_mount = None

        from deb_dev_builder.core.path_utils import resolve_from_project
        self.project_root = resolve_from_project(".")
        rel_proj = self.project_root.relative_to("/")
        self.project_mount = self.build_host_dir / rel_proj

    def check_host_tools(self) -> bool:
        """Check if primary ISO packaging tools exist on the host."""
        missing = [tool for tool in self.required_tools if shutil.which(tool) is None]
        if missing:
            logger.info(f"Missing tools on host: {', '.join(missing)}")
            return False
        return True

    def setup(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Simulating build_host setup.")
            return

        requested_backend = self.toolchain_config.get("backend", "isolated-oci")
        if requested_backend == "native":
            raise ToolchainManagerError("Native host toolchains are disabled; use an isolated cached toolchain tarball")
        if requested_backend not in {"auto", "isolated-tarball", "isolated-oci"}:
            raise ToolchainManagerError(f"Unsupported isolated toolchain backend: {requested_backend}")

        self.backend = "isolated-tarball"
        try:
            self.restore_toolchain_tarball()
        except ToolchainManagerError as exc:
            if requested_backend == "isolated-tarball" or not str(exc).startswith("No compatible"):
                raise
            self.backend = "isolated-oci"
            self.bootstrap_from_official_oci()
        self.use_isolated = self._is_bootstrapped()
        if not self.use_isolated:
            raise ToolchainManagerError("Could not prepare the isolated build toolchain")

    def _host_is_compatible(self) -> bool:
        """Native mode is restricted to Debian Trixie hosts for reproducibility."""
        try:
            values = {}
            for line in Path("/etc/os-release").read_text().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value.strip().strip('"')
            return values.get("ID") == "debian" and values.get("VERSION_CODENAME", "") == "trixie"
        except OSError:
            return False

    def restore_toolchain_tarball(self):
        """Restore a verified prebuilt toolchain from an explicit URL or local path."""
        source = os.environ.get("DEB_DEV_BUILDER_TOOLCHAIN_TARBALL") or self.toolchain_config.get("tarball_path")
        url = os.environ.get("DEB_DEV_BUILDER_TOOLCHAIN_URL") or self.toolchain_config.get("tarball_url")
        expected = os.environ.get("DEB_DEV_BUILDER_TOOLCHAIN_SHA256") or self.toolchain_config.get("tarball_sha256")
        if not source:
            for cached in self._cached_toolchain_archives():
                if cached.is_file():
                    source = str(cached)
                    break
        if not source and url:
            source_path = self.cache_dir / "toolchains" / f"build-host-{_HOST_ARCH}.tar.zst"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            if not source_path.exists():
                logger.info("Downloading isolated toolchain tarball from %s", url)
                try:
                    urllib.request.urlretrieve(url, source_path)
                except OSError as exc:
                    raise ToolchainManagerError(f"Could not download toolchain tarball: {exc}") from exc
            source = str(source_path)
        if not source:
            raise ToolchainManagerError(
                "No compatible native toolchain or bootstrap helper found; set "
                "DEB_DEV_BUILDER_TOOLCHAIN_TARBALL and DEB_DEV_BUILDER_TOOLCHAIN_SHA256"
            )
        archive = Path(source)
        if not archive.is_file():
            raise ToolchainManagerError(f"Toolchain tarball not found: {archive}")
        if not expected and archive.with_name(archive.name + ".sha256").is_file():
            expected = archive.with_name(archive.name + ".sha256").read_text().split()[0]
        if not expected:
            raise ToolchainManagerError("Toolchain tarball SHA-256 is required before extraction")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected.strip()):
            raise ToolchainManagerError("Toolchain tarball SHA-256 must be a 64-character hexadecimal digest")
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != expected.lower():
            raise ToolchainManagerError("Toolchain tarball SHA-256 verification failed")
        self.build_host_dir.mkdir(parents=True, exist_ok=True)
        # Debian rootfs archives legitimately contain absolute symlinks such as
        # /var/run -> /run. Reuse the guarded OCI extractor: it permits those
        # links while rejecting path traversal and extraction through an
        # escaping parent directory.
        self._oci_extract_layer(archive)
        if not self._is_bootstrapped():
            raise ToolchainManagerError("Toolchain tarball does not contain a valid build_host")
        cached_archives = {path.resolve() for path in self._cached_toolchain_archives()}
        if archive.resolve() not in cached_archives:
            self.cache_build_host()

    def _cached_toolchain_archives(self) -> list[Path]:
        """Return local build-host caches in preferred order."""
        root = self.cache_dir / "toolchains"
        return [
            root / f"build-host-{_HOST_ARCH}-v2.tar.xz",
            root / f"build-host-{_HOST_ARCH}-v2.tar.zst",
        ]

    @staticmethod
    def _oci_platform(machine: str) -> tuple[str, Optional[str]]:
        """Map Linux machine names to OCI platform descriptors."""
        platforms = {
            "x86_64": ("amd64", None), "amd64": ("amd64", None),
            "i386": ("386", None), "i486": ("386", None), "i586": ("386", None), "i686": ("386", None),
            "aarch64": ("arm64", None), "arm64": ("arm64", None),
            "armv7l": ("arm", "v7"), "armv6l": ("arm", "v6"),
            "ppc64le": ("ppc64le", None), "riscv64": ("riscv64", None), "s390x": ("s390x", None),
        }
        if machine not in platforms:
            raise ToolchainManagerError(f"Unsupported host architecture for OCI bootstrap: {machine}")
        return platforms[machine]

    def install_devuan_keyring(self) -> None:
        """Install a pinned Devuan archive keyring into the isolated build host."""
        if self.mode == "mock":
            return
        if not self.is_mounted:
            raise ToolchainManagerError("Devuan keyring installation requires a mounted isolated build host")
        keyring = self.build_host_dir / "usr" / "share" / "keyrings" / "devuan-archive-keyring.pgp"
        if keyring.is_file():
            return
        cache = self.cache_dir / "keyrings" / "devuan-keyring_2026.01.13_all.deb"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.is_file() or self._sha256(cache) != _DEVUAN_KEYRING_SHA256:
            bundled = self.project_root / "tools" / "keyrings" / cache.name
            if bundled.is_file() and self._sha256(bundled) == _DEVUAN_KEYRING_SHA256:
                shutil.copy2(bundled, cache)
            else:
                partial = cache.with_name(f"{cache.name}.partial")
                partial.unlink(missing_ok=True)
                try:
                    urllib.request.urlretrieve(_DEVUAN_KEYRING_URL, partial)
                except OSError as exc:
                    partial.unlink(missing_ok=True)
                    raise ToolchainManagerError(f"Could not download the pinned Devuan keyring: {exc}") from exc
                if self._sha256(partial) != _DEVUAN_KEYRING_SHA256:
                    partial.unlink(missing_ok=True)
                    raise ToolchainManagerError("Devuan keyring SHA-256 verification failed")
                partial.replace(cache)
            if self._sha256(cache) != _DEVUAN_KEYRING_SHA256:
                cache.unlink(missing_ok=True)
                raise ToolchainManagerError("Devuan keyring SHA-256 verification failed")
        destination = self.build_host_dir / "tmp" / cache.name
        shutil.copy2(cache, destination)
        self.run_in_build_host(["dpkg", "--install", f"/tmp/{cache.name}"])
        destination.unlink(missing_ok=True)
        if not keyring.is_file():
            raise ToolchainManagerError("Devuan keyring package did not install its archive keyring")

    def ensure_devuan_debootstrap_scripts(self, suites: list[str]) -> None:
        """Provide Devuan suite aliases expected by debootstrap."""
        if self.mode == "mock":
            return
        scripts = self.build_host_dir / "usr" / "share" / "debootstrap" / "scripts"
        ceres = scripts / "ceres"
        if not ceres.exists():
            raise ToolchainManagerError("The isolated build host has no debootstrap Ceres script")
        for suite in suites:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite):
                raise ToolchainManagerError(f"Invalid Devuan suite alias: {suite!r}")
            alias = scripts / suite
            if not alias.exists() and not alias.is_symlink():
                alias.symlink_to("ceres")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def cache_build_host(self) -> Path:
        """Store a verified, reusable isolated build host under the project cache."""
        if not self._is_bootstrapped():
            raise ToolchainManagerError("Cannot cache an incomplete build_host")
        archive = self._cached_toolchain_archives()[0]
        archive.parent.mkdir(parents=True, exist_ok=True)
        partial = archive.with_name(f"{archive.name}.partial")
        partial.unlink(missing_ok=True)
        project_mount_root = self.project_mount.relative_to(self.build_host_dir).parts[0]
        excluded_roots = {"proc", "sys", "dev", "run", project_mount_root}
        tar_cmd = ["tar", "--create", "--numeric-owner", "--one-file-system", "--directory", str(self.build_host_dir)]
        tar_cmd.extend(f"--exclude=./{name}" for name in sorted(excluded_roots))
        tar_cmd.append(".")
        with partial.open("wb") as output:
            compressor = subprocess.Popen(
                ["xz", "--threads=0", "--compress", "--stdout", "--check=crc64"],
                stdin=subprocess.PIPE,
                stdout=output,
            )
            try:
                tar_result = subprocess.run(tar_cmd, stdout=compressor.stdin, check=False)
            finally:
                if compressor.stdin:
                    compressor.stdin.close()
            if tar_result.returncode != 0 or compressor.wait() != 0:
                partial.unlink(missing_ok=True)
                raise ToolchainManagerError("Could not create the compressed build-host cache")
        partial.replace(archive)
        checksum = self._sha256(archive)
        archive.with_name(f"{archive.name}.sha256").write_text(
            f"{checksum}  {archive.name}\n", encoding="utf-8"
        )
        logger.info("Cached isolated build host at %s", archive)
        return archive

    def _is_bootstrapped(self) -> bool:
        return (self.build_host_dir / "etc" / "os-release").exists() and (self.build_host_dir / "usr" / "bin" / "xorriso").exists()

    def _is_base_rootfs(self) -> bool:
        return (self.build_host_dir / "etc" / "os-release").is_file() and (self.build_host_dir / "usr" / "bin" / "apt-get").is_file()

    def _oci_request(self, url: str, token: str, accept: Optional[str] = None) -> bytes:
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "deb-dev-builder/1"}
        if accept:
            headers["Accept"] = accept
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as response:
                return response.read()
        except OSError as exc:
            raise ToolchainManagerError(f"Could not download official Debian OCI content: {exc}") from exc

    def _oci_extract_layer(self, archive: Path) -> None:
        root = self.build_host_dir.resolve()
        with tarfile.open(archive, "r:*") as handle:
            # OCI layers frequently store duplicate files as hardlinks whose
            # target appears later in the archive. Extract ordinary entries
            # first so Python's tarfile extractor never depends on member
            # ordering.
            members = list(handle)
            ordered = [m for m in members if not m.islnk()] + [m for m in members if m.islnk()]
            for member in ordered:
                member_name = member.name
                while member_name.startswith("./"):
                    member_name = member_name[2:]
                if member_name in {"", "."}:
                    continue
                relative = PurePosixPath(member_name)
                if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                    raise ToolchainManagerError(f"Unsafe OCI layer member: {member.name!r}")
                destination = root.joinpath(*relative.parts)
                if root not in destination.parents and destination != root:
                    raise ToolchainManagerError(f"OCI layer member escapes build host: {member.name!r}")
                if relative.name.startswith(".wh."):
                    if relative.name == ".wh..wh..opq" and destination.parent.is_dir():
                        for child in destination.parent.iterdir():
                            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
                    elif relative.name != ".wh..wh..opq":
                        target = destination.with_name(relative.name[4:])
                        if target.exists() or target.is_symlink():
                            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
                    continue
                resolved_parent = destination.parent.resolve()
                if root not in resolved_parent.parents and resolved_parent != root:
                    raise ToolchainManagerError(f"OCI layer member follows a symlink outside build host: {member.name!r}")
                # Debian uses absolute symlinks inside its rootfs (for example,
                # alternatives below /etc). Creating such a link is safe; only
                # regular extraction through an escaping parent is forbidden.
                member_filter = tarfile.fully_trusted_filter if member.issym() else tarfile.data_filter
                handle.extract(member, root, filter=member_filter)

    def bootstrap_from_official_oci(self) -> None:
        """Bootstrap a build host from the official Debian OCI image into the workspace."""
        repository = self.toolchain_config.get("oci_repository", "library/debian")
        reference = self.toolchain_config.get("oci_reference", "bookworm")
        registry = self.toolchain_config.get("oci_registry", "https://registry-1.docker.io")
        if not all(isinstance(value, str) and value for value in (repository, reference, registry)):
            raise ToolchainManagerError("Invalid OCI bootstrap configuration")
        token_url = "https://auth.docker.io/token?" + urllib.parse.urlencode({
            "service": "registry.docker.io", "scope": f"repository:{repository}:pull",
        })
        try:
            with urllib.request.urlopen(token_url) as response:
                token = json.loads(response.read().decode("utf-8"))["token"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise ToolchainManagerError(f"Could not obtain Debian OCI registry token: {exc}") from exc
        index = json.loads(self._oci_request(
            f"{registry}/v2/{repository}/manifests/{reference}", token,
            "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
        ))
        host_arch, host_variant = self._oci_platform(_HOST_ARCH)
        descriptor = next((item for item in index.get("manifests", []) if item.get("platform", {}).get("os") == "linux" and item.get("platform", {}).get("architecture") == host_arch and (host_variant is None or item.get("platform", {}).get("variant") == host_variant)), None)
        if not descriptor or not re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor.get("digest", "")):
            raise ToolchainManagerError(f"Official Debian OCI image has no valid linux/{host_arch} manifest")
        manifest_digest = descriptor["digest"]
        manifest_data = self._oci_request(
            f"{registry}/v2/{repository}/manifests/{manifest_digest}", token,
            "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
        )
        if hashlib.sha256(manifest_data).hexdigest() != manifest_digest.split(":", 1)[1]:
            raise ToolchainManagerError("Official Debian OCI manifest digest verification failed")
        manifest = json.loads(manifest_data)
        from deb_dev_builder.core.path_utils import safe_remove_tree, unmount_all_under
        if self.build_host_dir.exists():
            unmount_all_under(self.build_host_dir)
            safe_remove_tree(self.build_host_dir, allowed_root=self.workdir_base.parent)
        self.build_host_dir.mkdir(parents=True, exist_ok=True)
        layers = self.cache_dir / "oci" / repository.replace("/", "_")
        layers.mkdir(parents=True, exist_ok=True)
        for layer in manifest.get("layers", []):
            digest = layer.get("digest", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ToolchainManagerError("Official Debian OCI layer has an invalid digest")
            path = layers / f"{digest.split(':', 1)[1]}.layer"
            if not path.is_file() or self._sha256(path) != digest.split(":", 1)[1]:
                payload = self._oci_request(f"{registry}/v2/{repository}/blobs/{digest}", token)
                partial = path.with_name(f"{path.name}.partial")
                partial.write_bytes(payload)
                if self._sha256(partial) != digest.split(":", 1)[1]:
                    partial.unlink(missing_ok=True)
                    raise ToolchainManagerError("Official Debian OCI layer digest verification failed")
                partial.replace(path)
            self._oci_extract_layer(path)
        if not self._is_base_rootfs():
            raise ToolchainManagerError("Official Debian OCI image did not provide an APT-capable rootfs")
        temp_dir = self.build_host_dir / "tmp"
        temp_dir.mkdir(exist_ok=True)
        temp_dir.chmod(0o1777)
        resolv = Path("/etc/resolv.conf")
        if resolv.is_file():
            destination = self.build_host_dir / "etc" / "resolv.conf"
            if destination.is_symlink():
                destination.unlink()
            shutil.copy2(resolv, destination)
        self.use_isolated = True
        try:
            self.mount_virtual_fs()
            packages = [
                "mmdebstrap", "debootstrap", "squashfs-tools", "zstd", "xorriso", "grub-common", "grub-pc-bin",
                "grub-efi-amd64-bin", "grub-efi-ia32-bin", "grub-efi-arm64-bin:arm64", "grub-efi-arm-bin:armhf",
                "grub-efi-riscv64-bin:riscv64", "grub-ieee1275-bin:ppc64el", "mtools", "dosfstools", "qemu-utils", "qemu-user-static", "parted",
                "btrfs-progs", "syslinux-utils", "fdisk", "util-linux", "ca-certificates", "xfsprogs", "f2fs-tools",
                "xz-utils", "gzip", "lz4",
            ]
            for architecture in ("arm64", "armhf", "riscv64", "ppc64el"):
                self.run_in_build_host(["dpkg", "--add-architecture", architecture], check=True)
            self.run_in_build_host(["apt-get", "update"], check=True)
            self.run_in_build_host(["apt-get", "install", "-y", *packages], check=True)
        except Exception:
            self.umount_virtual_fs()
            raise
        if not self._is_bootstrapped():
            raise ToolchainManagerError("Isolated Debian build host is missing required build tools")
        self.cache_build_host()

    def _tool_in_build_host(self, tool_binary: str) -> bool:
        return any((self.build_host_dir / relative / tool_binary).exists() for relative in ("usr/bin", "usr/sbin", "bin", "sbin"))

    def bootstrap_build_host(self):
        if self._is_bootstrapped():
            logger.info(f"♻️ Reusing existing isolated build_host environment at: {self.build_host_dir}")
            return

        logger.info(f"🚀 Initializing isolated build environment (build_host) at: {self.build_host_dir}")
        if self.build_host_dir.exists():
            from deb_dev_builder.core.path_utils import safe_remove_tree, unmount_all_under, resolve_from_project
            unmount_all_under(self.build_host_dir)
            safe_remove_tree(self.build_host_dir, allowed_root=resolve_from_project("workdir").parent)
        self.build_host_dir.mkdir(parents=True, exist_ok=True)

        mirror = "http://deb.debian.org/debian"
        suite = "bookworm"

        host_tools = [
            "squashfs-tools", "zstd", "xorriso", "grub-common", "grub-pc-bin",
            "grub-efi-amd64-bin", "grub-efi-ia32-bin", "mtools", "dosfstools", "qemu-utils", "parted", "btrfs-progs",
            "syslinux-utils", "fdisk", "util-linux", "ca-certificates",
            "xfsprogs", "f2fs-tools", "xz-utils", "gzip", "lz4"
        ]

        if shutil.which("mmdebstrap"):
            cmd = [
                "mmdebstrap",
                "--variant=essential",
                f"--include={','.join(host_tools)}",
                suite,
                str(self.build_host_dir),
                mirror,
            ]
        elif shutil.which("debootstrap"):
            cmd = [
                "debootstrap",
                f"--include={','.join(host_tools)}",
                suite,
                str(self.build_host_dir),
                mirror,
            ]
        else:
            raise ToolchainManagerError("Neither mmdebstrap nor debootstrap can create the isolated build host")

        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            raise ToolchainManagerError(f"Build-host bootstrap failed with exit code {res.returncode}")

        host_resolv = Path("/etc/resolv.conf")
        if host_resolv.exists():
            resolv_dest = self.build_host_dir / "etc" / "resolv.conf"
            resolv_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(host_resolv, resolv_dest)

        logger.info("✅ Isolated build_host environment ready!")
        self.cache_build_host()

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Mounting virtual filesystems into build_host.")
            self.is_mounted = True
            return

        if self.is_mounted:
            return
        if not self.use_isolated or not self.build_host_dir.exists() or not self._is_base_rootfs():
            return

        self.is_mounted = True
        mounts = [
            ("proc", self.build_host_dir / "proc", "proc", None),
            ("sysfs", self.build_host_dir / "sys", "sysfs", None),
            ("/dev", self.build_host_dir / "dev", None, "--bind"),
        ]
        for src, target, fstype, opts in mounts:
            target.mkdir(parents=True, exist_ok=True)
            if opts in {"--bind", "--rbind"}:
                result = subprocess.run(["mount", opts, src, str(target)], check=False, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    raise ToolchainManagerError(f"Could not bind-mount {src} at {target}: {result.stderr.strip()}")
                slave = subprocess.run(
                    ["mount", "--make-rslave", str(target)],
                    check=False, stderr=subprocess.PIPE, text=True,
                )
                if slave.returncode != 0:
                    raise ToolchainManagerError(
                        f"Could not make {target} a private slave mount: {slave.stderr.strip()}"
                    )
                continue
            cmd = ["mount", "-t", fstype]
            cmd.extend([src, str(target)])
            result = subprocess.run(cmd, check=False, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                raise ToolchainManagerError(f"Could not mount {fstype} at {target}: {result.stderr.strip()}")

        self.project_mount.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["mount", "--bind", str(self.project_root), str(self.project_mount)], check=False, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise ToolchainManagerError(f"Could not bind project into build host: {result.stderr.strip()}")

        # A plain bind mount does not include nested mounts. When --tmpfs is
        # used, explicitly bind the architecture workdir into the build host
        # so tools running in the isolated namespace can see the rootfs.
        try:
            relative_workdir = self.workdir_base.relative_to(self.project_root)
            self.workdir_mount = self.project_mount / relative_workdir
            self.workdir_mount.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["mount", "--rbind", str(self.workdir_base), str(self.workdir_mount)],
                check=False, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode != 0:
                raise ToolchainManagerError(f"Could not bind architecture workdir into build host: {result.stderr.strip()}")
            slave = subprocess.run(
                ["mount", "--make-rslave", str(self.workdir_mount)],
                check=False, stderr=subprocess.PIPE, text=True,
            )
            if slave.returncode != 0:
                raise ToolchainManagerError(
                    f"Could not make {self.workdir_mount} a private slave mount: {slave.stderr.strip()}"
                )
        except ValueError as exc:
            raise ToolchainManagerError("Architecture workdir must be inside the project root") from exc

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Unmounting virtual filesystems from build_host.")
            self.is_mounted = False
            return

        if not self.build_host_dir.exists():
            return

        from deb_dev_builder.core.path_utils import unmount_all_under

        for path in [
            self.workdir_mount,
            self.project_mount,
            self.build_host_dir / "dev",
            self.build_host_dir / "sys",
            self.build_host_dir / "proc",
        ]:
            if path is not None and path.exists():
                # Both the build-host /dev and the project/workdir binds can
                # contain nested mounts.  Always detach descendants first;
                # a lazy unmount of only the parent leaves mounts behind and
                # can make the next sudo invocation lose its host PTY.
                unmount_all_under(path)
                subprocess.run(["umount", "-l", str(path)], check=False, stderr=subprocess.DEVNULL)

        self.workdir_mount = None
        self.is_mounted = False

    def run_tool(self, tool_binary: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = f"{tool_binary} {' '.join(args)}"
            logger.info(f"[MOCK TOOL EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=[tool_binary] + args, returncode=0, stdout="", stderr="")

        if self.is_mounted and self._tool_in_build_host(tool_binary):
            cmd = ["chroot", str(self.build_host_dir), tool_binary] + args
        else:
            cmd = [tool_binary] + args

        return subprocess.run(cmd, check=check)

    def run_in_build_host(self, command: Union[str, List[str]], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = command if isinstance(command, str) else " ".join(command)
            logger.info(f"[MOCK BUILD_HOST EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        use_chroot = self.is_mounted and self._is_base_rootfs()
        if isinstance(command, str):
            cmd = (["chroot", str(self.build_host_dir), "/bin/sh", "-c", command]
                   if use_chroot else ["/bin/sh", "-c", command])
        else:
            cmd = (["chroot", str(self.build_host_dir)] + command
                   if use_chroot else list(command))

        return subprocess.run(cmd, check=check)
