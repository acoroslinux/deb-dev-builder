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
        self.cache_dir = self.workdir_base.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.is_mounted = False
        self.use_isolated = False
        self.backend = "mock" if self.mode == "mock" else "isolated-oci"

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
        with tarfile.open(archive, "r:*") as handle:
            handle.extractall(self.build_host_dir, filter="data")
        if not self._is_bootstrapped():
            raise ToolchainManagerError("Toolchain tarball does not contain a valid build_host")
        cached_archives = {path.resolve() for path in self._cached_toolchain_archives()}
        if archive.resolve() not in cached_archives:
            self.cache_build_host()

    def _cached_toolchain_archives(self) -> list[Path]:
        """Return local build-host caches in preferred order."""
        root = self.cache_dir / "toolchains"
        return [
            root / f"build-host-{_HOST_ARCH}.tar.xz",
            root / f"build-host-{_HOST_ARCH}.tar.zst",
        ]

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def cache_build_host(self) -> Path:
        """Store a verified, reusable isolated build host under ``workdir/cache``."""
        if not self._is_bootstrapped():
            raise ToolchainManagerError("Cannot cache an incomplete build_host")
        archive = self._cached_toolchain_archives()[0]
        archive.parent.mkdir(parents=True, exist_ok=True)
        partial = archive.with_name(f"{archive.name}.partial")
        partial.unlink(missing_ok=True)
        project_mount_root = self.project_mount.relative_to(self.build_host_dir).parts[0]
        excluded_roots = {"proc", "sys", "dev", "run", project_mount_root}
        with tarfile.open(partial, "w:xz") as handle:
            for entry in sorted(self.build_host_dir.iterdir(), key=lambda path: path.name):
                if entry.name in excluded_roots:
                    continue
                handle.add(entry, arcname=entry.name, recursive=True)
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
            for member in handle:
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
        host_arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(_HOST_ARCH, _HOST_ARCH)
        descriptor = next((item for item in index.get("manifests", []) if item.get("platform", {}).get("os") == "linux" and item.get("platform", {}).get("architecture") == host_arch), None)
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
                "grub-efi-amd64-bin", "grub-efi-ia32-bin", "mtools", "dosfstools", "qemu-utils", "parted",
                "btrfs-progs", "syslinux-utils", "fdisk", "util-linux", "ca-certificates", "xfsprogs", "f2fs-tools",
                "xz-utils", "gzip", "lz4",
            ]
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
            ("/dev", self.build_host_dir / "dev", None, "--rbind"),
        ]
        for src, target, fstype, opts in mounts:
            target.mkdir(parents=True, exist_ok=True)
            if opts == "--rbind":
                result = subprocess.run(["mount", "--rbind", src, str(target)], check=False, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    raise ToolchainManagerError(f"Could not bind-mount {src} at {target}: {result.stderr.strip()}")
                subprocess.run(["mount", "--make-rslave", str(target)], check=False)
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

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Unmounting virtual filesystems from build_host.")
            self.is_mounted = False
            return

        if not self.build_host_dir.exists():
            return

        for path in [
            self.project_mount,
            self.build_host_dir / "dev",
            self.build_host_dir / "sys",
            self.build_host_dir / "proc",
        ]:
            if path.exists():
                subprocess.run(["umount", "-l", str(path)], check=False, stderr=subprocess.DEVNULL)

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
