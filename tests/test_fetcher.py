"""Tests for vibebuild.fetcher module."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import subprocess

from vibebuild.fetcher import SRPMFetcher, SRPMSource
from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError


class TestSRPMSource:
    def test_default_values(self):
        source = SRPMSource(name="test", base_url="https://example.internal")

        assert source.name == "test"
        assert source.base_url == "https://example.internal"
        assert source.koji_server is None
        assert source.priority == 100

    def test_with_all_values(self):
        source = SRPMSource(
            name="fedora",
            base_url="https://koji.fedoraproject.org",
            koji_server="https://koji.fedoraproject.org/kojihub",
            priority=10
        )

        assert source.koji_server == "https://koji.fedoraproject.org/kojihub"
        assert source.priority == 10


class TestSRPMFetcher:
    def test_default_initialization(self, tmp_path):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        assert fetcher.download_dir == tmp_path
        assert len(fetcher.sources) == 2
        assert fetcher.fedora_release == "rawhide"
        assert fetcher._cache == {}

    def test_custom_sources(self, tmp_path):
        custom_sources = [
            SRPMSource(name="custom", base_url="https://custom.internal", priority=5)
        ]
        fetcher = SRPMFetcher(download_dir=str(tmp_path), sources=custom_sources)

        assert len(fetcher.sources) == 1
        assert fetcher.sources[0].name == "custom"

    def test_sources_sorted_by_priority(self, tmp_path):
        sources = [
            SRPMSource(name="low-priority", base_url="https://low.internal", priority=100),
            SRPMSource(name="high-priority", base_url="https://high.internal", priority=10),
        ]
        fetcher = SRPMFetcher(download_dir=str(tmp_path), sources=sources)

        assert fetcher.sources[0].name == "high-priority"
        assert fetcher.sources[1].name == "low-priority"

    def test_download_dir_created(self, tmp_path):
        download_dir = tmp_path / "new_dir"
        assert not download_dir.exists()

        fetcher = SRPMFetcher(download_dir=str(download_dir))

        assert download_dir.exists()

    def test_download_srpm_returns_cached(self, tmp_path, mock_subprocess_run):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        cached_path = tmp_path / "cached.src.rpm"
        cached_path.write_text("cached srpm")
        fetcher._cache["test-pkg-latest"] = str(cached_path)

        result = fetcher.download_srpm("test-pkg")

        assert result == str(cached_path)
        mock_subprocess_run.assert_not_called()

    def test_download_srpm_cache_miss_if_file_deleted(self, tmp_path, mocker):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher._cache["test-pkg-latest"] = "/nonexistent/path.src.rpm"
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        mock_server.getLatestBuilds.return_value = [
            {"nvr": "test-pkg-1.0-1.fc40", "name": "test-pkg", "version": "1.0", "release": "1.fc40"}
        ]
        pkg_dir = tmp_path / "test-pkg"
        pkg_dir.mkdir()
        srpm_file = pkg_dir / "test-pkg-1.0-1.fc40.src.rpm"
        srpm_file.write_text("fake srpm")

        result = fetcher.download_srpm("test-pkg")

        assert result == str(srpm_file)

    def test_download_srpm_from_koji_with_version(self, tmp_path, mocker):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuild.return_value = {
            "nvr": "test-pkg-1.0-1.fc40", "name": "test-pkg", "version": "1.0", "release": "1.fc40"
        }
        pkg_dir = tmp_path / "test-pkg"
        pkg_dir.mkdir()
        srpm_file = pkg_dir / "test-pkg-1.0-1.fc40.src.rpm"
        srpm_file.write_text("fake srpm")

        result = fetcher.download_srpm("test-pkg", version="1.0-1.fc40")

        assert "test-pkg" in result
        assert result.endswith(".src.rpm")

    def test_download_srpm_from_koji_latest(self, tmp_path, mocker):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        mock_server.getLatestBuilds.return_value = [
            {"nvr": "test-pkg-2.0-1.fc42", "name": "test-pkg", "version": "2.0", "release": "1.fc42"}
        ]
        pkg_dir = tmp_path / "test-pkg"
        pkg_dir.mkdir()
        srpm_file = pkg_dir / "test-pkg-2.0-1.fc42.src.rpm"
        srpm_file.write_text("fake srpm")

        result = fetcher.download_srpm("test-pkg")

        assert result == str(srpm_file)

    def test_download_srpm_fallback_to_rawhide(self, tmp_path, mocker):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        # First call (f42 tag) returns empty, second call (rawhide) returns build
        mock_server.getLatestBuilds.side_effect = [
            [],  # f42 tag
            [{"nvr": "test-pkg-3.0-1.fc42", "name": "test-pkg", "version": "3.0", "release": "1.fc42"}],  # rawhide
        ]
        pkg_dir = tmp_path / "test-pkg"
        pkg_dir.mkdir()
        srpm_file = pkg_dir / "test-pkg-3.0-1.fc42.src.rpm"
        srpm_file.write_text("fake srpm")

        result = fetcher.download_srpm("test-pkg")

        assert result == str(srpm_file)

    def test_download_srpm_raises_not_found(self, tmp_path, mocker):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        mock_server.getLatestBuilds.return_value = []

        with pytest.raises(SRPMNotFoundError, match="Could not find SRPM"):
            fetcher.download_srpm("nonexistent-pkg")

    def test_download_srpm_no_srpm_after_download(self, tmp_path, mocker):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        mock_server.getLatestBuilds.return_value = [
            {"nvr": "test-pkg-1.0-1.fc40", "name": "test-pkg", "version": "1.0", "release": "1.fc40"}
        ]
        mocker.patch.object(fetcher, '_download_file')
        pkg_dir = tmp_path / "test-pkg"
        pkg_dir.mkdir()
        # Don't create the srpm file - simulates download failure

        with pytest.raises(SRPMNotFoundError, match="Failed to download SRPM"):
            fetcher.download_srpm("test-pkg")

    def test_search_fedora_src_success(self, tmp_path, mocker):
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = {
            "projects": [
                {"name": "python3"},
                {"name": "python3-devel"},
                {"name": "python3-libs"}
            ]
        }
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.search_fedora_src("python3")

        assert "python3" in result
        assert "python3-devel" in result

    def test_search_fedora_src_no_results(self, tmp_path, mocker):
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = {"projects": []}
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.search_fedora_src("nonexistent")

        assert result == []

    def test_get_package_versions_success(self, tmp_path, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = """python3-3.11.0-1.fc40
