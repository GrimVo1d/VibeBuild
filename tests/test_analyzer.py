"""Tests for vibebuild.analyzer module."""

import pytest

from vibebuild.analyzer import (
    BuildRequirement,
    PackageInfo,
    SpecAnalyzer,
    get_build_requires,
    get_package_info_from_srpm,
    read_srpm_header,
)
from vibebuild.exceptions import InvalidSRPMError, SpecParseError


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


class TestSpecAnalyzer:
    def test_analyze_spec_extracts_name(self, sample_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(sample_spec))

        assert result.name == "test-package"

    def test_analyze_spec_extracts_version(self, sample_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(sample_spec))

        assert result.version == "1.0.0"

    def test_analyze_spec_extracts_release(self, sample_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(sample_spec))

        assert result.release == "1"

    def test_analyze_spec_extracts_build_requires(self, sample_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(sample_spec))

        req_names = [r.name for r in result.build_requires]
        assert "python3-devel" in req_names
        assert "gcc" in req_names
        assert "make" in req_names

    def test_analyze_spec_extracts_version_constraints(self, sample_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(sample_spec))

        make_req = next((r for r in result.build_requires if r.name == "make"), None)
        assert make_req is not None
        assert make_req.operator == ">="
        assert make_req.version == "4.0"

    def test_analyze_spec_extracts_source_urls(self, sample_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(sample_spec))

        assert len(result.source_urls) >= 1
        assert "test-package-1.0.0.tar.gz" in result.source_urls[0]

    def test_analyze_spec_raises_on_missing_file(self):
        analyzer = SpecAnalyzer()

        with pytest.raises(FileNotFoundError):
            analyzer.analyze_spec("/nonexistent/path.spec")

    def test_analyze_spec_raises_on_missing_name(self, tmp_path):
        invalid_spec = tmp_path / "invalid.spec"
        invalid_spec.write_text("Version: 1.0\nRelease: 1")
        analyzer = SpecAnalyzer()

        with pytest.raises(SpecParseError, match="Could not find Name"):
            analyzer.analyze_spec(str(invalid_spec))

    def test_analyze_spec_raises_on_missing_version(self, tmp_path):
        invalid_spec = tmp_path / "invalid.spec"
        invalid_spec.write_text("Name: test-pkg\nRelease: 1")
        analyzer = SpecAnalyzer()

        with pytest.raises(SpecParseError, match="Could not find Version"):
            analyzer.analyze_spec(str(invalid_spec))

    def test_analyze_spec_defaults_release_to_1(self, tmp_path):
        spec = tmp_path / "minimal.spec"
        spec.write_text("Name: test-pkg\nVersion: 1.0")
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        assert result.release == "1"

    def test_analyze_spec_ignores_comments(self, tmp_path):
        spec = tmp_path / "commented.spec"
        spec.write_text(
            """
Name: test-pkg
Version: 1.0
Release: 1
# BuildRequires: should-be-ignored
BuildRequires: actual-dep
"""
        )
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        req_names = [r.name for r in result.build_requires]
        assert "should-be-ignored" not in req_names
        assert "actual-dep" in req_names

    def test_analyze_spec_handles_comma_separated_requires(self, complex_spec):
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(complex_spec))

        req_names = [r.name for r in result.build_requires]
        assert "python3-setuptools" in req_names
        assert "python3-wheel" in req_names
        assert "gcc" in req_names
        assert "gcc-c++" in req_names

    def test_analyze_spec_expands_macros(self, tmp_path):
        spec = tmp_path / "macro.spec"
        spec.write_text(
            """
Name: my-package
Version: 2.0
Release: 1
Source0: %{name}-%{version}.tar.gz
"""
        )
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        assert "my-package-2.0.tar.gz" in result.source_urls[0]


class TestSpecAnalyzerEdgeCases:
    def test_extract_value_without_colon(self):
        """_extract_value should return '' when line has no colon."""
        analyzer = SpecAnalyzer()

        result = analyzer._extract_value("NoColonHere", "NoColonHere")

        assert result == ""

    def test_build_requires_with_empty_parts(self, tmp_path):
        """BuildRequires with consecutive commas should skip empty parts."""
        spec = tmp_path / "empty_parts.spec"
        spec.write_text("Name: test-pkg\nVersion: 1.0\nRelease: 1\nBuildRequires: gcc,,make\n")
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        req_names = [r.name for r in result.build_requires]
        assert "gcc" in req_names
        assert "make" in req_names

    def test_build_requires_version_as_separate_tokens(self, tmp_path):
        """BuildRequires with 'pkg >= ver' as separate tokens in comma list."""
        spec = tmp_path / "sep_tokens.spec"
        spec.write_text("Name: test-pkg\nVersion: 1.0\nRelease: 1\nBuildRequires: pkg-a, >= ,1.0\n")
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        req_names = [r.name for r in result.build_requires]
        assert "pkg-a" in req_names


