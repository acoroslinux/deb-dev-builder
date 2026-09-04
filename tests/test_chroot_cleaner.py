from deb_dev_builder.core.chroot_cleaner import ChrootCleaner


def test_cleaner_removes_ephemeral_content_and_keeps_package_database(tmp_path):
    root = tmp_path / "root"
    (root / "var/lib/apt/lists/partial").mkdir(parents=True)
    (root / "var/lib/apt/lists/index").write_text("index")
    (root / "var/cache/apt/archives/partial").mkdir(parents=True)
    (root / "var/cache/apt/archives/pkg.deb").write_text("deb")
    (root / "var/log/journal").mkdir(parents=True)
    (root / "var/log/journal/log").write_text("log")
    (root / "tmp/work").mkdir(parents=True)
    (root / "tmp/work/file").write_text("tmp")
    (root / "var/lib/dpkg/status").parent.mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text("status")
    (root / ".deb-dev-builder-bootstrap.json").write_text("metadata")
    (root / "etc/apt/apt.conf.d").mkdir(parents=True)
    (root / "etc/apt/apt.conf.d/99optimize").write_text("Acquire::Languages \"none\";")

    report = ChrootCleaner(root, {"cleanup": {}}).clean()

    assert report["removed"] >= 5
    assert not (root / "var/lib/apt/lists/index").exists()
    assert not (root / "var/cache/apt/archives/pkg.deb").exists()
    assert not (root / "tmp/work").exists()
    assert not (root / ".deb-dev-builder-bootstrap.json").exists()
    assert (root / "var/lib/dpkg/status").exists()
    assert (root / "var/lib/apt/lists/partial").exists()
    assert not (root / "etc/apt/apt.conf.d/99optimize").exists()
