"""Edit gzip/newc initramfs without extracting archive paths onto the host."""
from dataclasses import dataclass, replace
import fnmatch
import gzip
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from .path_utils import resolve_from_project


def archive_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\0" in name or path.as_posix() == "TRAILER!!!":
        raise ValueError(f"Unsafe initrd path: {name!r}")
    return path.as_posix()


@dataclass
class Entry:
    name: str
    data: bytes
    mode: int
    uid: int = 0
    gid: int = 0
    mtime: int = 0
    rdevmajor: int = 0
    rdevminor: int = 0


def unpack_initrd(payload):
    """Read all gzip members and all padded CPIO archives, last path wins.

    Hardlinks are materialized before merging so archive inode numbers cannot
    collide across concatenated archives or with newly generated files.
    """
    raw = gzip.decompress(payload)
    entries, groups, current = {}, {}, []
    offset = 0
    trailers = 0

    def finish():
        for entry, group in current:
            entries[entry.name] = replace(entry, data=groups.get(group, entry.data)) if group else entry
        current.clear()
        groups.clear()

    while offset < len(raw):
        if raw[offset] == 0:
            offset += 1
            continue
        header = raw[offset:offset + 110]
        if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
            raise ValueError(f"Unsupported or truncated CPIO header at {offset}")
        try:
            ino, mode, uid, gid, nlink, mtime, size, major, minor, rmajor, rminor, namesize, checksum = (
                int(header[i:i + 8], 16) for i in range(6, 110, 8)
            )
        except ValueError as exc:
            raise ValueError("Invalid CPIO metadata") from exc
        start = offset + 110
        end = start + namesize
        if namesize < 1 or end > len(raw) or raw[end - 1] != 0:
            raise ValueError("Invalid CPIO filename")
        name = raw[start:end - 1].decode("utf-8", errors="surrogateescape")
        offset = (end + 3) & ~3
        data = raw[offset:offset + size]
        if len(data) != size:
            raise ValueError("Truncated CPIO file")
        if header[:6] == b"070702" and sum(data) & 0xffffffff != checksum:
            raise ValueError("CPIO checksum mismatch")
        offset = (offset + size + 3) & ~3
        if name == "TRAILER!!!":
            finish()
            trailers += 1
            continue
        entry = Entry(archive_path(name), data, mode, uid, gid, mtime, rmajor, rminor)
        group = (ino, major, minor) if stat.S_ISREG(mode) and nlink > 1 else None
        if group and data:
            if group in groups and groups[group] != data:
                raise ValueError("Conflicting CPIO hardlink data")
            groups[group] = data
        current.append((entry, group))
    if current:
        raise ValueError("CPIO archive has no trailer")
    if not trailers:
        raise ValueError("Initrd contains no CPIO archive")
    return entries


def pack_initrd(entries, *, level=9, epoch=None):
    output = bytearray()
    ordered = sorted(entries.values(), key=lambda e: (e.name.count("/"), e.name))
    ordered.append(Entry("TRAILER!!!", b"", 0))
    for ino, entry in enumerate(ordered, 1):
        name = entry.name.encode("utf-8", errors="surrogateescape") + b"\0"
        fields = (ino, entry.mode, entry.uid, entry.gid, 1,
                  entry.mtime if epoch is None else epoch, len(entry.data),
                  0, 0, entry.rdevmajor, entry.rdevminor, len(name), 0)
        if any(not 0 <= value <= 0xffffffff for value in fields):
            raise ValueError("CPIO metadata exceeds newc limits")
        output.extend(b"070701" + b"".join(f"{value:08x}".encode() for value in fields))
        output.extend(name)
        output.extend(b"\0" * (-len(output) % 4))
        output.extend(entry.data)
        output.extend(b"\0" * (-len(output) % 4))
    return gzip.compress(bytes(output), compresslevel=level, mtime=0)


def replace_initrd(path, entries, *, level=9, epoch=None):
    payload = pack_initrd(entries, level=level, epoch=epoch)
    if unpack_initrd(payload) != {name: replace(entry, mtime=epoch) if epoch is not None else entry
                                 for name, entry in entries.items()}:
        raise ValueError("Initrd round-trip validation failed")
    path = Path(path)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".initrd-", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        pending.chmod(path.stat().st_mode & 0o777)
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def customize_initrd(path, policy, frontend, *, generated=(), epoch=None):
    entries = unpack_initrd(Path(path).read_bytes())
    if policy["enabled"]:
        for overlay in policy["overlays"]:
            if frontend not in overlay["frontends"]:
                continue
            source = resolve_from_project(overlay["source"])
            if not source.is_dir() or source.is_symlink():
                raise ValueError(f"Initrd overlay must be a directory: {source}")
            for root, dirs, files in os.walk(source, followlinks=False):
                for filename in [*dirs, *files]:
                    item = Path(root) / filename
                    info = item.lstat()
                    name = archive_path(item.relative_to(source).as_posix())
                    if stat.S_ISLNK(info.st_mode):
                        data = os.fsencode(os.readlink(item))
                    elif stat.S_ISREG(info.st_mode):
                        data = item.read_bytes()
                    elif stat.S_ISDIR(info.st_mode):
                        data = b""
                    else:
                        raise ValueError(f"Unsupported overlay file type: {item}")
                    entries[name] = Entry(name, data, info.st_mode, overlay["uid"], overlay["gid"], int(info.st_mtime))
        for pattern in policy["remove"]:
            pattern = archive_path(pattern)
            entries = {name: entry for name, entry in entries.items()
                       if not fnmatch.fnmatchcase(name, pattern) and not name.startswith(pattern + "/")}
        for patch in policy["patches"]:
            if frontend not in patch["frontends"]:
                continue
            name = archive_path(patch["path"])
            if name not in entries:
                raise ValueError(f"Patch target not found: {name}")
            entry = entries[name]
            if not stat.S_ISREG(entry.mode):
                raise ValueError(f"Patch target must be a regular file: {name}")
            text = entry.data.decode("utf-8")
            if not patch["search"] or text.count(patch["search"]) != patch["count"]:
                raise ValueError(f"Unexpected patch match count: {name}")
            entries[name] = replace(entry, data=text.replace(patch["search"], patch["replace"]).encode())
    # Mandatory generated hooks win over overlays/removals/patches.
    for entry in generated:
        name = archive_path(entry.name)
        for parent in reversed(PurePosixPath(name).parents):
            if str(parent) != ".":
                entries.setdefault(str(parent), Entry(str(parent), b"", stat.S_IFDIR | 0o755))
        entries[name] = entry
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if str(parent) in entries and not stat.S_ISDIR(entries[str(parent)].mode):
                raise ValueError(f"Initrd path descends through a non-directory: {name}")
    replace_initrd(path, entries, level=policy["compression_level"], epoch=epoch)
