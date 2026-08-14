from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def resolve_from_project(relative_path: str | Path) -> Path:
    """Resolve a path relative to project root safely."""
    path_obj = Path(relative_path)
    if path_obj.is_absolute():
        return path_obj
    return (_PROJECT_ROOT / path_obj).resolve()

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

    unique_mounts = list(dict.fromkeys(mounts))
    unique_mounts.sort(key=lambda m: len(m), reverse=True)

    for mp in unique_mounts:
        subprocess.run(["umount", "-l", "-f", mp], capture_output=True)
