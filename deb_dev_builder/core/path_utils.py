from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def resolve_from_project(relative_path: str | Path) -> Path:
    """Resolve a path relative to project root safely."""
    path_obj = Path(relative_path)
    if path_obj.is_absolute():
        return path_obj
    return (_PROJECT_ROOT / path_obj).resolve()