python3-3.10.0-1.fc39
python3-3.9.0-1.fc38
"""
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.get_package_versions("python3")

        assert "3.11.0" in result
        assert "3.10.0" in result
        assert "3.9.0" in result

    def test_get_package_versions_no_results(self, tmp_path, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stdout = ""
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.get_package_versions("nonexistent")

        assert result == []

    def test_clear_cache(self, tmp_path):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher._cache["pkg1"] = "/path/to/pkg1.src.rpm"
        fetcher._cache["pkg2"] = "/path/to/pkg2.src.rpm"

        fetcher.clear_cache()

        assert fetcher._cache == {}

    def test_cleanup_removes_directory(self, tmp_path):
        download_dir = tmp_path / "to_cleanup"
        download_dir.mkdir()
        (download_dir / "some_file.txt").write_text("test")
        fetcher = SRPMFetcher(download_dir=str(download_dir))
        fetcher._cache["test"] = "value"

        fetcher.cleanup()

        assert not download_dir.exists()
        assert fetcher._cache == {}


class TestSRPMFetcherWithRequests:
    @pytest.fixture
    def mock_requests(self, mocker):
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock = mocker.patch("vibebuild.fetcher.requests")
        mock.get.return_value.status_code = 200
        return mock

    def test_download_from_src_success(self, tmp_path, mock_subprocess_run, mock_requests):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=1)
        ]
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.text = """
Name: test-pkg
Version: 1.0
Release: 1
Source0: test-pkg-1.0.tar.gz
"""
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = ""
        work_dir = tmp_path / "test-pkg" / "build"
        work_dir.mkdir(parents=True)
        srpm_file = work_dir / "test-pkg-1.0-1.src.rpm"
        srpm_file.write_text("fake srpm")

        with patch.object(fetcher, '_download_file'):
            with patch("vibebuild.fetcher.Path.glob") as mock_glob:
                mock_glob.return_value = [srpm_file]
                result = fetcher.download_srpm("test-pkg")

        assert "test-pkg" in result

    def test_download_from_src_spec_not_found(self, tmp_path, mock_requests):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=1)
        ]
        mock_requests.get.return_value.status_code = 404

        with pytest.raises(SRPMNotFoundError):
            fetcher.download_srpm("nonexistent-pkg")

    def test_search_fedora_src_with_requests(self, tmp_path, mock_requests):
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = {
            "projects": [
                {"name": "python3"},
                {"name": "python3-devel"}
            ]
        }
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.search_fedora_src("python3")

        assert "python3" in result
        assert "python3-devel" in result


class TestSRPMFetcherExtractSources:
    def test_extract_sources_from_spec(self, tmp_path):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        spec_content = """
