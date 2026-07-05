"""Integration tests against the dev Koji stand (dev/koji-server).

Excluded from the default run. Inside the client container:

    docker compose exec vibebuild-dev pytest -m integration

They use ~/.koji/config there, the real rpm tools and the stand's build target.
"""

import shutil
import subprocess

import pytest

from vibebuild import cli
from vibebuild.analyzer import get_package_info_from_srpm
from vibebuild.koji_client import KojiClient
from vibebuild.spec_patcher import cleanup_patched, patch_srpm

pytestmark = pytest.mark.integration

SPEC = """\
Name:           vb-smoke
Version:        1.0
Release:        1
Summary:        VibeBuild integration test package
License:        MIT
BuildArch:      noarch
BuildRequires:  make

%description
Built by the VibeBuild integration tests.

%install
mkdir -p %{buildroot}%{_datadir}/vb-smoke
echo ok > %{buildroot}%{_datadir}/vb-smoke/ok

%files
%{_datadir}/vb-smoke/ok
"""


@pytest.fixture(scope="module")
def koji_config():
    config = cli.load_koji_config()
    if not config["server"] or not config["target"]:
        pytest.skip("needs ~/.koji/config with server and target (the dev stand client)")
    return config


@pytest.fixture(scope="module")
def srpm(tmp_path_factory):
    if not shutil.which("rpmbuild"):
        pytest.skip("needs rpmbuild")
    topdir = tmp_path_factory.mktemp("rpmbuild")
    spec = topdir / "vb-smoke.spec"
    spec.write_text(SPEC)
    subprocess.run(
        ["rpmbuild", "-bs", "--define", f"_topdir {topdir}", str(spec)],
        check=True,
        capture_output=True,
    )
    return next((topdir / "SRPMS").glob("vb-smoke-*.src.rpm"))


def test_hub_has_target_with_external_repos(koji_config):
    client = KojiClient(koji_config["server"], koji_config["serverca"])

    target = client.get_target(koji_config["target"])

    assert target is not None
    assert client.has_external_repos(target["build_tag"])


def test_reads_srpm_header(srpm):
    info = get_package_info_from_srpm(str(srpm))

    assert info.nvr.startswith("vb-smoke-1.0-1")
    assert [r.name for r in info.build_requires] == ["make"]


def test_patch_srpm_rebuilds_it(srpm):
    patched = patch_srpm(str(srpm))
    try:
        assert get_package_info_from_srpm(patched).name == "vb-smoke"
    finally:
        cleanup_patched(patched)


def test_dry_run(koji_config, srpm):
    assert cli.main(["--dry-run", str(srpm)]) == 0


def test_scratch_build(koji_config, srpm):
    assert cli.main(["--scratch", str(srpm)]) == 0
