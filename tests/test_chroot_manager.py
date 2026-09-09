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
