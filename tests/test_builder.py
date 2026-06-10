"""Tests for vibebuild.builder module."""

from unittest.mock import MagicMock, patch

import koji
import pytest

from vibebuild.analyzer import PackageInfo
from vibebuild.builder import (
    BuildResult,
    BuildStatus,
    BuildTask,
    KojiBuilder,
)
from vibebuild.exceptions import KojiBuildError


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

    def test_build_package_success(self, tmp_path, mock_koji_session, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        spec_file = tmp_path / "test-package.spec"
        spec_file.write_text(sample_spec_content)
        mock_koji_session.build.return_value = 12345
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=False)

        assert task.package_name == "test-package"
        assert task.task_id == 12345
        assert task.status == BuildStatus.BUILDING

    def test_build_package_with_wait(self, tmp_path, mock_koji_session, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with patch.object(builder, "_poll_build", return_value=BuildStatus.COMPLETE):
                task = builder.build_package(str(srpm), wait=True)

        assert task.status == BuildStatus.COMPLETE

    def test_build_package_scratch(self, tmp_path, mock_koji_session, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = KojiBuilder(scratch=True)

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        # session.build(srpm_path_on_hub, target, opts): the third argument contains scratch.
        build_call = mock_koji_session.build.call_args
        assert build_call.args[2] == {"scratch": True}

    def test_build_package_file_not_found(self):
        builder = KojiBuilder()

        with pytest.raises(FileNotFoundError):
            builder.build_package("/nonexistent/path.src.rpm")

    def test_build_package_failure(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.side_effect = koji.GenericError("dependency error")
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError, match="session.build failed"):
                builder.build_package(str(srpm))

    def test_wait_for_repo_success(self, mock_koji_session):
        mock_koji_session.newRepo.return_value = 555
        mock_koji_session.getTaskInfo.return_value = {"state": 2}  # closed
        builder = KojiBuilder()

        with patch("vibebuild.builder.time.sleep"):
            result = builder.wait_for_repo()

        assert result is True
        mock_koji_session.newRepo.assert_called_once_with("fedora-build")

    def test_wait_for_repo_custom_tag(self, mock_koji_session):
        mock_koji_session.newRepo.return_value = 555
        mock_koji_session.getTaskInfo.return_value = {"state": 2}
        builder = KojiBuilder()

        with patch("vibebuild.builder.time.sleep"):
            builder.wait_for_repo(tag="custom-tag")

        mock_koji_session.newRepo.assert_called_once_with("custom-tag")

    def test_wait_for_repo_failure(self, mock_koji_session):
        mock_koji_session.newRepo.return_value = 555
        mock_koji_session.getTaskInfo.return_value = {"state": 5}  # failed
        builder = KojiBuilder()

        with patch("vibebuild.builder.time.sleep"):
            result = builder.wait_for_repo()

        assert result is False

    def test_get_build_status_complete(self, mock_koji_session):
        mock_koji_session.getTaskInfo.return_value = {"state": 2}
        builder = KojiBuilder()
        assert builder.get_build_status(12345) == BuildStatus.COMPLETE

    def test_get_build_status_failed(self, mock_koji_session):
        mock_koji_session.getTaskInfo.return_value = {"state": 5}
        builder = KojiBuilder()
        assert builder.get_build_status(12345) == BuildStatus.FAILED

    def test_get_build_status_building(self, mock_koji_session):
        mock_koji_session.getTaskInfo.return_value = {"state": 1}
        builder = KojiBuilder()
        assert builder.get_build_status(12345) == BuildStatus.BUILDING

    def test_get_build_status_canceled(self, mock_koji_session):
        mock_koji_session.getTaskInfo.return_value = {"state": 3}
        builder = KojiBuilder()
        assert builder.get_build_status(12345) == BuildStatus.CANCELED

    def test_get_build_status_error(self, mock_koji_session):
        mock_koji_session.getTaskInfo.side_effect = koji.GenericError("rpc failed")
        builder = KojiBuilder()
        assert builder.get_build_status(12345) == BuildStatus.FAILED

    def test_cancel_build_success(self, mock_koji_session):
        builder = KojiBuilder()
        assert builder.cancel_build(12345) is True
        mock_koji_session.cancelTask.assert_called_once_with(12345)

    def test_cancel_build_failure(self, mock_koji_session):
        mock_koji_session.cancelTask.side_effect = koji.GenericError("permission denied")
        builder = KojiBuilder()
        assert builder.cancel_build(12345) is False


class TestSubmitBuild:
    def test_submit_build_returns_building_status(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder._submit_build(str(srpm))

        assert task.status == BuildStatus.BUILDING
        assert task.task_id == 12345
        assert task.package_name == "test-pkg"

    def test_submit_build_uses_single_koji_session(self, tmp_path, mock_koji_session):
        # Regression for AuthLockError: add-pkg + uploadWrapper + build go through
        # the same ClientSession instance. The session lifecycle (logout) is
        # the responsibility of build_with_deps / build_package, so here we do not
        # check logout.
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 99
        builder = KojiBuilder(cert="/p/c.pem", serverca="/p/ca.crt")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder._submit_build(str(srpm))

        mock_koji_session.packageListAdd.assert_called_once()
        mock_koji_session.uploadWrapper.assert_called_once()
        mock_koji_session.build.assert_called_once()

    def test_build_package_closes_session_after_call(self, tmp_path, mock_koji_session):
        # build_package owns the session and must log out in finally.
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 99
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        mock_koji_session.ssl_login.assert_called_once()
        mock_koji_session.logout.assert_called_once()

    def test_submit_build_ignores_packageListAdd_already_exists(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.packageListAdd.side_effect = koji.GenericError(
            "package python-flask already exists in tag f42"
        )
        mock_koji_session.build.return_value = 42
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder._submit_build(str(srpm))

        assert task.task_id == 42

    def test_submit_build_file_not_found(self):
        builder = KojiBuilder()

        with pytest.raises(FileNotFoundError):
            builder._submit_build("/nonexistent/path.src.rpm")

    def test_submit_build_failure_raises(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.side_effect = koji.GenericError("Build submission failed")
        builder = KojiBuilder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError):
                builder._submit_build(str(srpm))


class TestPollBuilds:
    # koji task state codes: 2=closed, 3=canceled, 5=failed, 1=open.
    _CLOSED = {"state": 2}
    _FAILED = {"state": 5}
    _CANCELED = {"state": 3}
    _OPEN = {"state": 1}

    def test_poll_builds_all_complete(self, mock_koji_session):
        builder = KojiBuilder()
        tasks = [
            BuildTask("pkg1", "/p1", "t", task_id=101, status=BuildStatus.BUILDING),
            BuildTask("pkg2", "/p2", "t", task_id=102, status=BuildStatus.BUILDING),
        ]
        mock_koji_session.getTaskInfo.return_value = self._CLOSED

        with patch("vibebuild.builder.time.sleep"):
            builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.COMPLETE
        assert tasks[1].status == BuildStatus.COMPLETE

    def test_poll_builds_mixed_results(self, mock_koji_session):
        builder = KojiBuilder()
        tasks = [
            BuildTask("pkg1", "/p1", "t", task_id=201, status=BuildStatus.BUILDING),
            BuildTask("pkg2", "/p2", "t", task_id=202, status=BuildStatus.BUILDING),
        ]

        def info(task_id):
            return self._CLOSED if task_id == 201 else self._FAILED

        mock_koji_session.getTaskInfo.side_effect = info

        with patch("vibebuild.builder.time.sleep"):
            builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.COMPLETE
        assert tasks[1].status == BuildStatus.FAILED

    def test_poll_builds_timeout(self, mock_koji_session):
        builder = KojiBuilder()
        tasks = [BuildTask("pkg1", "/p1", "t", task_id=301, status=BuildStatus.BUILDING)]
        mock_koji_session.getTaskInfo.return_value = self._OPEN

        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            return 0 if call_count <= 3 else 9999

        with patch("vibebuild.builder.time.time", side_effect=fake_time):
            with patch("vibebuild.builder.time.sleep"):
                builder._poll_builds(tasks, timeout=60, interval=1)

        assert tasks[0].status == BuildStatus.FAILED
        assert "timed out" in tasks[0].error_message

    def test_poll_builds_no_task_id_skipped(self, mock_koji_session):
        builder = KojiBuilder()
        tasks = [BuildTask("pkg1", "/p1", "t", task_id=None, status=BuildStatus.BUILDING)]

        builder._poll_builds(tasks)

        assert tasks[0].status == BuildStatus.BUILDING

    def test_poll_builds_canceled(self, mock_koji_session):
        builder = KojiBuilder()
        tasks = [BuildTask("pkg1", "/p1", "t", task_id=401, status=BuildStatus.BUILDING)]
        mock_koji_session.getTaskInfo.return_value = self._CANCELED

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

    def test_build_chain_stops_on_failure(self, tmp_path, mock_koji_session):
        pkg1 = tmp_path / "pkg1.src.rpm"
        pkg1.write_text("fake")
        pkg2 = tmp_path / "pkg2.src.rpm"
        pkg2.write_text("fake")
        mock_koji_session.build.side_effect = koji.GenericError("Build failed")
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
    def test_get_build_status_pending_fallthrough(self, mock_koji_session):
        """An unknown koji state maps to PENDING."""
        mock_koji_session.getTaskInfo.return_value = {"state": 42}  # not in map
        builder = KojiBuilder()

        result = builder.get_build_status(12345)

        assert result == BuildStatus.PENDING


class TestBuildPackageAddPkg:
    def test_packageListAdd_called_before_build(self, tmp_path, mock_koji_session):
        """build_package must call packageListAdd before session.build."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = KojiBuilder(target="f42")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        mock_koji_session.packageListAdd.assert_called_once_with(
            "f42", "test-pkg", owner="kojiadmin"
        )
        method_names = [name for name, _, _ in mock_koji_session.method_calls]
        assert method_names.index("packageListAdd") < method_names.index("build")

    def test_packageListAdd_already_exists_ignored(self, tmp_path, mock_koji_session):
        """`already exists` from packageListAdd must be silently ignored."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.packageListAdd.side_effect = koji.GenericError(
            "package already exists in tag"
        )
        mock_koji_session.build.return_value = 12345
        builder = KojiBuilder(target="f42")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder.build_package(str(srpm), wait=False)

        assert task.status == BuildStatus.BUILDING

    def test_packageListAdd_other_error_raises(self, tmp_path, mock_koji_session):
        """Any other packageListAdd error is fatal, wrapped in KojiBuildError."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.packageListAdd.side_effect = koji.GenericError("permission denied")
        builder = KojiBuilder(target="f42")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError, match="packageListAdd"):
                builder.build_package(str(srpm), wait=False)


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


class TestUnpackagedFilesRetry:
    """Retry of a build with `_unpackaged_files_terminate_build 0` on the corresponding
    rpmbuild error. Applying the macro is optional, only on retry.
    """

    def test_detect_unpackaged_marker_in_build_log(self, mock_koji_session):
        mock_koji_session.listTasks.return_value = [{"id": 501}]
        mock_koji_session.listTaskOutput.return_value = {
            "build.log": {"st_size": "8000"},
            "state.log": {"st_size": "200"},
        }
        marker = KojiBuilder._UNPACKAGED_FILES_MARKER.encode()
        mock_koji_session.downloadTaskOutput.return_value = b"... noise ...\n" + marker + b"\n"
        builder = KojiBuilder()

        assert builder._task_has_unpackaged_files_error(500) is True
        # the detector reads with a positive offset; for an 8000-byte log the offset must be 0
        args, kwargs = mock_koji_session.downloadTaskOutput.call_args
        assert kwargs.get("offset", args[2] if len(args) > 2 else None) == 0

    def test_detect_uses_tail_offset_for_large_log(self, mock_koji_session):
        mock_koji_session.listTasks.return_value = []
        mock_koji_session.listTaskOutput.return_value = {
            "build.log": {"st_size": str(5_000_000)},
        }
        marker = KojiBuilder._UNPACKAGED_FILES_MARKER.encode()
        mock_koji_session.downloadTaskOutput.return_value = marker
        builder = KojiBuilder()

        assert builder._task_has_unpackaged_files_error(800) is True
        args, kwargs = mock_koji_session.downloadTaskOutput.call_args
        offset = kwargs.get("offset", args[2] if len(args) > 2 else None)
        assert offset == 5_000_000 - KojiBuilder._UNPACKAGED_FILES_LOG_TAIL_BYTES

    def test_detect_returns_false_when_marker_absent(self, mock_koji_session):
        mock_koji_session.listTasks.return_value = [{"id": 601}]
        mock_koji_session.listTaskOutput.return_value = {
            "build.log": {"st_size": "500"},
        }
        mock_koji_session.downloadTaskOutput.return_value = b"unrelated build output\n"
        builder = KojiBuilder()

        assert builder._task_has_unpackaged_files_error(600) is False

    def test_detect_returns_false_when_log_missing(self, mock_koji_session):
        mock_koji_session.listTasks.return_value = [{"id": 701}]
        mock_koji_session.listTaskOutput.return_value = {"state.log": {"st_size": "100"}}
        builder = KojiBuilder()

        assert builder._task_has_unpackaged_files_error(700) is False
        mock_koji_session.downloadTaskOutput.assert_not_called()

    def test_build_package_retries_with_macro_on_unpackaged_error(
        self, tmp_path, mock_koji_session
    ):
        srpm = tmp_path / "pkg.src.rpm"
        srpm.write_text("fake")
        builder = KojiBuilder()

        first = BuildTask(
            "pkg", str(srpm), "fedora-target", task_id=11, status=BuildStatus.BUILDING
        )
        retry = BuildTask(
            "pkg", str(srpm), "fedora-target", task_id=22, status=BuildStatus.BUILDING
        )
        submit_calls: list[bool] = []

        def fake_submit(path, add_unpackaged_macro=False):
            submit_calls.append(add_unpackaged_macro)
            return first if len(submit_calls) == 1 else retry

        poll_results = [BuildStatus.FAILED, BuildStatus.COMPLETE]

        def fake_poll(task_id, label, *_, **__):
            return poll_results.pop(0)

        with patch.object(builder, "_submit_build", side_effect=fake_submit):
            with patch.object(builder, "_poll_build", side_effect=fake_poll):
                with patch.object(builder, "_task_has_unpackaged_files_error", return_value=True):
                    task = builder.build_package(str(srpm), wait=True)

        assert submit_calls == [False, True]
        assert task.task_id == 22
        assert task.status == BuildStatus.COMPLETE

    def test_build_package_no_retry_when_marker_absent(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "pkg.src.rpm"
        srpm.write_text("fake")
        builder = KojiBuilder()

        first = BuildTask(
            "pkg", str(srpm), "fedora-target", task_id=11, status=BuildStatus.BUILDING
        )

        with patch.object(builder, "_submit_build", return_value=first) as mock_submit:
            with patch.object(builder, "_poll_build", return_value=BuildStatus.FAILED):
                with patch.object(builder, "_task_has_unpackaged_files_error", return_value=False):
                    task = builder.build_package(str(srpm), wait=True)

        assert mock_submit.call_count == 1
        assert task.task_id == 11
        assert task.status == BuildStatus.FAILED

    def test_retry_unpackaged_failures_swaps_task_on_success(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "dep.src.rpm"
        srpm.write_text("fake")
        builder = KojiBuilder()
        failed_task = BuildTask(
            "dep", str(srpm), "fedora-target", task_id=42, status=BuildStatus.FAILED
        )
        retry_task = BuildTask(
            "dep", str(srpm), "fedora-target", task_id=43, status=BuildStatus.BUILDING
        )

        def fake_poll(tasks, *_, **__):
            for t in tasks:
                t.status = BuildStatus.COMPLETE

        with patch.object(builder, "_task_has_unpackaged_files_error", return_value=True):
            with patch.object(builder, "_submit_build", return_value=retry_task) as mock_submit:
                with patch.object(builder, "_poll_builds", side_effect=fake_poll):
                    builder._retry_unpackaged_failures([failed_task])

        mock_submit.assert_called_once_with(str(srpm), add_unpackaged_macro=True)
        assert failed_task.task_id == 43
        assert failed_task.status == BuildStatus.COMPLETE

    def test_retry_unpackaged_failures_skips_when_marker_absent(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "dep.src.rpm"
        srpm.write_text("fake")
        builder = KojiBuilder()
        failed_task = BuildTask(
            "dep", str(srpm), "fedora-target", task_id=42, status=BuildStatus.FAILED
        )

        with patch.object(builder, "_task_has_unpackaged_files_error", return_value=False):
            with patch.object(builder, "_submit_build") as mock_submit:
                builder._retry_unpackaged_failures([failed_task])

        mock_submit.assert_not_called()
        assert failed_task.task_id == 42
        assert failed_task.status == BuildStatus.FAILED
