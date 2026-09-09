from pathlib import Path
import shutil

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def resolve_from_project(relative_path: str | Path) -> Path:
    """Resolve a path relative to project root safely."""
    path_obj = Path(relative_path)
    if path_obj.is_absolute():
        return path_obj
    return (_PROJECT_ROOT / path_obj).resolve()


_ARTIFACT_SUFFIXES = (
    ".netboot.tar.gz", ".oci.tar", ".tar.xz", ".tar.gz", ".qcow2", ".vmdk", ".vdi",
    ".vhdx", ".vhd", ".raw", ".img", ".iso", ".zst", ".xz", ".gz",
    ".lz4", ".manifest",
)


def resolve_output_path(output_name: str | Path, extension: str) -> Path:
    """Return an artifact path, placing bare names in the project output dir.

    ``output_name`` is a stem from the orchestrator or a user supplied path.  A
    known artifact suffix is replaced instead of duplicated (``disk.qcow2``
    never becomes ``disk.qcow2.qcow2``).  Relative paths containing a directory
    are resolved from the current working directory, matching normal CLI path
    semantics; bare names are always written below the project ``output/``.
    """
    requested = Path(output_name).expanduser()
    suffix = extension if str(extension).startswith(".") else f".{extension}"
    filename = requested.name
    lowered = filename.lower()
    changed = True
    while changed:
        changed = False
        for known in _ARTIFACT_SUFFIXES:
            if lowered.endswith(known):
                filename = filename[:-len(known)]
                lowered = lowered[:-len(known)]
                changed = True
                break
    filename = f"{filename or 'artifact'}{suffix}"
    candidate = requested.with_name(filename)
    if candidate.is_absolute():
        return candidate
    if candidate.parent == Path("."):
        return resolve_from_project("output") / candidate
    if candidate.parts[0] == "output":
        return resolve_from_project(candidate)
    return candidate.resolve()


def safe_remove_tree(path: str | Path, allowed_root: str | Path | None = None) -> None:
    """Remove one exact directory without following a symlink or broad path."""
    candidate = Path(path)
    if candidate.is_symlink():
        candidate.unlink()
        return
    resolved = candidate.resolve()
    forbidden = {Path("/"), Path("/home"), Path("/root"), Path("/tmp"), Path("/var")}
    if resolved in forbidden:
        raise ValueError(f"Refusing to remove unsafe directory: {resolved}")
    if allowed_root is not None:
        root = Path(allowed_root).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Refusing to remove path outside {root}: {resolved}")
    if candidate.exists():
        shutil.rmtree(candidate)

def unmount_all_under(target_dir: Path) -> None:
    """
    Scans /proc/mounts for all active mountpoints inside target_dir
    and unmounts them in reverse order (deepest path first).
    Guarantees clean directory removal without 'Device or resource busy' errors.
    """
    import os, subprocess, logging
    if os.geteuid() != 0:
        return

    target_resolved = target_dir.resolve()
    target_str = str(target_resolved)
    if target_resolved in {Path("/"), Path("/home"), Path("/root"), Path("/tmp"), Path("/var")}:
        raise ValueError(f"Refusing to unmount broad directory: {target_resolved}")

    mounts = mountpoints_under(target_resolved)
    unique_mounts = list(dict.fromkeys(mounts))
    unique_mounts.sort(key=lambda m: len(m), reverse=True)

    for mp in unique_mounts:
        subprocess.run(["umount", "-l", "-f", mp], capture_output=True)

    # Recursive bind mounts may not disappear when each entry is detached
    # individually (notably devpts/shm/mqueue below a rbind /dev).  Ask
    # util-linux to walk the tree as a final, narrowly scoped fallback.
    remaining = mountpoints_under(target_resolved)
    if remaining:
        subprocess.run(["umount", "--recursive", "--lazy", str(target_resolved)], capture_output=True)
        # A few kernels expose descendants again after the recursive detach;
        # retry the deepest entries once more before returning to callers.
        for mp in sorted(mountpoints_under(target_resolved), key=len, reverse=True):
            subprocess.run(["umount", "-l", "-f", mp], capture_output=True)

def mountpoints_under(target_dir: str | Path) -> list[str]:
    """Return active mountpoints at or below a narrow target directory."""
    import os
    target_resolved = Path(target_dir).resolve()
    if target_resolved in {Path("/"), Path("/home"), Path("/root"), Path("/tmp"), Path("/var")}:
        raise ValueError(f"Refusing to inspect broad directory: {target_resolved}")
    if os.geteuid() != 0 or not os.path.exists("/proc/mounts"):
        return []
    target_str = str(target_resolved)
    mounts = []
    if os.path.exists("/proc/mounts"):
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mp = parts[1].replace("\\040", " ").replace("\\011", "\t")
                        try:
                            mp_path = Path(mp).resolve()
                            mp_str = str(mp_path)
                            if mp_str == target_str or mp_str.startswith(target_str + "/"):
                                mounts.append(mp_str)
                        except Exception:
                            if mp == target_str or mp.startswith(target_str + "/"):
                                mounts.append(mp)
        except Exception as e:
            pass

    return list(dict.fromkeys(mounts))
