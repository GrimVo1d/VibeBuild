"""Tests for vibebuild.builder module."""

import subprocess
from unittest.mock import MagicMock, Mock, patch

import pytest

from vibebuild.analyzer import PackageInfo
from vibebuild.builder import (
    BuildResult,
    BuildStatus,
    BuildTask,
    KojiBuilder,
)
from vibebuild.exceptions import KojiBuildError, KojiConnectionError


class TestBuildStatus:
    def test_status_values(self):
        assert BuildStatus.PENDING.value == "pending"
        assert BuildStatus.BUILDING.value == "building"
        assert BuildStatus.COMPLETE.value == "complete"
        assert BuildStatus.FAILED.value == "failed"
        assert BuildStatus.CANCELED.value == "canceled"


class TestBuildTask:
    def test_default_values(self):
        task = BuildTask(
            package_name="test-pkg", srpm_path="/path/to/test.src.rpm", target="fedora-target"
        )

        assert task.package_name == "test-pkg"
        assert task.srpm_path == "/path/to/test.src.rpm"
        assert task.target == "fedora-target"
        assert task.task_id is None
        assert task.status == BuildStatus.PENDING
        assert task.error_message is None
        assert task.nvr is None

    def test_with_all_values(self):
        task = BuildTask(
            package_name="test-pkg",
            srpm_path="/path/to/test.src.rpm",
            target="fedora-target",
            task_id=12345,
            status=BuildStatus.COMPLETE,
            nvr="test-pkg-1.0-1.fc40",
        )

        assert task.task_id == 12345
        assert task.status == BuildStatus.COMPLETE
        assert task.nvr == "test-pkg-1.0-1.fc40"


class TestBuildResult:
    def test_default_values(self):
        result = BuildResult(success=True)

        assert result.success is True
        assert result.tasks == []
        assert result.failed_packages == []
        assert result.built_packages == []
        assert result.total_time == 0.0

    def test_with_tasks(self):
        task1 = BuildTask(package_name="pkg1", srpm_path="/path/1", target="target")
        task2 = BuildTask(package_name="pkg2", srpm_path="/path/2", target="target")
        result = BuildResult(
            success=True, tasks=[task1, task2], built_packages=["pkg1", "pkg2"], total_time=120.5
        )

        assert len(result.tasks) == 2
        assert "pkg1" in result.built_packages
        assert result.total_time == 120.5


