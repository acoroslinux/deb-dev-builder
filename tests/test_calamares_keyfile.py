"""Exercise keyfile preparation and actual initramfs content validation."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from deb_dev_builder.core.initrd_editor import Entry, pack_initrd
from deb_dev_builder.core.installer_crypto import keyfile_unlock_script, validate_keyfile_content
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.runtime_config import load_runtime, RuntimeConfigError


@pytest.mark.parametrize('override', [
    {'mode': '0644'}, {'umask': '0022'}, {'mode': '0000'},
    {'key_file': '/../outside'}, {'key_file': '/keys/*'},
])
def test_unsafe_unlock_configuration_is_rejected_before_building(override):
    with pytest.raises(RuntimeConfigError):
        load_runtime({'runtime': {'build': {'installer_storage': {'keyfile_unlock': override}}}})


@pytest.fixture
def unlock_target(tmp_path):
    root = tmp_path / 'installed system'
    for directory in ['etc', 'boot', 'tmp']:
        (root / directory).mkdir(parents=True)
    (root / 'crypto_keyfile.bin').write_bytes(os.urandom(64))
    (root / 'etc/crypttab').write_text('root UUID=root /crypto_keyfile.bin luks\nswap UUID=swap /crypto_keyfile.bin luks\n')
    tools = tmp_path / 'bin'
    tools.mkdir()
    for name, body in {
        'findmnt': 'printf "%s\\n" "/dev/mapper/root[/@]"',
        'lsblk': 'printf "%s\\n" "$BOOT_TYPE" part disk',
        'findfs': 'printf "%s\\n" /dev/test-device',
        'cryptsetup': 'printf "%s\\n" "$*" >> "$CALLS"; exit "${VERIFY_RESULT:-0}"',
    }.items():
        path = tools / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'BOOT_TYPE': 'crypt', 'CALLS': str(tmp_path / 'calls')}
    return root, tools, env


def run_preparation(root, env):
    script = keyfile_unlock_script(load_runtime()['build']['installer_storage'])
    subprocess.run(['sh', '-n'], input=script, check=True, capture_output=True)
    return subprocess.run(['sh', '-s', '--', str(root)], input=script, env=env, capture_output=True)


def test_keyfile_preparation_verifies_both_volumes_and_writes_real_shell_settings(unlock_target):
    root, _, env = unlock_target
    for _ in range(2):
        result = run_preparation(root, env)
        assert result.returncode == 0, result.stderr
    calls = Path(env['CALLS']).read_text().splitlines()
    assert len(calls) == 4  # Both root and swap, including a finalization retry.
    assert all('open --test-passphrase --key-file' in call for call in calls)
    conf = root / 'etc/cryptsetup-initramfs/conf-hook'
    assert conf.read_text().count('KEYFILE_PATTERN=') == 1
    script = '. ' + shlex.quote(str(conf)) + '; printf "%s" "$KEYFILE_PATTERN"'
    assert subprocess.check_output(['sh', '-c', script]) == b'/crypto_keyfile.bin'
    assert (root / 'etc/initramfs-tools/conf.d/zz-deb-dev-keyfile').read_text() == 'UMASK=0077\n'
    assert (root / 'crypto_keyfile.bin').stat().st_mode & 0o777 == 0o600
    assert all('initramfs' in line for line in (root / 'etc/crypttab').read_text().splitlines())


@pytest.mark.parametrize('problem', ['unencrypted-boot', 'unenrolled-key', 'symlink'])
def test_keyfile_preparation_rejects_unsafe_or_unusable_keys(unlock_target, problem):
    root, _, env = unlock_target
    if problem == 'unencrypted-boot':
        env['BOOT_TYPE'] = 'part'
    elif problem == 'unenrolled-key':
        env['VERIFY_RESULT'] = '2'
    else:
        key = root / 'crypto_keyfile.bin'
        key.rename(root / 'original-key')
        key.symlink_to('original-key')
    result = run_preparation(root, env)
    assert result.returncode != 0
    assert not (root / 'etc/cryptsetup-initramfs/conf-hook').exists()
    assert not (root / 'etc/initramfs-tools/conf.d/zz-deb-dev-keyfile').exists()


def test_unencrypted_installation_needs_no_keyfile_configuration(unlock_target):
    root, _, env = unlock_target
    (root / 'crypto_keyfile.bin').unlink()
    assert run_preparation(root, env).returncode == 0
    assert not Path(env['CALLS']).exists()


@pytest.mark.parametrize('problem', [None, 'missing-key', 'wrong-key', 'wrong-device', 'public-image', 'symlink'])
def test_validation_extracts_real_initramfs_and_checks_private_matching_keys(unlock_target, problem):
    if not shutil.which('unmkinitramfs'):
        pytest.skip('unmkinitramfs required')
    root, tools, env = unlock_target
    key = (root / 'crypto_keyfile.bin').read_bytes()
    entries = {
        'cryptroot': Entry('cryptroot', b'', 0o040700),
        'cryptroot/keyfiles': Entry('cryptroot/keyfiles', b'', 0o040700),
        'cryptroot/crypttab': Entry('cryptroot/crypttab', b'root UUID=root /cryptroot/keyfiles/root.key luks\nswap UUID=swap /cryptroot/keyfiles/swap.key luks\n', 0o100600),
    }
    if problem == 'wrong-device':
        entries['cryptroot/crypttab'].data = entries['cryptroot/crypttab'].data.replace(b'UUID=root', b'UUID=incorrect')
    for name in ['root', 'swap']:
        if problem == 'missing-key' and name == 'swap':
            continue
        data = b'wrong' if problem == 'wrong-key' and name == 'swap' else key
        path = 'cryptroot/keyfiles/' + name + '.key'
        entries[path] = Entry(path, data, 0o100600)
    image = root / 'boot/initrd.img-test'
    image.write_bytes(pack_initrd(entries))
    image.chmod(0o644 if problem == 'public-image' else 0o600)
    if problem == 'symlink':
        image.rename(root / 'boot/linked-image')
        image.symlink_to('linked-image')
    chroot = tools / 'chroot'
    chroot.write_text('#!/bin/sh\nroot=$1\nshift\nexec ' + shlex.quote(shutil.which('unmkinitramfs')) + ' "$root$2" "$root$3"\n')
    chroot.chmod(0o755)
    storage = load_runtime()['build']['installer_storage']
    script = 'set -eu\ntarget=' + shlex.quote(str(root)) + '\nimage="$target/boot/initrd.img-test"\nguest_image=/boot/initrd.img-test\n' + '\n'.join(validate_keyfile_content(storage))
    result = subprocess.run(['sh'], input=script.encode(), env=env, capture_output=True)
    assert (result.returncode == 0) == (problem is None), result.stderr
    assert not list((root / 'tmp').glob('deb-dev-keyfile.*'))


@pytest.mark.parametrize('luks_type', ['luks1', 'luks2'])
def test_generated_key_can_unlock_a_real_luks_image(tmp_path, luks_type):
    binary = shutil.which('cryptsetup')
    if binary is None:
        packages = sorted(resolve_from_project('cache/debian').glob('*/amd64/apt/archives/cryptsetup-bin_*.deb'))
        if not packages:
            pytest.skip('cryptsetup or a cached native package required')
        subprocess.run(['dpkg-deb', '-x', str(packages[-1]), str(tmp_path / 'native')], check=True, capture_output=True)
        binary = str(next((tmp_path / 'native').glob('usr/sbin/cryptsetup')))
    image = tmp_path / 'test-volume.luks'
    with image.open('wb') as stream:
        stream.truncate(32 * 1024 * 1024)
    passphrase = tmp_path / 'test-passphrase'
    passphrase.write_bytes(b'test-only-unlock-passphrase')
    key = tmp_path / 'test-random-key'
    key.write_bytes(os.urandom(64))
    for path in [key, passphrase]:
        path.chmod(0o600)
    options = ['--pbkdf', 'pbkdf2', '--pbkdf-force-iterations', '1000']
    subprocess.run([binary, 'luksFormat', '--type', luks_type, '--batch-mode', *options,
                    '--key-file', str(passphrase), str(image)], check=True, capture_output=True)
    subprocess.run([binary, 'luksAddKey', *options, '--key-file', str(passphrase),
                    str(image), str(key)], check=True, capture_output=True)
    subprocess.run([binary, 'open', '--test-passphrase', '--key-file', str(key), str(image)], check=True, capture_output=True)
