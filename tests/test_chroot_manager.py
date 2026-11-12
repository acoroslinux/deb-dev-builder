import subprocess

import pytest

from deb_dev_builder.core.chroot_manager import ChrootManager, ChrootManagerError


def test_missing_virtual_mount_stops_command_before_package_scripts(tmp_path, monkeypatch):
    manager = ChrootManager(tmp_path, mode="real")
    manager.is_mounted = True
    monkeypatch.setattr('os.path.ismount', lambda path: path.name != 'proc')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: pytest.fail('command must not run'))
    with pytest.raises(ChrootManagerError, match='proc'):
        manager.run_in_chroot(['apt-get', 'install', '-y', 'example'])


def test_chroot_uses_available_build_locale_and_respects_explicit_override(tmp_path, monkeypatch):
    manager = ChrootManager(tmp_path, mode="real")
    monkeypatch.setenv('LC_ALL', 'pt_PT.UTF-8')
    environments = []
    def run(command, **kwargs):
        environments.append(kwargs['env'])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', run)
    manager.run_in_chroot(['true'])
    manager.run_in_chroot(['true'], env={'LC_ALL': 'en_US.UTF-8'})
    assert environments[0]['LC_ALL'] == 'C.UTF-8'
    assert environments[1]['LC_ALL'] == 'en_US.UTF-8'


def test_chroot_mounts_private_devpts_after_isolating_dev(tmp_path, monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    manager = ChrootManager(tmp_path, mode='real')
    manager.mount_virtual_fs()
    isolate = ['mount', '--make-rslave', str(tmp_path / 'dev')]
    pts = ['mount', '-t', 'devpts', '-o', 'newinstance,ptmxmode=0666,mode=0620,gid=5', 'devpts', str(tmp_path / 'dev/pts')]
    assert commands.index(isolate) < commands.index(pts)
    assert ['mount', '--rbind', '/dev', str(tmp_path / 'dev')] not in commands


def test_missing_devpts_stops_command_before_package_scripts(tmp_path, monkeypatch):
    manager = ChrootManager(tmp_path, mode='real')
    manager.is_mounted = True
    monkeypatch.setattr('os.path.ismount', lambda path: path != tmp_path / 'dev/pts')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: pytest.fail('command must not run'))
    with pytest.raises(ChrootManagerError, match='dev/pts'):
        manager.run_in_chroot(['apt-get', 'install', '-y', 'example'])
