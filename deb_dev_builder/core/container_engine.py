import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, Any
import logging
from deb_dev_builder.core.path_utils import resolve_output_path

logger = logging.getLogger("container_engine")

class ContainerEngineError(Exception):
    """Raised when an OCI container archive build fails."""
    pass

class ContainerEngine:
    """
    Builds OCI-compliant container layer archives (.oci.tar) from a bootstrapped Debian/Devuan rootfs.
    """
    def __init__(self, target_root: Path, output_name: str, config: Dict[str, Any], mode: str = "real"):
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()

    def build_oci_archive(self) -> Path:
        out_path = resolve_output_path(self.output_name, ".oci.tar")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            out_path.touch()
            return out_path

        if not self.target_root.is_dir():
            raise ContainerEngineError(f"Target rootfs does not exist: {self.target_root}")

        logger.info("📦 Packaging OCI image-layout archive to: %s", out_path)
        excluded_roots = {"proc", "sys", "dev", "run", "tmp"}
        try:
            with tempfile.TemporaryDirectory(prefix="deb-dev-oci-") as temp_dir:
                layout = Path(temp_dir)
                blobs = layout / "blobs" / "sha256"
                blobs.mkdir(parents=True)

                layer_tmp = layout / "layer.tar"
                with tarfile.open(layer_tmp, "w", format=tarfile.PAX_FORMAT) as layer:
                    for child in sorted(self.target_root.iterdir(), key=lambda p: p.name):
                        if child.name in excluded_roots:
                            continue
                        layer.add(child, arcname=child.name, recursive=True, filter=self._layer_filter)
                layer_digest = self._store_blob(layer_tmp.read_bytes(), blobs)
                layer_size = (blobs / layer_digest).stat().st_size
                layer_tmp.unlink()

                architecture = self.config.get("dpkg_arch", self.config.get("architecture", "amd64"))
                oci_arch = {"amd64": "amd64", "arm64": "arm64", "aarch64": "arm64", "i386": "386", "armhf": "arm"}.get(architecture, architecture)
                image_config = {
                    "architecture": oci_arch,
                    "os": "linux",
                    "rootfs": {"type": "layers", "diff_ids": [f"sha256:{layer_digest}"]},
                    "config": {"Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"], "Cmd": ["/bin/sh"]},
                }
                config_bytes = json.dumps(image_config, sort_keys=True, separators=(",", ":")).encode()
                config_digest = self._store_blob(config_bytes, blobs)

                manifest = {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": f"sha256:{config_digest}", "size": len(config_bytes)},
                    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": f"sha256:{layer_digest}", "size": layer_size}],
                }
                manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                manifest_digest = self._store_blob(manifest_bytes, blobs)
                (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
                index = {
                    "schemaVersion": 2,
                    "manifests": [{
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": f"sha256:{manifest_digest}",
                        "size": len(manifest_bytes),
                        "annotations": {"org.opencontainers.image.ref.name": "latest"},
                    }],
                }
                (layout / "index.json").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                with tarfile.open(out_path, "w") as archive:
                    for item in sorted(layout.iterdir(), key=lambda p: p.name):
                        archive.add(item, arcname=item.name)
        except (OSError, tarfile.TarError) as exc:
            raise ContainerEngineError(f"Failed to create OCI archive: {exc}") from exc

        return out_path

    @staticmethod
    def _store_blob(content: bytes, blobs: Path) -> str:
        digest = hashlib.sha256(content).hexdigest()
        (blobs / digest).write_bytes(content)
        return digest

    @staticmethod
    def _layer_filter(info: tarfile.TarInfo):
        path = info.name.lstrip("./")
        if path == "var/cache/apt" or path.startswith("var/cache/apt/"):
            return None
        return info