Name: test
Version: 1.0
Source0: https://example.internal/test-1.0.tar.gz
Source1: test-config.conf
Source2: https://cdn.internal/patches/patch1.diff
"""

        result = fetcher._extract_sources(spec_content)

        assert "https://example.internal/test-1.0.tar.gz" in result
        assert "test-config.conf" in result
        assert "https://cdn.internal/patches/patch1.diff" in result

    def test_extract_sources_empty(self, tmp_path):
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        spec_content = """
Name: test
Version: 1.0
"""

        result = fetcher._extract_sources(spec_content)

        assert result == []


class TestSRPMFetcherGetEnv:
    def test_get_env_with_no_ssl_verify(self, tmp_path):
        """_get_env with no_ssl_verify=True should set SSL env vars."""
        fetcher = SRPMFetcher(download_dir=str(tmp_path), no_ssl_verify=True)

        env = fetcher._get_env()

        assert env is not None
        assert env["PYTHONHTTPSVERIFY"] == "0"
        assert env["REQUESTS_CA_BUNDLE"] == ""
        assert env["CURL_CA_BUNDLE"] == ""

    def test_get_env_without_no_ssl_verify(self, tmp_path):
        """_get_env without no_ssl_verify should return None."""
        fetcher = SRPMFetcher(download_dir=str(tmp_path), no_ssl_verify=False)

        env = fetcher._get_env()

        assert env is None


class TestSRPMFetcherDownloadWithNameResolver:
    def test_download_srpm_with_name_resolver(self, tmp_path, mocker):
        """download_srpm with name_resolver should try multiple candidates."""
        mock_resolver = Mock()
        mock_resolver.get_download_candidates.return_value = ["python-requests", "python3-requests"]
        fetcher = SRPMFetcher(download_dir=str(tmp_path), name_resolver=mock_resolver)
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuild.return_value = {
            "nvr": "python-requests-2.0-1", "name": "python-requests", "version": "2.0", "release": "1"
        }
        pkg_dir = tmp_path / "python-requests"
        pkg_dir.mkdir()
        srpm_file = pkg_dir / "python-requests-2.0-1.src.rpm"
        srpm_file.write_text("fake srpm")

        result = fetcher.download_srpm("python-requests", version="2.0-1")

        assert "python-requests" in result
        mock_resolver.get_download_candidates.assert_called_once_with("python-requests")


class TestSRPMFetcherKojiEdgeCases:
    def test_koji_connection_error(self, tmp_path, mocker):
        """Connection error when connecting to Koji XML-RPC."""
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="koji", base_url="https://koji.internal",
                       koji_server="https://koji.internal/kojihub", priority=1)
        ]
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.side_effect = Exception("connection refused")
        mock_server.getTag.side_effect = Exception("connection refused")
        mock_server.getLatestBuilds.side_effect = Exception("connection refused")

        with pytest.raises(SRPMNotFoundError, match="Could not find SRPM"):
            fetcher.download_srpm("test-pkg")

    def test_koji_package_not_found(self, tmp_path, mocker):
        """Koji returns empty builds for package."""
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="koji", base_url="https://koji.internal",
                       koji_server="https://koji.internal/kojihub", priority=1)
        ]
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        mock_server.getLatestBuilds.return_value = []

        with pytest.raises(SRPMNotFoundError, match="Could not find SRPM"):
            fetcher.download_srpm("test-pkg")

    def test_download_file_failure(self, tmp_path, mocker):
        """Download failure should raise."""
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="koji", base_url="https://koji.internal",
                       koji_server="https://koji.internal/kojihub", priority=1)
        ]
        mock_server = MagicMock()
        mocker.patch("xmlrpc.client.ServerProxy", return_value=mock_server)
        mock_server.getBuildTarget.return_value = {"dest_tag_name": "f42"}
        mock_server.getLatestBuilds.return_value = [
            {"nvr": "test-pkg-1.0-1.fc40", "name": "test-pkg", "version": "1.0", "release": "1.fc40"}
        ]
        mocker.patch.object(fetcher, '_download_file', side_effect=Exception("download error"))

        with pytest.raises(SRPMNotFoundError, match="Could not find SRPM"):
            fetcher.download_srpm("test-pkg")


class TestSRPMFetcherSrcEdgeCases:
    def test_http_source_url_in_spec(self, tmp_path, mock_subprocess_run, mocker):
        """HTTP source URLs should be downloaded directly."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.text = "Name: test\nVersion: 1.0\nRelease: 1\nSource0: https://example.internal/test-1.0.tar.gz\n"
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=1)
        ]
        mock_subprocess_run.return_value.returncode = 0
        work_dir = tmp_path / "test" / "build"
        work_dir.mkdir(parents=True)
        srpm_file = work_dir / "test-1.0-1.src.rpm"
        srpm_file.write_text("fake srpm")

        with patch.object(fetcher, '_download_file'):
            with patch("vibebuild.fetcher.Path.glob") as mock_glob:
                mock_glob.return_value = [srpm_file]
                result = fetcher.download_srpm("test")

        assert "test" in result

    def test_lookaside_exception_passes(self, tmp_path, mock_subprocess_run, mocker):
        """Lookaside download exception should be silently ignored."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.text = "Name: test\nVersion: 1.0\nRelease: 1\nSource0: local-file.tar.gz\n"
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=1)
        ]
        mock_subprocess_run.return_value.returncode = 0
        work_dir = tmp_path / "test" / "build"
        work_dir.mkdir(parents=True)
        srpm_file = work_dir / "test-1.0-1.src.rpm"
        srpm_file.write_text("fake srpm")

        with patch.object(fetcher, '_download_file', side_effect=Exception("download failed")):
            with patch("vibebuild.fetcher.Path.glob") as mock_glob:
                mock_glob.return_value = [srpm_file]
                result = fetcher.download_srpm("test")

        assert "test" in result

    def test_rpmbuild_failure(self, tmp_path, mock_subprocess_run, mocker):
        """rpmbuild failure should raise VibeBuildError."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.text = "Name: test\nVersion: 1.0\nRelease: 1\n"
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=1)
        ]
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stderr = "rpmbuild error"

        with pytest.raises(SRPMNotFoundError):
            fetcher.download_srpm("test")

    def test_no_srpm_after_rpmbuild(self, tmp_path, mock_subprocess_run, mocker):
        """No SRPM after rpmbuild should raise SRPMNotFoundError."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.text = "Name: test\nVersion: 1.0\nRelease: 1\n"
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = [
            SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=1)
        ]
        mock_subprocess_run.return_value.returncode = 0

        with pytest.raises(SRPMNotFoundError):
            fetcher.download_srpm("test")


class TestSRPMFetcherDownloadFile:
    def test_download_file_without_requests_curl(self, tmp_path, mock_subprocess_run, mocker):
        """_download_file without requests should use curl."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", False)
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_subprocess_run.return_value.returncode = 0

        fetcher._download_file("https://example.internal/file.tar.gz", tmp_path / "file.tar.gz")

        call_args = mock_subprocess_run.call_args[0][0]
        assert "curl" in call_args
        assert "-L" in call_args

    def test_download_file_without_requests_curl_ssl(self, tmp_path, mock_subprocess_run, mocker):
        """_download_file without requests + no_ssl_verify should use curl -k."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", False)
        fetcher = SRPMFetcher(download_dir=str(tmp_path), no_ssl_verify=True)
        mock_subprocess_run.return_value.returncode = 0

        fetcher._download_file("https://example.internal/file.tar.gz", tmp_path / "file.tar.gz")

        call_args = mock_subprocess_run.call_args[0][0]
        assert "-k" in call_args

    def test_download_file_with_requests(self, tmp_path, mocker):
        """_download_file with requests should use requests.get."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_response = Mock()
        mock_response.iter_content.return_value = [b"data"]
        mock_requests.get.return_value = mock_response
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        dest = tmp_path / "file.tar.gz"

        fetcher._download_file("https://example.internal/file.tar.gz", dest)

        mock_requests.get.assert_called_once()
        assert dest.exists()


