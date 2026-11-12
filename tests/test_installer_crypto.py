import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from deb_dev_builder.core.installer_crypto import select_early_unlock
from deb_dev_builder.core.installer_lifecycle import target_header
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.runtime_config import load_runtime


def test_native_di_crypto_entries_are_explicitly_selected_without_changing_other_volumes(tmp_path):
    storage = load_runtime()["build"]["installer_storage"]
    (tmp_path / "etc").mkdir()
    table = tmp_path / "etc/crypttab"
    table.write_text("# retain documentation\n\n"
                     "rootcrypt UUID=root none luks,discard,x-initrd.attach\n"
                     "other UUID=other none luks,noauto\n"
                     "explicit UUID=explicit /root/key luks,initramfs,x-initrd.attach\n")
    script = "\n".join([*target_header(str(tmp_path)), *select_early_unlock(storage)])
    for _ in range(2):
        subprocess.run(["sh", "-ec", script], check=True)
    assert table.read_text() == ("# retain documentation\n\n"
                                 "rootcrypt UUID=root none luks,discard,x-initrd.attach,initramfs\n"
                                 "other UUID=other none luks,noauto\n"
                                 "explicit UUID=explicit /root/key luks,initramfs,x-initrd.attach\n")
    assert not list(table.parent.glob("crypttab.*"))


def test_debian_native_hook_consumes_the_explicit_initramfs_option(tmp_path):
    """Use the real cryptsetup-initramfs selector, not our own option parser."""
    packages = sorted(resolve_from_project("cache/debian").glob("*/amd64/apt/archives/cryptsetup-initramfs_*.deb"))
    if not packages:
        pytest.skip("Cached Debian cryptsetup-initramfs required")
    payload = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(packages[-1])], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        hook = next(item for item in archive if item.name.endswith("/hooks/cryptroot"))
        source = archive.extractfile(hook).read().decode()
    start = source.index("crypttab_print_initramfs_entry() {")
    end = source.index("\n}\n", start) + 3
    selector = source[start:end]
    (tmp_path / "cryptroot").mkdir()
    (tmp_path / "cryptroot/targets").touch()
    # Stub unrelated dependency inspection; keep the native decision itself.
    script = '''crypttab_parse_options() { return 0; }
crypttab_print_entry() { printf '%s\\n' "$CRYPTTAB_NAME"; }
CRYPTTAB_NAME=rootcrypt
'''
    script += selector + "\n"
    script += "crypttab_print_initramfs_entry\n"
    env = {**os.environ, "DESTDIR": str(tmp_path)}
    omitted = subprocess.run(["sh", "-c", script], env=env, capture_output=True, check=True)
    included = subprocess.run(["sh", "-c", script], env={**env, "CRYPTTAB_OPTION_initramfs": "yes"}, capture_output=True, check=True)
    assert omitted.stdout == b""
    assert included.stdout == b"rootcrypt\n"


@pytest.mark.parametrize('source', ['/dev/mapper/debian--vg-root', 'UUID=root-filesystem'])
@pytest.mark.parametrize('mapping', ['sda3_crypt', 'missing', 'disabled'])
def test_legacy_di_lvm_ancestors_require_unlock_without_selecting_data_volumes(tmp_path, source, mapping):
    storage = load_runtime()['build']['installer_storage']
    (tmp_path / 'etc').mkdir()
    table = tmp_path / 'etc/crypttab'
    options = 'luks,discard,noauto' if mapping == 'disabled' else 'luks,discard'
    name = 'wrong_mapping' if mapping == 'missing' else 'sda3_crypt'
    original = f'{name} UUID=encrypted none {options}\ndata UUID=data none luks\n'
    table.write_text(original)
    (tmp_path / 'etc/fstab').write_text(f'{source} / ext4 defaults 0 1\n')
    tools = tmp_path / 'bin'
    tools.mkdir()
    chroot = tools / 'chroot'
    chroot.write_text('''#!/bin/sh
shift
case "$1" in
    findfs) printf '%s\\n' /dev/mapper/debian--vg-root ;;
    lsblk) printf '%s\\n' 'lvm debian--vg-root' 'crypt sda3_crypt' 'part sda3' 'disk sda' ;;
    *) exit 99 ;;
esac
''')
    chroot.chmod(0o755)
    script = '\n'.join([*target_header(str(tmp_path)), *select_early_unlock(storage)])
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']}
    for _ in range(2):
        result = subprocess.run(['sh', '-ec', script], env=env, capture_output=True)
        assert (result.returncode == 0) == (mapping == 'sda3_crypt'), result.stderr
    if mapping == 'sda3_crypt':
        assert table.read_text() == 'sda3_crypt UUID=encrypted none luks,discard,initramfs\ndata UUID=data none luks\n'
    else:
        assert table.read_text() == original
    assert not list(table.parent.glob('crypttab.*'))


@pytest.mark.parametrize('include_key', [False, True])
def test_native_cryptroot_hook_embeds_only_explicitly_selected_keys(tmp_path, include_key):
    packages = sorted(resolve_from_project('cache/debian').glob('*/amd64/apt/archives/cryptsetup-initramfs_*.deb'))
    if not packages:
        pytest.skip('Cached Debian cryptsetup-initramfs required')
    payload = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(packages[-1])])
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        source = archive.extractfile(next(item for item in archive if item.name.endswith('/hooks/cryptroot'))).read().decode()
    start = source.index('crypttab_print_entry() {')
    function = source[start:source.index('\n}\n', start) + 3]
    key = tmp_path / 'test key'
    key.write_bytes(os.urandom(64))
    key.chmod(0o600)
    image = tmp_path / 'image'
    image.mkdir()
    script = '''set -eu
_resolve_device() { return 1; }
crypttab_key_check() { return 0; }
cryptsetup_message() { printf '%s\\n' "$*" >&2; }
copy_file() { cp -p -- "$2" "$DESTDIR$3"; }
usage=rootfs
exec 3>"$DESTDIR/table"
'''
    script += function + '\ncrypttab_print_entry\n'
    env = {**os.environ, 'DESTDIR': str(image), 'CRYPTTAB_NAME': 'root',
           'CRYPTTAB_SOURCE': 'UUID=test', 'CRYPTTAB_KEY': str(key),
           '_CRYPTTAB_NAME': 'root', '_CRYPTTAB_SOURCE': 'UUID=test',
           '_CRYPTTAB_KEY': str(key), '_CRYPTTAB_OPTIONS': 'luks,initramfs',
           'KEYFILE_PATTERN': str(key) if include_key else ''}
    result = subprocess.run(['sh'], input=script.encode(), env=env, capture_output=True)
    assert (result.returncode == 0) == include_key
    embedded = image / 'cryptroot/keyfiles/root.key'
    if include_key:
        assert embedded.read_bytes() == key.read_bytes()
        assert embedded.stat().st_mode & 0o077 == 0
        assert '/cryptroot/keyfiles/root.key' in (image / 'table').read_text()
    else:
        assert not embedded.exists()
