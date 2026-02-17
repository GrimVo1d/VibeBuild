"""Tests for vibebuild.analyzer module."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from vibebuild.analyzer import (
    BuildRequirement,
    PackageInfo,
    SpecAnalyzer,
    get_build_requires,
    get_package_info_from_srpm,
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
            name="test-package",
            version="1.0.0",
            release="1",
            build_requires=[],
            source_urls=[]
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
        spec.write_text("""
Name: test-pkg
Version: 1.0
Release: 1
# BuildRequires: should-be-ignored
BuildRequires: actual-dep
""")
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
        spec.write_text("""
Name: my-package
Version: 2.0
Release: 1
Source0: %{name}-%{version}.tar.gz
""")
        analyzer = SpecAnalyzer()

        result = analyzer.analyze_spec(str(spec))

        assert "my-package-2.0.tar.gz" in result.source_urls[0]


class TestGetBuildRequires:
    def test_returns_list_of_strings(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "python3-devel\ngcc\nmake >= 4.0\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("vibebuild.analyzer.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.suffix = ".rpm"
            mock_path.return_value.name = "test.src.rpm"
            result = get_build_requires("/fake/test.src.rpm")

        assert isinstance(result, list)
        assert "python3-devel" in result
        assert "gcc" in result
        assert "make" in result

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            get_build_requires("/nonexistent/file.src.rpm")

    def test_raises_on_invalid_srpm_extension(self, tmp_path):
        invalid_file = tmp_path / "not-an-srpm.txt"
        invalid_file.write_text("not an srpm")

        with pytest.raises(InvalidSRPMError, match="Not a valid SRPM"):
            get_build_requires(str(invalid_file))

    def test_raises_on_rpm2cpio_failure(self, tmp_path, mock_subprocess_run):
        import subprocess
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm content")
        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=['rpm2cpio'],
            stderr=b"rpm2cpio failed"
        )

        with pytest.raises(InvalidSRPMError, match="Failed to extract"):
            get_build_requires(str(srpm))

    def test_filters_rpmlib_dependencies(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = """
python3-devel
rpmlib(CompressedFileNames) <= 3.0.4-1
rpmlib(FileDigests) <= 4.6.0-1
gcc
/bin/sh
"""
        mock_subprocess_run.return_value.stderr = ""

        with patch("vibebuild.analyzer.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.suffix = ".rpm"
            mock_path.return_value.name = "test.src.rpm"
            result = get_build_requires("/fake/test.src.rpm")

        assert "python3-devel" in result
        assert "gcc" in result
        assert not any("rpmlib" in r for r in result)
        assert not any(r.startswith("/") for r in result)


class TestGetPackageInfoFromSrpm:
    def test_extracts_package_info(self, tmp_path, mock_subprocess_run, sample_spec_content):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        spec_file = tmp_path / "test-package.spec"
        spec_file.write_text(sample_spec_content)
        mock_subprocess_run.return_value.returncode = 0

        with patch("vibebuild.analyzer.tempfile.TemporaryDirectory") as mock_tmpdir:
            mock_tmpdir.return_value.__enter__ = Mock(return_value=str(tmp_path))
            mock_tmpdir.return_value.__exit__ = Mock(return_value=False)
            result = get_package_info_from_srpm(str(srpm))

        assert result.name == "test-package"
        assert result.version == "1.0"

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            get_package_info_from_srpm("/nonexistent/file.src.rpm")

    def test_raises_when_no_spec_found(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "empty.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0

        with patch("vibebuild.analyzer.tempfile.TemporaryDirectory") as mock_tmpdir:
            empty_dir = tmp_path / "empty_extract"
            empty_dir.mkdir()
            mock_tmpdir.return_value.__enter__ = Mock(return_value=str(empty_dir))
            mock_tmpdir.return_value.__exit__ = Mock(return_value=False)

            with pytest.raises(InvalidSRPMError, match="No spec file found"):
                get_package_info_from_srpm(str(srpm))


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
        spec.write_text("Name: test-pkg\nVersion: 1.0\nRelease: 1\nBuildRequires: %{?nonexistent_macro}\n")
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

    def test_duplicate_requires_in_rpm_output(self, mock_subprocess_run):
        """get_build_requires should deduplicate requires."""
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "python3-devel\ngcc\npython3-devel\ngcc\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("vibebuild.analyzer.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.suffix = ".rpm"
            mock_path.return_value.name = "test.src.rpm"
            result = get_build_requires("/fake/test.src.rpm")

        assert result.count("python3-devel") == 1
        assert result.count("gcc") == 1


class TestGetBuildRequiresEdgeCases:
    def test_rpm2cpio_not_found(self, tmp_path, mock_subprocess_run):
        """get_build_requires should handle rpm2cpio not found."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm content")
        mock_subprocess_run.side_effect = FileNotFoundError("rpm2cpio not found")

        with pytest.raises(InvalidSRPMError, match="rpm2cpio not found"):
            get_build_requires(str(srpm))

    def test_rpm_qp_failure(self, tmp_path, mock_subprocess_run):
        """get_build_requires should raise on rpm -qp failure."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm content")
        mock_subprocess_run.side_effect = [
            Mock(returncode=0, stdout=b"", stderr=b""),  # rpm2cpio
            Mock(returncode=1, stdout="", stderr="rpm query failed"),  # rpm -qp
        ]

        with pytest.raises(InvalidSRPMError, match="Failed to query"):
            get_build_requires(str(srpm))

    def test_stdout_with_empty_lines(self, mock_subprocess_run):
        """get_build_requires should skip empty lines in output."""
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "\n\npython3-devel\n\ngcc\n\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("vibebuild.analyzer.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.suffix = ".rpm"
            mock_path.return_value.name = "test.src.rpm"
            result = get_build_requires("/fake/test.src.rpm")

        assert "python3-devel" in result
        assert "gcc" in result
        assert "" not in result
