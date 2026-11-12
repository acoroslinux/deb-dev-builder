"""Exercise separate /boot copying with the native installer tar implementation."""
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

import pytest

from deb_dev_builder.core.installer_lifecycle import materialize_boot_hardlinks
from deb_dev_builder.core.path_utils import resolve_from_project


def source_tree(root):
    (root / 'boot').mkdir(parents=True)
    (root / 'usr/lib/modules/kernel').mkdir(parents=True)
    (root / 'usr/lib64').mkdir()
    config = root / 'boot/config'
    config.write_bytes(b'kernel configuration\n')
    config.chmod(0o640)
    os.utime(config, (1234567890, 1234567890))
    os.link(config, root / 'usr/lib/modules/kernel/config')
    (root / 'usr/lib64/loader').symlink_to('../lib/loader')
    return root


def copy_archive(source):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        # Preserve this order: the failed deferred hardlink precedes the loader
        # symlink in the native tar restore queue, as observed in the installer.
        for name in ['boot/config', 'usr/lib/modules/kernel/config', 'usr/lib64/loader']:
            archive.add(source / name, arcname='./' + name, recursive=False)
    return stream.getvalue()


def test_boot_hardlinks_are_materialized_without_changing_contents_or_metadata(tmp_path):
    root = source_tree(tmp_path / 'source')
    original = (root / 'boot/config').stat()
    assert materialize_boot_hardlinks(root) == 1
    assert materialize_boot_hardlinks(root) == 0
    for name in ['boot/config', 'usr/lib/modules/kernel/config']:
        path = root / name
        assert path.read_bytes() == b'kernel configuration\n'
        metadata = path.stat()
        assert metadata.st_mode == original.st_mode
        assert metadata.st_uid == original.st_uid and metadata.st_gid == original.st_gid
        assert metadata.st_mtime_ns == original.st_mtime_ns
    assert (root / 'boot/config').stat().st_ino != (root / 'usr/lib/modules/kernel/config').stat().st_ino
    assert (root / 'usr/lib64/loader').is_symlink()
    assert not list((root / 'boot').glob('.deb-dev-boot-*'))


def test_native_busybox_tar_restores_loader_with_separate_boot_after_materialization(tmp_path):
    if platform.machine() != 'x86_64':
        pytest.skip('Cached amd64 installer binaries require an amd64 test host')
    busyboxes = sorted(resolve_from_project('cache/installer/testing/amd64').glob('busybox-udeb_1.38*.udeb'))
    libraries = sorted(resolve_from_project('cache/debian/testing/amd64/apt/archives').glob('libc6_*_amd64.deb'))
    if not busyboxes or not libraries:
        pytest.skip('Cached native BusyBox 1.38 and matching libc packages required')
    tools = tmp_path / 'tools'
    for package in [busyboxes[-1], libraries[-1]]:
        subprocess.run(['dpkg-deb', '-x', str(package), str(tools)], check=True, capture_output=True)
    lib = tools / 'usr/lib/x86_64-linux-gnu'
    native = [str(lib / 'ld-linux-x86-64.so.2'), '--library-path', str(lib), str(tools / 'usr/bin/busybox')]
    source = source_tree(tmp_path / 'source')
    with tempfile.TemporaryDirectory(prefix='.di-copy-test-', dir=resolve_from_project('')) as external:
        external = Path(external)
        if external.stat().st_dev == tmp_path.stat().st_dev:
            pytest.skip('Two filesystems are required to reproduce the cross-device hardlink failure')
        for fixed in [False, True]:
            target = tmp_path / str(fixed)
            (target / 'usr/lib/modules/kernel').mkdir(parents=True)
            (target / 'usr/lib64').mkdir()
            boot = external / str(fixed)
            boot.mkdir()
            # The archive omits the boot directory entry. This link emulates a
            # separate mounted /boot without requiring mount privileges.
            (target / 'boot').symlink_to(boot)
            if fixed:
                materialize_boot_hardlinks(source)
            result = subprocess.run(native + ['tar', 'xv'], input=copy_archive(source), cwd=target, capture_output=True)
            assert (result.returncode == 0) == fixed, result.stderr
            assert (target / 'usr/lib64/loader').is_symlink() == fixed
            if fixed:
                assert (target / 'usr/lib/modules/kernel/config').read_bytes() == (boot / 'config').read_bytes()
            else:
                assert b"can't create hardlink" in result.stderr


@pytest.mark.skipif(not shutil.which('mksquashfs') or not shutil.which('unsquashfs'), reason='SquashFS tools required')
def test_squashfs_deduplication_does_not_reintroduce_cross_boot_hardlinks(tmp_path):
    source = source_tree(tmp_path / 'source')
    materialize_boot_hardlinks(source)
    image = tmp_path / 'filesystem.squashfs'
    subprocess.run(['mksquashfs', str(source), str(image), '-noappend', '-no-progress', '-processors', '1'], check=True, capture_output=True)
    target = tmp_path / 'unpacked'
    subprocess.run(['unsquashfs', '-d', str(target), str(image)], check=True, capture_output=True)
    boot = target / 'boot/config'
    module = target / 'usr/lib/modules/kernel/config'
    assert boot.read_bytes() == module.read_bytes()
    assert boot.stat().st_ino != module.stat().st_ino
