import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from deb_dev_builder.core.installer_cdrom import target_cdrom_hooks
from deb_dev_builder.core.initrd_editor import Entry, pack_initrd, unpack_initrd
from deb_dev_builder.core.installer_media import LiveInstallerMedia
from deb_dev_builder.core.runtime_config import load_runtime


@pytest.mark.parametrize("existing", [False, True])
def test_target_media_binding_is_idempotent_and_restores_apt_configuration(tmp_path, existing):
    target = tmp_path / "target with spaces"
    medium = tmp_path / "installer medium"
    (medium / ".disk").mkdir(parents=True)
    (medium / ".disk/info").write_text("DEVUAN_LIVE amd64\n")
    table = tmp_path / "mounts"
    table.write_text("")
    config = {"base_distro": "devuan", "suite": "excalibur"}
    policy = {**load_runtime(config)["installer"]["target_cdrom"], "source": str(medium), "mounts_file": str(table)}
    apt_file = target / policy["config_file"]
    apt_file.parent.mkdir(parents=True)
    if existing:
        apt_file.write_text("// Previous project configuration\n")
        apt_file.chmod(0o640)
    tools = tmp_path / "bin"
    tools.mkdir()
    implementation = '''#!/usr/bin/python3
import os,sys,shutil
from pathlib import Path
table=Path(os.environ['MEDIA_TEST_MOUNTS'])
with open(os.environ['MEDIA_TEST_EVENTS'],'a') as stream: stream.write(Path(sys.argv[0]).name+'\\n')
if Path(sys.argv[0]).name=='mount':
    assert sys.argv[1]=='--bind'
    source,destination=map(Path,sys.argv[2:])
    shutil.copytree(source/'.disk',destination/'.disk')
    escaped=str(destination).replace(chr(92),chr(92)+'134').replace(' ',chr(92)+'040')
    table.write_text('fixture '+escaped+' iso9660 ro 0 0\\n')
else:
    destination=Path(sys.argv[1])
    (destination/'.disk/info').unlink()
    (destination/'.disk').rmdir()
    table.write_text('')
'''
    for name in ("mount", "umount"):
        path = tools / name
        path.write_text(implementation)
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "MEDIA_TEST_MOUNTS": str(table), "MEDIA_TEST_EVENTS": str(tmp_path / "events")}
    prepare, finish = target_cdrom_hooks(str(target), policy)
    for _ in range(2):
        subprocess.run(["sh", "-eu"], input=prepare, text=True, env=env, capture_output=True, check=True)
    assert (tmp_path / "events").read_text() == "mount\n"
    assert 'Acquire::cdrom::mount "/media/deb-dev-installer/";' in apt_file.read_text()
    assert 'APT::CDROM::NoMount "true";' in apt_file.read_text()
    assert (target / policy["state_directory"] / ".ready").exists()
    for _ in range(2):
        subprocess.run(["sh", "-eu"], input=finish, text=True, env=env, capture_output=True, check=True)
    assert (tmp_path / "events").read_text() == "mount\numount\n"
    assert not (target / policy["mount"]).exists()
    assert not (target / policy["state_directory"]).exists()
    if existing:
        assert apt_file.read_text() == "// Previous project configuration\n"
        assert apt_file.stat().st_mode & 0o777 == 0o640
    else:
        assert not apt_file.exists()


def test_target_media_is_released_after_validation_before_native_ejection(tmp_path):
    config = {"base_distro": "devuan", "suite": "excalibur", "architecture": "amd64"}
    runtime = load_runtime(config)
    (tmp_path / "initrd.gz").write_bytes(pack_initrd({"original": Entry("original", b"native initrd", 0o100644)}))
    LiveInstallerMedia(config, runtime["installer"]).embed_preseed(tmp_path)
    entries = unpack_initrd((tmp_path / "initrd.gz").read_bytes())
    policy = runtime["installer"]["target_cdrom"]
    assert policy["release_hook"] in entries
    assert Path(runtime["installer"]["target_boot_hook"]).name < Path(policy["release_hook"]).name < "15cdrom-detect"
    validation = entries[runtime["installer"]["target_boot_hook"]].data
    assert (policy["state_directory"] + "/.ready").encode() in validation
    assert target_cdrom_hooks("/target", load_runtime()["installer"]["target_cdrom"]) == ("", "")


def test_native_apt_cdrom_fetches_registered_medium_without_remount_or_prompt(tmp_path):
    helper = Path("/usr/lib/apt/apt-helper")
    if not shutil.which("apt-cdrom") or not helper.exists():
        pytest.skip("Native APT CD-ROM tools required")
    medium = tmp_path / "mounted-medium"
    (medium / ".disk").mkdir(parents=True)
    (medium / "pool").mkdir()
    (medium / ".disk/info").write_text("DEVUAN_LIVE amd64\n")
    payload = b"verified offline init package payload"
    (medium / "pool/init.deb").write_bytes(payload)
    database = tmp_path / "cdroms.list"
    identified = subprocess.run(["apt-cdrom", "-m", "-d", str(medium), "-o", "Acquire::cdrom::AutoDetect=false", "-o", "Dir::State::cdroms=" + str(database), "ident"], capture_output=True, text=True, timeout=10, check=True)
    identifier = re.search(r"\[([^]]+)\]", identified.stdout)[1]
    database.write_text(f'CD::{identifier} "DEVUAN_LIVE amd64";\n')
    runtime = load_runtime({"base_distro": "devuan"})
    prepare, _ = target_cdrom_hooks("/target", runtime["installer"]["target_cdrom"])
    # Use the generated APT flags; only replace the chroot-local mount path
    # with the fixture's existing directory. No privileged mount is needed.
    # The configuration is shell-quoted as a single printf argument, so read
    # its payload from the exact generated shell command instead of parsing it.
    import shlex
    match = re.search(r"printf '%s' (.*?) > \"\$media_config\"", prepare, re.DOTALL)
    apt_configuration = shlex.split(match[1])[0]
    apt_configuration = apt_configuration.replace("/media/deb-dev-installer/", str(medium) + "/")
    apt_file = tmp_path / "apt.conf"
    apt_file.write_text(apt_configuration)
    output = tmp_path / "downloaded.deb"
    fetched = subprocess.run([str(helper), "-o", "Dir::State::cdroms=" + str(database), "download-file", "cdrom:[DEVUAN_LIVE amd64]/pool/init.deb", str(output), "SHA256:" + hashlib.sha256(payload).hexdigest()], env={**os.environ, "APT_CONFIG": str(apt_file)}, capture_output=True, text=True, timeout=10, check=True)
    assert output.read_bytes() == payload
    assert "Please insert" not in fetched.stdout + fetched.stderr
