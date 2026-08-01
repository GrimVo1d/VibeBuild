"""Tests for vibebuild.analyzer module."""

import subprocess

import pytest

from vibebuild.analyzer import (
    BuildRequirement,
    PackageInfo,
    get_build_requires,
    get_package_info_from_srpm,
)
from vibebuild.exceptions import InvalidSRPMError, SRPMNotFoundError


class TestBuildRequirement:
    def test_str_with_version(self):
        req = BuildRequirement(name="make", version="4.0", operator=">=")

        result = str(req)

        assert result == "make >= 4.0"

    def test_str_without_version(self):
        req = BuildRequirement(name="gcc")

        result = str(req)

        assert result == "gcc"

    def test_hash_based_on_name(self):
        req1 = BuildRequirement(name="gcc", version="10.0", operator=">=")
        req2 = BuildRequirement(name="gcc", version="11.0", operator=">=")

        assert hash(req1) == hash(req2)

    def test_equality_based_on_name(self):
        req1 = BuildRequirement(name="gcc", version="10.0", operator=">=")
        req2 = BuildRequirement(name="gcc", version="11.0", operator=">=")
        req3 = BuildRequirement(name="make")

        assert req1 == req2
        assert req1 != req3

    def test_equality_with_non_build_requirement(self):
        req = BuildRequirement(name="gcc")

        assert req != "gcc"
        assert req != 123


class TestPackageInfo:
    def test_nvr_property(self):
        info = PackageInfo(
            name="test-package", version="1.0.0", release="1", build_requires=[], source_urls=[]
        )

        assert info.nvr == "test-package-1.0.0-1"


def _header(name, version, release, *requires):
    """rpm -qp --qf output for _HEADER_QUERY."""
    return "".join(f"{x}\n" for x in (name, version, release)) + "".join(
        "\t".join(r) + "\n" for r in requires
    )


@pytest.fixture
def srpm(tmp_path):
    path = tmp_path / "hello-2.12.3-2.fc45.src.rpm"
    path.write_bytes(b"fake srpm")
    return path


class TestGetPackageInfoFromSrpm:
    def test_nvr_comes_from_header(self, srpm, mock_subprocess_run):
        # %autorelease in the spec text: the header has the real release
        mock_subprocess_run.return_value.stdout = _header("hello", "2.12.3", "2.fc45")

        info = get_package_info_from_srpm(str(srpm))

        assert (info.name, info.version, info.release) == ("hello", "2.12.3", "2.fc45")
        assert info.nvr == "hello-2.12.3-2.fc45"
        argv = mock_subprocess_run.call_args.args[0]
        assert argv[:3] == ["rpm", "-qp", "--qf"]
        assert argv[-1] == str(srpm.resolve())
        assert mock_subprocess_run.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_rpm_reads_the_srpm_in_the_sandbox(self, srpm, mock_subprocess_run, mocker):
        mock_subprocess_run.return_value.stdout = _header("x", "1", "1")
        wrap = mocker.patch(
            "vibebuild.analyzer.sandbox_if_available",
            side_effect=lambda cmd, ro_paths=(): ["bwrap", *cmd],
        )

        get_package_info_from_srpm(str(srpm))

        assert wrap.call_args.args[1] == [srpm.resolve()]
        assert mock_subprocess_run.call_args.args[0][:2] == ["bwrap", "rpm"]

    def test_relative_path_is_passed_absolute(self, tmp_path, monkeypatch, mock_subprocess_run):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "-x-1-1.src.rpm").write_bytes(b"fake srpm")
        mock_subprocess_run.return_value.stdout = _header("x", "1", "1")

        get_package_info_from_srpm("-x-1-1.src.rpm")

        expected = tmp_path.resolve() / "-x-1-1.src.rpm"
        assert mock_subprocess_run.call_args.args[0][-1] == str(expected)

    def test_build_requires_keep_version_constraints(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = _header(
            "PyYAML",
            "6.0.3",
            "6.fc45",
            ("gcc", "", ""),
            ("python3dist(cython)", ">=", "3"),
            ("(python3dist(tomli) if python3-devel < 3.11)", "", ""),
        )

        reqs = get_package_info_from_srpm(str(srpm)).build_requires

        assert [str(r) for r in reqs] == [
            "gcc",
            "python3dist(cython) >= 3",
            "(python3dist(tomli) if python3-devel < 3.11)",
        ]
        assert reqs[0].operator is None and reqs[0].version is None

    def test_skips_rpmlib_file_deps_and_duplicates(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = _header(
            "pkg",
            "1",
            "1",
            ("python3-devel", "", ""),
            ("python3-devel", "", ""),
            ("rpmlib(CompressedFileNames)", "<=", "3.0.4-1"),
            ("/usr/bin/sh", "", ""),
            ("make", "", ""),
        )

        names = [r.name for r in get_package_info_from_srpm(str(srpm)).build_requires]

        assert names == ["python3-devel", "make"]

    def test_missing_file(self):
        with pytest.raises(SRPMNotFoundError):
            get_package_info_from_srpm("/nonexistent/file.src.rpm")

    def test_not_an_srpm(self, tmp_path):
        path = tmp_path / "not-an-srpm.txt"
        path.write_text("x")
        with pytest.raises(InvalidSRPMError, match="Not a valid SRPM"):
            get_package_info_from_srpm(str(path))

    def test_rpm_missing(self, srpm, mock_subprocess_run):
        mock_subprocess_run.side_effect = FileNotFoundError("rpm")
        with pytest.raises(InvalidSRPMError, match="rpm not found"):
            get_package_info_from_srpm(str(srpm))

    def test_rpm_timeout(self, srpm, mock_subprocess_run):
        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd="rpm", timeout=60)
        with pytest.raises(InvalidSRPMError, match="timed out"):
            get_package_info_from_srpm(str(srpm))

    def test_rpm_fails(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stderr = "error: not an rpm package\n"
        with pytest.raises(InvalidSRPMError, match="not an rpm package"):
            get_package_info_from_srpm(str(srpm))

    def test_unexpected_output(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = "hello\n"
        with pytest.raises(InvalidSRPMError, match="Unexpected rpm output"):
            get_package_info_from_srpm(str(srpm))


class TestGetBuildRequires:
    def test_returns_build_requirements(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = _header(
            "pkg", "1", "1", ("make", ">=", "4.0"), ("gcc", "", "")
        )

        reqs = get_build_requires(str(srpm))

        assert all(isinstance(r, BuildRequirement) for r in reqs)
        assert [str(r) for r in reqs] == ["make >= 4.0", "gcc"]
