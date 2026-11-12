"""Session names shared by live login and installed-system login."""
import re


def session_candidates(config, runtime):
    policy = runtime["desktop"]
    name = config.get("desktop_session") or config.get("desktop")
    if not name:
        return []
    name = name.removesuffix(".desktop")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Invalid desktop session name")
    alias = policy["session_aliases"].get(name.lower(), name)
    if config.get("desktop_session"):
        return [alias]
    return list(policy["session_candidates"].get(name.lower(), [alias]))


def detect_session(root, config, runtime):
    policy = runtime["desktop"]
    candidates = session_candidates(config, runtime)
    for name in candidates:
        for directory in policy["session_directories"]:
            if (root / directory / (name + ".desktop")).is_file():
                return name
    # Never silently log into a different installed desktop when a specific
    # desktop was selected. Empty roots used by mock/configuration runs still
    # receive the selected desktop's normal session name.
    if candidates:
        return candidates[0]
    for directory in policy["session_directories"]:
        for entry in sorted((root / directory).glob("*.desktop")):
            if entry.is_file():
                return entry.stem
    return policy["fallback_session"]