class TestSRPMFetcherDownloadEmptySources:
    def test_download_srpm_empty_sources_no_errors(self, tmp_path, mock_subprocess_run):
        """download_srpm with empty sources list should raise without error details."""
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        fetcher.sources = []  # Override after init (empty list is falsy in constructor)

        with pytest.raises(SRPMNotFoundError, match="Could not find SRPM"):
            fetcher.download_srpm("test-pkg")


class TestSRPMFetcherGetPackageVersionsEdgeCases:
    def test_get_package_versions_empty_lines_and_no_hyphen(self, tmp_path, mock_subprocess_run):
        """get_package_versions with empty lines and names without hyphen."""
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "python3-3.11.0-1.fc40\n\nsimplename\n"
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.get_package_versions("python3")

        assert "3.11.0" in result


class TestSRPMFetcherCleanupEdgeCases:
    def test_cleanup_nonexistent_dir(self, tmp_path):
        """cleanup should handle non-existing download_dir."""
        download_dir = tmp_path / "nonexistent"
        fetcher = SRPMFetcher.__new__(SRPMFetcher)
        fetcher.download_dir = download_dir
        fetcher._cache = {"test": "value"}

        fetcher.cleanup()

        assert fetcher._cache == {}


class TestSearchFedoraSrcEdgeCases:
    def test_search_without_requests_uses_koji(self, tmp_path, mock_subprocess_run, mocker):
        """search_fedora_src without requests should use koji CLI."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", False)
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "python3\npython3-devel\n"

        result = fetcher.search_fedora_src("python3")

        assert "python3" in result
        assert "python3-devel" in result

    def test_search_without_requests_koji_failure(self, tmp_path, mock_subprocess_run, mocker):
        """search_fedora_src without requests should return [] on failure."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", False)
        fetcher = SRPMFetcher(download_dir=str(tmp_path))
        mock_subprocess_run.return_value.returncode = 1

        result = fetcher.search_fedora_src("nonexistent")

        assert result == []

    def test_search_with_requests_exception(self, tmp_path, mocker):
        """search_fedora_src with requests should return [] on exception."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.side_effect = Exception("connection error")
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.search_fedora_src("python3")

        assert result == []

    def test_search_with_requests_non_200(self, tmp_path, mocker):
        """search_fedora_src with non-200 status should return []."""
        mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
        mock_requests = mocker.patch("vibebuild.fetcher.requests")
        mock_requests.get.return_value.status_code = 500
        fetcher = SRPMFetcher(download_dir=str(tmp_path))

        result = fetcher.search_fedora_src("python3")

        assert result == []
