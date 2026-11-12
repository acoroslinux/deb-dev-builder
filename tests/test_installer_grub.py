import os
from pathlib import Path
import shlex
import subprocess

import pytest

from deb_dev_builder.core.installer_grub import grub_entries, grub_lines
from deb_dev_builder.core.installer_lifecycle import target_header
from deb_dev_builder.core.installer_media import target_boot_hook
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.runtime_config import load_runtime


def install_plymouth(root, runtime):
    policy = runtime['customization']['plymouth']
    for name in [policy['binary'], policy['selector'], *policy['required_initramfs_paths']]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o755)


@pytest.mark.parametrize('plymouth', [True, False])
def test_grub_defaults_preserve_storage_and_custom_parameters_and_do_not_duplicate(tmp_path, plymouth):
    runtime = load_runtime()
    root = tmp_path / 'installed system'
    defaults = root / 'etc/default/grub'
    defaults.parent.mkdir(parents=True)
    original = '''GRUB_ENABLE_CRYPTODISK=y
GRUB_CMDLINE_LINUX="cryptopts=target=rootcrypt"
GRUB_CMDLINE_LINUX_DEFAULT="quiet audit=1 example='value'"
GRUB_TIMEOUT=0
GRUB_TIMEOUT=2
CUSTOM_SETTING=keep
'''
    defaults.write_text(original)
    if plymouth:
        install_plymouth(root, runtime)
    script = '\n'.join([*target_header(str(root)), *grub_lines(runtime)])
    for _ in range(2):
        subprocess.run(['sh', '-e'], input=script, text=True, check=True)
    content = defaults.read_text()
    assert content.count('GRUB_TIMEOUT=') == 1
    assert content.count('# GRUB configuration managed') == 1
    assert 'GRUB_ENABLE_CRYPTODISK=y' in content
    assert 'CUSTOM_SETTING=keep' in content
    result = subprocess.run(['sh', '-ec', '. "$1"; printf "%s\\n%s\\n" "$GRUB_CMDLINE_LINUX_DEFAULT" "$GRUB_CMDLINE_LINUX"', 'check', str(defaults)], text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ["quiet audit=1 example='value'" + (' splash' if plymouth else ''), 'cryptopts=target=rootcrypt']


@pytest.mark.parametrize('regenerate', [True, False])
def test_d_i_grub_hook_selects_theme_and_updates_installed_menu(tmp_path, regenerate):
    root = tmp_path / 'target'
    (root / 'etc').mkdir(parents=True)
    (root / 'sys/dev/block').mkdir(parents=True)
    runtime = load_runtime({'runtime': {'installer': {'target_root': str(root)}}})
    install_plymouth(root, runtime)
    tools = tmp_path / 'bin'
    tools.mkdir()
    chroot = tools / 'chroot'
    chroot.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$EVENTS"\n')
    chroot.chmod(0o755)
    entry, = grub_entries({}, runtime, regenerate=regenerate)
    subprocess.run(['sh', '-e'], input=entry.data, check=True, env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'], 'EVENTS': str(tmp_path / 'events')})
    events = (tmp_path / 'events').read_text().splitlines()
    assert events[0].endswith('plymouth-set-default-theme deb-dev-modern')
    assert events[-1].endswith('update-grub')
    assert any('update-initramfs' in event for event in events) == regenerate
    assert Path(entry.name).name < '14zz-deb-dev-target-boot'


def test_live_iso_uses_plymouth_arguments_when_theme_is_available(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    engine = ISOEngine(tmp_path / 'work', root, 'test', {'architecture': 'amd64'}, mode='mock', toolchain=None)
    assert 'splash' not in engine._get_kernel_params().split()
    install_plymouth(root, engine.runtime)
    params = engine._get_kernel_params().split()
    assert 'quiet' in params and 'splash' in params and 'boot=live' in params


@pytest.mark.parametrize('label_plugin', ['label.so', 'label-pango.so', 'label-freetype.so'])
def test_installed_initrd_accepts_supported_plymouth_label_plugins(tmp_path, label_plugin):
    runtime = load_runtime()
    root = tmp_path / 'target'
    (root / 'etc').mkdir(parents=True)
    (root / 'etc/fstab').write_text('UUID=root / ext4 defaults 0 1\n')
    (root / 'boot/grub').mkdir(parents=True)
    (root / 'boot/grub/grub.cfg').write_text('linux /vmlinuz root=UUID=root quiet splash\n')
    (root / 'boot/initrd.img-test').touch()
    install_plymouth(root, runtime)
    paths = runtime['customization']['plymouth']['required_initramfs_paths'][:]
    paths += ['usr/lib/x86_64-linux-gnu/plymouth/script.so', f'usr/lib/x86_64-linux-gnu/plymouth/{label_plugin}']
    tools = tmp_path / 'bin'
    tools.mkdir()
    chroot = tools / 'chroot'
    chroot.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.join(paths) + '\n')
    chroot.chmod(0o755)
    script = target_boot_hook(str(root), runtime['build']['installer_storage'], [], package_operations=False, regenerate=False, plymouth=runtime['customization']['plymouth'])
    result = subprocess.run(['sh', '-e'], input=script, capture_output=True, env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('missing', [None, 'theme', 'plugin'])
def test_installed_initrd_requires_plymouth_theme_and_plugins(tmp_path, missing):
    runtime = load_runtime()
    root = tmp_path / 'target'
    (root / 'etc').mkdir(parents=True)
    (root / 'etc/fstab').write_text('UUID=root / ext4 defaults 0 1\n')
    (root / 'boot/grub').mkdir(parents=True)
    (root / 'boot/grub/grub.cfg').write_text('linux /vmlinuz root=UUID=root quiet splash\n')
    (root / 'boot/initrd.img-test').touch()
    install_plymouth(root, runtime)
    paths = runtime['customization']['plymouth']['required_initramfs_paths'][:]
    paths += ['usr/lib/x86_64-linux-gnu/plymouth/script.so', 'usr/lib/x86_64-linux-gnu/plymouth/label-pango.so']
    if missing == 'theme':
        paths = [p for p in paths if not p.endswith('.plymouth')]
    if missing == 'plugin':
        paths = [p for p in paths if not p.endswith(('script.so', 'label.so', 'label-pango.so', 'label-freetype.so'))]
    tools = tmp_path / 'bin'
    tools.mkdir()
    chroot = tools / 'chroot'
    chroot.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.join(paths) + '\n')
    chroot.chmod(0o755)
    script = target_boot_hook(str(root), runtime['build']['installer_storage'], [], package_operations=False, regenerate=False, plymouth=runtime['customization']['plymouth'])
    result = subprocess.run(['sh', '-e'], input=script, capture_output=True, env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
    assert result.returncode == (0 if missing is None else 10), result.stderr