class TestSpecAnalyzerBranchCoverage:
    def test_source_line_with_empty_url(self, tmp_path):
        """Source line with nothing after colon should not add empty URL."""
        spec = tmp_path / "empty_source.spec"
        spec.write_text("Name: test-pkg\nVersion: 1.0\nRelease: 1\nSource0:\n")
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        assert result.source_urls == []

    def test_build_requires_macro_expands_to_empty(self, tmp_path):
        """BuildRequires with macro that expands to empty should be skipped."""
        spec = tmp_path / "macro_empty.spec"
        spec.write_text(
            "Name: test-pkg\nVersion: 1.0\nRelease: 1\nBuildRequires: %{?nonexistent_macro}\n"
        )
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        req_names = [r.name for r in result.build_requires]
        assert "" not in req_names

    def test_build_requires_version_op_without_name(self, tmp_path):
        """BuildRequires starting with version operator should give empty name."""
        spec = tmp_path / "no_name.spec"
        spec.write_text("Name: test-pkg\nVersion: 1.0\nRelease: 1\nBuildRequires: >=1.0\n")
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        req_names = [r.name for r in result.build_requires]
        assert "" not in req_names


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


class TestReadSrpmHeader:
    def test_nvr_comes_from_header(self, srpm, mock_subprocess_run):
        # %autorelease in the spec text: the header has the real release
        mock_subprocess_run.return_value.stdout = _header("hello", "2.12.3", "2.fc45")

        info = read_srpm_header(str(srpm))

        assert (info.name, info.version, info.release) == ("hello", "2.12.3", "2.fc45")
        assert info.nvr == "hello-2.12.3-2.fc45"
        argv = mock_subprocess_run.call_args.args[0]
        assert argv[:3] == ["rpm", "-qp", "--qf"]
        assert argv[-1] == str(srpm)

    def test_build_requires_keep_version_constraints(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = _header(
            "PyYAML",
            "6.0.3",
            "6.fc45",
            ("gcc", "", ""),
            ("python3dist(cython)", ">=", "3"),
            ("(python3dist(tomli) if python3-devel < 3.11)", "", ""),
        )

        reqs = read_srpm_header(str(srpm)).build_requires

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

        names = [r.name for r in read_srpm_header(str(srpm)).build_requires]

        assert names == ["python3-devel", "make"]

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            read_srpm_header("/nonexistent/file.src.rpm")

    def test_not_an_srpm(self, tmp_path):
        path = tmp_path / "not-an-srpm.txt"
        path.write_text("x")
        with pytest.raises(InvalidSRPMError, match="Not a valid SRPM"):
            read_srpm_header(str(path))

    def test_rpm_missing(self, srpm, mock_subprocess_run):
        mock_subprocess_run.side_effect = FileNotFoundError("rpm")
        with pytest.raises(InvalidSRPMError, match="rpm not found"):
            read_srpm_header(str(srpm))

    def test_rpm_timeout(self, srpm, mock_subprocess_run):
        import subprocess

        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd="rpm", timeout=60)
        with pytest.raises(InvalidSRPMError, match="timed out"):
            read_srpm_header(str(srpm))

    def test_rpm_fails(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stderr = "error: not an rpm package\n"
        with pytest.raises(InvalidSRPMError, match="not an rpm package"):
            read_srpm_header(str(srpm))

    def test_unexpected_output(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = "hello\n"
        with pytest.raises(InvalidSRPMError, match="Unexpected rpm output"):
            read_srpm_header(str(srpm))


class TestGetBuildRequires:
    def test_returns_build_requirements(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = _header(
            "pkg", "1", "1", ("make", ">=", "4.0"), ("gcc", "", "")
        )

        reqs = get_build_requires(str(srpm))

        assert all(isinstance(r, BuildRequirement) for r in reqs)
        assert [str(r) for r in reqs] == ["make >= 4.0", "gcc"]


class TestGetPackageInfoFromSrpm:
    def test_reads_header(self, srpm, mock_subprocess_run):
        mock_subprocess_run.return_value.stdout = _header("hello", "2.12.3", "2.fc45")

        info = get_package_info_from_srpm(str(srpm), use_cache=False)

        assert info.nvr == "hello-2.12.3-2.fc45"

    def test_uses_cache(self, srpm, mock_subprocess_run, mocker):
        cached = PackageInfo("cached", "1", "1", [], [])
        mocker.patch("vibebuild._spec_cache.get", return_value=cached)

        assert get_package_info_from_srpm(str(srpm)) is cached
        mock_subprocess_run.assert_not_called()

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            get_package_info_from_srpm("/nonexistent/file.src.rpm")
