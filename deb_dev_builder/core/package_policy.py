"""Package inventories shared by the live filesystem and its installers."""
from pathlib import Path


def installed_packages(root):
    status = Path(root) / "var/lib/dpkg/status"
    if not status.is_file():
        return {}
    result = {}
    for paragraph in status.read_text(errors="replace").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in paragraph.splitlines()
                      if ": " in line and not line[0].isspace())
        if fields.get("Status") == "install ok installed" and fields.get("Package"):
            identity = fields["Package"] + ":" + fields.get("Architecture", "all")
            result[identity] = {key: fields.get(key, "") for key in ("Package", "Version", "Architecture", "Source")}
    return result


def live_removals(before, after, keep):
    """Version changes do not make an installed package live-only."""
    protected_names = {entry["Package"] for entry in before.values()} | set(keep)
    return sorted({entry["Package"] for key, entry in after.items()
                   if key not in before and entry["Package"] not in protected_names})