class TestKojiBuilder:
    def test_default_initialization(self):
        builder = KojiBuilder()

        assert builder.koji_server == "https://koji.fedoraproject.org/kojihub"
        assert builder.target == "fedora-target"
        assert builder.build_tag == "fedora-build"
        assert builder.scratch is False
        assert builder.nowait is False

    def test_custom_initialization(self):
        builder = KojiBuilder(
            koji_server="https://custom.koji.internal/kojihub",
            koji_web_url="https://custom.koji.internal/koji",
            cert="/path/to/cert.pem",
            serverca="/path/to/ca.crt",
            target="custom-target",
            build_tag="custom-build",
            scratch=True,
            nowait=True,
            download_dir="/tmp/downloads",
        )

        assert builder.koji_server == "https://custom.koji.internal/kojihub"
        assert builder.cert == "/path/to/cert.pem"
        assert builder.target == "custom-target"
        assert builder.scratch is True
        assert builder.nowait is True

    def test_run_koji_command(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "output"
        builder = KojiBuilder()

        result = builder._run_koji("list-tags")

        assert result.returncode == 0
        call_args = mock_subprocess_run.call_args[0][0]
        assert "koji" in call_args
        assert "--server=https://koji.fedoraproject.org/kojihub" in call_args
        assert "list-tags" in call_args

    def test_run_koji_with_cert(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        builder = KojiBuilder(cert="/path/to/cert.pem", serverca="/path/to/ca.crt")

        builder._run_koji("list-tags")

        call_args = mock_subprocess_run.call_args[0][0]
        assert "--cert=/path/to/cert.pem" in call_args
        # --serverca is not passed on CLI (RHEL9 compat); read from ~/.koji/config
        assert not any("--serverca" in a for a in call_args)

    def test_run_koji_timeout(self, mock_subprocess_run):
        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd="koji", timeout=60)
        builder = KojiBuilder()

        with pytest.raises(KojiConnectionError, match="timed out"):
            builder._run_koji("list-tags")

    def test_build_package_success(self, tmp_path, mock_subprocess_run, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        spec_file = tmp_path / "test-package.spec"
        spec_file.write_text(sample_spec_content)
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345\nTask info: id=12345"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=False)

        assert task.package_name == "test-package"
        assert task.task_id == 12345
        assert task.status == BuildStatus.BUILDING

    def test_build_package_with_wait(self, tmp_path, mock_subprocess_run, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345\nBuild complete"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder, "_poll_build", return_value=BuildStatus.COMPLETE):
                task = builder.build_package(str(srpm), wait=True)

        assert task.status == BuildStatus.COMPLETE

    def test_build_package_scratch(self, tmp_path, mock_subprocess_run, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder(scratch=True)

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        call_args = mock_subprocess_run.call_args[0][0]
        assert "--scratch" in call_args

    def test_build_package_file_not_found(self):
        builder = KojiBuilder()

        with pytest.raises(FileNotFoundError):
            builder.build_package("/nonexistent/path.src.rpm")

    def test_build_package_failure(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stderr = "Build failed: dependency error"
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError, match="Build failed"):
                builder.build_package(str(srpm))

    def test_wait_for_repo_success(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Repo ready"
        builder = KojiBuilder()

        result = builder.wait_for_repo()

        assert result is True
        call_args = mock_subprocess_run.call_args[0][0]
        assert "wait-repo" in call_args
        assert "fedora-build" in call_args

    def test_wait_for_repo_custom_tag(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        builder = KojiBuilder()

        builder.wait_for_repo(tag="custom-tag")

        call_args = mock_subprocess_run.call_args[0][0]
        assert "custom-tag" in call_args

    def test_wait_for_repo_failure(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stderr = "Timeout waiting for repo"
        builder = KojiBuilder()

        result = builder.wait_for_repo()

        assert result is False

    def test_get_build_status_complete(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task: 12345\nState: closed"
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.COMPLETE

    def test_get_build_status_failed(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task: 12345\nState: failed"
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.FAILED

    def test_get_build_status_building(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task: 12345\nState: open"
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.BUILDING

    def test_get_build_status_canceled(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task: 12345\nState: canceled"
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.CANCELED

    def test_get_build_status_error(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 1
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.FAILED

    def test_cancel_build_success(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 0
        builder = KojiBuilder()

        result = builder.cancel_build(12345)

        assert result is True
        call_args = mock_subprocess_run.call_args[0][0]
        assert "cancel" in call_args
        assert "12345" in call_args

    def test_cancel_build_failure(self, mock_subprocess_run):
        mock_subprocess_run.return_value.returncode = 1
        builder = KojiBuilder()

        result = builder.cancel_build(12345)

        assert result is False


class TestSubmitBuild:
    def test_submit_build_returns_building_status(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder._submit_build(str(srpm))

        assert task.status == BuildStatus.BUILDING
        assert task.task_id == 12345
        assert task.package_name == "test-pkg"

    def test_submit_build_parses_task_info_format(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task info: id=67890"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder._submit_build(str(srpm))

        assert task.task_id == 67890
        assert task.status == BuildStatus.BUILDING

    def test_submit_build_file_not_found(self):
        builder = KojiBuilder()

        with pytest.raises(FileNotFoundError):
            builder._submit_build("/nonexistent/path.src.rpm")

    def test_submit_build_failure_raises(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stderr = "Build submission failed"
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError):
                builder._submit_build(str(srpm))


class TestPollBuilds:
    def test_poll_builds_all_complete(self, mock_subprocess_run):
        builder = KojiBuilder()
        tasks = [
            BuildTask(
                package_name="pkg1",
                srpm_path="/p1",
                target="t",
                task_id=101,
                status=BuildStatus.BUILDING,
            ),
            BuildTask(
                package_name="pkg2",
                srpm_path="/p2",
                target="t",
                task_id=102,
                status=BuildStatus.BUILDING,
            ),
        ]
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "State: closed"

        with patch("vibebuild.builder.time.sleep"):
            builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.COMPLETE
        assert tasks[1].status == BuildStatus.COMPLETE

    def test_poll_builds_mixed_results(self, mock_subprocess_run):
        builder = KojiBuilder()
        tasks = [
            BuildTask(
                package_name="pkg1",
                srpm_path="/p1",
                target="t",
                task_id=201,
                status=BuildStatus.BUILDING,
            ),
            BuildTask(
                package_name="pkg2",
                srpm_path="/p2",
                target="t",
                task_id=202,
                status=BuildStatus.BUILDING,
            ),
        ]
        mock_subprocess_run.return_value.returncode = 0

        # pkg1 closed, pkg2 failed
        def taskinfo_side_effect(*args, **kwargs):
            cmd = args[0]
            if "201" in cmd:
                return Mock(returncode=0, stdout="State: closed", stderr="")
            elif "202" in cmd:
                return Mock(returncode=0, stdout="State: failed", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        mock_subprocess_run.side_effect = taskinfo_side_effect

        with patch("vibebuild.builder.time.sleep"):
            builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.COMPLETE
        assert tasks[1].status == BuildStatus.FAILED

    def test_poll_builds_timeout(self, mock_subprocess_run):
        builder = KojiBuilder()
        tasks = [
            BuildTask(
                package_name="pkg1",
                srpm_path="/p1",
                target="t",
                task_id=301,
                status=BuildStatus.BUILDING,
            ),
        ]
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "State: open"

        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            # First few calls return 0 (start), then exceed timeout
            if call_count <= 3:
                return 0
            return 9999

        with patch("vibebuild.builder.time.time", side_effect=fake_time):
            with patch("vibebuild.builder.time.sleep"):
                builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.FAILED
        assert "timed out" in tasks[0].error_message

    def test_poll_builds_no_task_id_skipped(self, mock_subprocess_run):
        builder = KojiBuilder()
        tasks = [
            BuildTask(
                package_name="pkg1",
                srpm_path="/p1",
                target="t",
                task_id=None,
                status=BuildStatus.BUILDING,
            ),
        ]

        builder._poll_builds(tasks)

        # Status unchanged — no task_id to poll
        assert tasks[0].status == BuildStatus.BUILDING

    def test_poll_builds_canceled(self, mock_subprocess_run):
        builder = KojiBuilder()
        tasks = [
            BuildTask(
                package_name="pkg1",
                srpm_path="/p1",
                target="t",
                task_id=401,
                status=BuildStatus.BUILDING,
            ),
        ]
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "State: canceled"

        with patch("vibebuild.builder.time.sleep"):
            builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.CANCELED


class TestKojiBuilderBuildWithDeps:
    def _make_complete_task(self, name, srpm_path):
        return BuildTask(
            package_name=name,
            srpm_path=str(srpm_path),
            target="fedora-target",
            task_id=12345,
            status=BuildStatus.COMPLETE,
        )

    def test_build_with_deps_no_missing_deps(
        self, tmp_path, mock_subprocess_run, sample_spec_content
    ):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                    with patch.object(
                        builder,
                        "build_package",
                        return_value=self._make_complete_task("test", srpm),
                    ):
                        result = builder.build_with_deps(str(srpm))

        assert result.success is True
        assert "test" in result.built_packages

    def test_build_with_deps_builds_dependencies_first(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "main.src.rpm"
        srpm.write_text("fake srpm")
        dep_srpm = tmp_path / "dep.src.rpm"
        dep_srpm.write_text("fake dep srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()
        from vibebuild.resolver import DependencyNode

        builder.resolver._dependency_graph = {
            "main": DependencyNode(name="main", srpm_path=str(srpm)),
            "dep": DependencyNode(name="dep", srpm_path=str(dep_srpm)),
        }

        dep_task = BuildTask(
            package_name="dep",
            srpm_path=str(dep_srpm),
            target="fedora-target",
            task_id=111,
            status=BuildStatus.BUILDING,
        )

        def mock_poll(tasks, **kwargs):
            for t in tasks:
                t.status = BuildStatus.COMPLETE

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="main", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(
                    builder.resolver, "get_build_chain", return_value=[["dep"], ["main"]]
                ):
                    with patch.object(builder, "_submit_build", return_value=dep_task):
                        with patch.object(builder, "_poll_builds", side_effect=mock_poll):
                            with patch.object(
                                builder,
                                "build_package",
                                return_value=self._make_complete_task("main", srpm),
                            ):
                                result = builder.build_with_deps(str(srpm))

        assert "dep" in result.built_packages
        assert "main" in result.built_packages

    def test_build_with_deps_file_not_found(self):
        builder = KojiBuilder()

        with pytest.raises(FileNotFoundError):
            builder.build_with_deps("/nonexistent/path.src.rpm")

    def test_build_with_deps_returns_time(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                    with patch.object(
                        builder,
                        "build_package",
                        return_value=self._make_complete_task("test", srpm),
                    ):
                        result = builder.build_with_deps(str(srpm))

        assert result.total_time >= 0


class TestKojiBuilderBuildChain:
    def test_build_chain_success(self, tmp_path, mock_subprocess_run):
        pkg1 = tmp_path / "pkg1.src.rpm"
        pkg1.write_text("fake")
        pkg2 = tmp_path / "pkg2.src.rpm"
        pkg2.write_text("fake")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()
        packages = [("pkg1", str(pkg1)), ("pkg2", str(pkg2))]

        task1 = BuildTask(
            package_name="pkg1",
            srpm_path=str(pkg1),
            target="fedora-target",
            task_id=1,
            status=BuildStatus.COMPLETE,
        )
        task2 = BuildTask(
            package_name="pkg2",
            srpm_path=str(pkg2),
            target="fedora-target",
            task_id=2,
            status=BuildStatus.COMPLETE,
        )

        with patch.object(builder, "build_package", side_effect=[task1, task2]):
            result = builder.build_chain(packages)

        assert result.success is True
        assert "pkg1" in result.built_packages
        assert "pkg2" in result.built_packages

    def test_build_chain_stops_on_failure(self, tmp_path, mock_subprocess_run):
        pkg1 = tmp_path / "pkg1.src.rpm"
        pkg1.write_text("fake")
        pkg2 = tmp_path / "pkg2.src.rpm"
        pkg2.write_text("fake")
        mock_subprocess_run.side_effect = [
            Mock(returncode=1, stderr="Build failed"),
        ]
        builder = KojiBuilder()
        packages = [("pkg1", str(pkg1)), ("pkg2", str(pkg2))]

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="pkg1", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            result = builder.build_chain(packages)

        assert result.success is False
        assert "pkg1" in result.failed_packages
        assert "pkg2" not in result.built_packages
        assert "pkg2" not in result.failed_packages


class TestKojiBuilderInit:
    def test_init_ml_import_error(self, mocker):
        """KojiBuilder should handle ImportError for ml_resolver."""
        mocker.patch(
            "vibebuild.builder.MLPackageResolver", side_effect=ImportError("no ml"), create=True
        )
        # Force the import to fail inside __init__
        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def mock_import(name, *args, **kwargs):
            if name == "vibebuild.ml_resolver":
                raise ImportError("no sklearn")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            builder = KojiBuilder(no_name_resolution=False, no_ml=False)

        # ML resolver should be None due to ImportError
        assert builder.name_resolver is not None

    def test_init_no_name_resolution(self):
        """KojiBuilder with no_name_resolution=True should have no name_resolver."""
        builder = KojiBuilder(no_name_resolution=True)
        assert builder.name_resolver is None

    def test_init_no_ml(self):
        """KojiBuilder with no_ml=True should have name_resolver but no ml_resolver."""
        builder = KojiBuilder(no_ml=True)
        assert builder.name_resolver is not None
        assert builder.name_resolver.ml_resolver is None

    def test_init_ml_resolver_available(self, mocker):
        """When ML resolver is available, it should be set on name_resolver."""
        mock_ml_instance = MagicMock()
        mock_ml_instance.is_available.return_value = True
        mock_ml_cls = MagicMock(return_value=mock_ml_instance)
        mock_module = MagicMock()
        mock_module.MLPackageResolver = mock_ml_cls
        mocker.patch.dict("sys.modules", {"vibebuild.ml_resolver": mock_module})

        builder = KojiBuilder(no_ml=False, no_name_resolution=False)
        assert builder.name_resolver is not None
        assert builder.name_resolver.ml_resolver == mock_ml_instance


class TestKojiBuilderGetEnv:
    def test_get_env_with_no_ssl_verify(self):
        builder = KojiBuilder(no_ssl_verify=True)

        env = builder._get_env()

        assert env is not None
        assert env["PYTHONHTTPSVERIFY"] == "0"
        assert env["REQUESTS_CA_BUNDLE"] == ""
        assert env["CURL_CA_BUNDLE"] == ""

    def test_get_env_without_no_ssl_verify(self):
        builder = KojiBuilder(no_ssl_verify=False)

        env = builder._get_env()

        assert env is None


class TestBuildPackageEdgeCases:
    def test_build_package_task_id_parse_created_task_nan(self, tmp_path, mock_subprocess_run):
        """ValueError when parsing task_id from 'Created task: NaN'."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: NaN\n"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=True)

        assert task.task_id is None

    def test_build_package_task_id_parse_task_info_nan(self, tmp_path, mock_subprocess_run):
        """ValueError when parsing task_id from 'Task info: id=NaN'."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task info: id=NaN\n"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=True)

        assert task.task_id is None


class TestBuildWithDepsEdgeCases:
    def test_srpm_resolver_callback_success(self, tmp_path, mock_subprocess_run):
        """The srpm_resolver callback in build_with_deps should download SRPMs."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()

        captured_resolver = {}

        def capture_resolver(name, path, srpm_resolver=None):
            captured_resolver["fn"] = srpm_resolver

        complete_task = BuildTask(
            package_name="test",
            srpm_path=str(srpm),
            target="fedora-target",
            task_id=12345,
            status=BuildStatus.COMPLETE,
        )

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(
                builder.resolver, "build_dependency_graph", side_effect=capture_resolver
            ):
                with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                    with patch.object(builder, "build_package", return_value=complete_task):
                        builder.build_with_deps(str(srpm))

        # Call the captured srpm_resolver
        assert captured_resolver["fn"] is not None
        with patch.object(builder.fetcher, "download_srpm", return_value="/path/to/dep.src.rpm"):
            dep_path = captured_resolver["fn"]("dep-pkg")
        assert dep_path == "/path/to/dep.src.rpm"

    def test_srpm_resolver_callback_exception(self, tmp_path, mock_subprocess_run):
        """The srpm_resolver callback should return None on exception."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()

        captured_resolver = {}

        def capture_resolver(name, path, srpm_resolver=None):
            captured_resolver["fn"] = srpm_resolver

        complete_task = BuildTask(
            package_name="test",
            srpm_path=str(srpm),
            target="fedora-target",
            task_id=12345,
            status=BuildStatus.COMPLETE,
        )

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(
                builder.resolver, "build_dependency_graph", side_effect=capture_resolver
            ):
                with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                    with patch.object(builder, "build_package", return_value=complete_task):
                        builder.build_with_deps(str(srpm))

        with patch.object(builder.fetcher, "download_srpm", side_effect=Exception("download fail")):
            dep_path = captured_resolver["fn"]("dep-pkg")
        assert dep_path is None

    def test_no_node_or_no_srpm_path_skips_package(self, tmp_path, mock_subprocess_run):
        """Package with no SRPM path should be skipped."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()
        from vibebuild.resolver import DependencyNode

        builder.resolver._dependency_graph = {
            "test": DependencyNode(name="test", srpm_path=str(srpm)),
            "no-srpm": DependencyNode(name="no-srpm", srpm_path=None),
        }

        complete_task = BuildTask(
            package_name="test",
            srpm_path=str(srpm),
            target="fedora-target",
            task_id=12345,
            status=BuildStatus.COMPLETE,
        )

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(
                    builder.resolver, "get_build_chain", return_value=[["no-srpm"], ["test"]]
                ):
                    with patch.object(builder, "build_package", return_value=complete_task):
                        result = builder.build_with_deps(str(srpm))

        assert "no-srpm" not in result.built_packages

    def test_dep_build_failure_adds_to_failed(self, tmp_path, mock_subprocess_run):
        """Dep submit exception should add to failed_packages."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        dep_srpm = tmp_path / "dep.src.rpm"
        dep_srpm.write_text("fake dep")
        builder = KojiBuilder()
        from vibebuild.resolver import DependencyNode

        builder.resolver._dependency_graph = {
            "test": DependencyNode(name="test", srpm_path=str(srpm)),
            "dep": DependencyNode(name="dep", srpm_path=str(dep_srpm)),
        }

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(builder.resolver, "get_build_chain", return_value=[["dep"]]):
                    with patch.object(
                        builder, "_submit_build", side_effect=Exception("build error")
                    ):
                        result = builder.build_with_deps(str(srpm))

        assert "dep" in result.failed_packages

    def test_target_build_exception(self, tmp_path, mock_subprocess_run):
        """Target build exception should mark as failed."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                    with patch.object(
                        builder, "build_package", side_effect=Exception("build fail")
                    ):
                        result = builder.build_with_deps(str(srpm))

        assert result.success is False
        assert "test" in result.failed_packages

    def test_target_build_non_complete_status(self, tmp_path, mock_subprocess_run):
        """Target build returning non-COMPLETE should mark as failed."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        builder = KojiBuilder()

        failed_task = BuildTask(
            package_name="test",
            srpm_path=str(srpm),
            target="fedora-target",
            status=BuildStatus.FAILED,
        )

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                    with patch.object(builder, "build_package", return_value=failed_task):
                        result = builder.build_with_deps(str(srpm))

        assert result.success is False
        assert "test" in result.failed_packages

    def test_dep_build_non_complete_status(self, tmp_path, mock_subprocess_run):
        """Dep build with non-COMPLETE status should mark as failed."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        dep_srpm = tmp_path / "dep.src.rpm"
        dep_srpm.write_text("fake dep")
        builder = KojiBuilder()
        from vibebuild.resolver import DependencyNode

        builder.resolver._dependency_graph = {
            "test": DependencyNode(name="test", srpm_path=str(srpm)),
            "dep": DependencyNode(name="dep", srpm_path=str(dep_srpm)),
        }

        building_task = BuildTask(
            package_name="dep",
            srpm_path=str(dep_srpm),
            target="fedora-target",
            task_id=999,
            status=BuildStatus.BUILDING,
        )

        def mock_poll(tasks, **kwargs):
            for t in tasks:
                t.status = BuildStatus.FAILED

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder.resolver, "build_dependency_graph"):
                with patch.object(builder.resolver, "get_build_chain", return_value=[["dep"]]):
                    with patch.object(builder, "_submit_build", return_value=building_task):
                        with patch.object(builder, "_poll_builds", side_effect=mock_poll):
                            result = builder.build_with_deps(str(srpm))

        assert "dep" in result.failed_packages
        assert result.success is False


class TestBuildChainEdgeCases:
    def test_build_chain_non_complete_breaks(self, tmp_path, mock_subprocess_run):
        """build_chain should break when a package status is not COMPLETE."""
        pkg1 = tmp_path / "pkg1.src.rpm"
        pkg1.write_text("fake")
        pkg2 = tmp_path / "pkg2.src.rpm"
        pkg2.write_text("fake")
        builder = KojiBuilder()

        failed_task = BuildTask(
            package_name="pkg1",
            srpm_path=str(pkg1),
            target="fedora-target",
            status=BuildStatus.FAILED,
        )

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="pkg1", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder, "build_package", return_value=failed_task):
                result = builder.build_chain([("pkg1", str(pkg1)), ("pkg2", str(pkg2))])

        assert result.success is False
        assert "pkg1" in result.failed_packages
        assert "pkg2" not in result.built_packages

    def test_build_chain_exception_breaks(self, tmp_path):
        """build_chain should break on exception."""
        pkg1 = tmp_path / "pkg1.src.rpm"
        pkg1.write_text("fake")
        pkg2 = tmp_path / "pkg2.src.rpm"
        pkg2.write_text("fake")
        builder = KojiBuilder()

        with patch.object(builder, "build_package", side_effect=Exception("boom")):
            result = builder.build_chain([("pkg1", str(pkg1)), ("pkg2", str(pkg2))])

        assert result.success is False
        assert "pkg1" in result.failed_packages


class TestGetBuildStatusEdgeCases:
    def test_get_build_status_pending_fallthrough(self, mock_subprocess_run):
        """get_build_status should return PENDING for unrecognized state."""
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Task: 12345\nState: waiting"
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.PENDING


class TestBuildPackageAddPkg:
    def test_add_pkg_called_before_build(self, tmp_path, mock_subprocess_run):
        """build_package should call add-pkg before submitting the build."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        mock_subprocess_run.return_value.stderr = ""
        builder = KojiBuilder(target="f42")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        # Check that add-pkg was called (first subprocess call)
        calls = mock_subprocess_run.call_args_list
        add_pkg_call = calls[0][0][0]
        assert "add-pkg" in add_pkg_call
        assert "f42" in add_pkg_call
        assert "test-pkg" in add_pkg_call
        assert "--owner=kojiadmin" in add_pkg_call

    def test_add_pkg_failure_logs_warning(self, tmp_path, mock_subprocess_run):
        """build_package should warn but continue if add-pkg fails."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")

        # First call (add-pkg) fails, second call (build) succeeds
        mock_subprocess_run.side_effect = [
            Mock(returncode=1, stderr="GenericError: some error", stdout=""),
            Mock(returncode=0, stderr="", stdout="Created task: 12345"),
        ]
        builder = KojiBuilder(target="f42")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=False)

        assert task.status == BuildStatus.BUILDING

    def test_add_pkg_already_exists_ignored(self, tmp_path, mock_subprocess_run):
        """build_package should silently ignore 'already exists' from add-pkg."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")

        mock_subprocess_run.side_effect = [
            Mock(returncode=1, stderr="package already exists in tag", stdout=""),
            Mock(returncode=0, stderr="", stdout="Created task: 12345"),
        ]
        builder = KojiBuilder(target="f42")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=False)

        assert task.status == BuildStatus.BUILDING


class TestBuildWithDepsEnsureRepoReady:
    def test_ensure_repo_ready_called_at_start(self, tmp_path, mock_subprocess_run):
        """build_with_deps should call _ensure_repo_ready at the start."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = KojiBuilder()

        complete_task = BuildTask(
            package_name="test",
            srpm_path=str(srpm),
            target="fedora-target",
            task_id=12345,
            status=BuildStatus.COMPLETE,
        )

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder, "_ensure_repo_ready") as mock_ensure:
                with patch.object(builder.resolver, "build_dependency_graph"):
                    with patch.object(builder.resolver, "get_build_chain", return_value=[]):
                        with patch.object(builder, "build_package", return_value=complete_task):
                            builder.build_with_deps(str(srpm))

            # Called once at the start of build_with_deps (no longer before final build)
            assert mock_ensure.call_count == 1
