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
from vibebuild.resolver import DependencyNode


def make_builder(**kwargs) -> KojiBuilder:
    """A KojiBuilder for the test hub: target fedora-target, build tag fedora-build."""
    defaults = {
        "koji_server": "https://koji.test/kojihub",
        "target": "fedora-target",
        "build_tag": "fedora-build",
    }
    return KojiBuilder(**{**defaults, **kwargs})


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
    def test_initialization(self):
        builder = make_builder(
            cert="/path/to/cert.pem",
            serverca="/path/to/ca.crt",
            target="custom-target",
            build_tag="custom-build",
            dest_tag="custom-dest",
            scratch=True,
            nowait=True,
        )

        assert builder.target == "custom-target"
        assert builder.build_tag == "custom-build"
        assert builder.dest_tag == "custom-dest"
        assert builder.hub.cert == "/path/to/cert.pem"
        assert builder.koji_client.serverca == "/path/to/ca.crt"
        assert builder.resolver.koji_tag == "custom-build"
        assert builder.scratch is True
        assert builder.nowait is True

    def test_dest_tag_defaults_to_target(self):
        assert make_builder(target="f44").dest_tag == "f44"

    def test_name_resolver_is_shared(self):
        resolver = MagicMock()
        builder = make_builder(name_resolver=resolver)

        assert builder.resolver.name_resolver is resolver
        assert builder.fetcher.name_resolver is resolver

    def test_build_package_success(self, tmp_path, mock_koji_session, sample_spec_content):
        srpm = tmp_path / "test-package-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        spec_file = tmp_path / "test-package.spec"
        spec_file.write_text(sample_spec_content)
        mock_koji_session.build.return_value = 12345
        builder = make_builder()

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
        builder = make_builder()

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
        builder = make_builder(scratch=True)

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-package", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        # session.build(srpm_path_on_hub, target, opts): the third argument contains scratch.
        build_call = mock_koji_session.build.call_args
        assert build_call.args[2] == {"scratch": True}

    def test_build_package_file_not_found(self):
        builder = make_builder()

        with pytest.raises(FileNotFoundError):
            builder.build_package("/nonexistent/path.src.rpm")

    def test_build_package_failure(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.side_effect = koji.GenericError("dependency error")
        builder = make_builder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError, match="dependency error"):
                builder.build_package(str(srpm))

    def test_wait_for_repo_success(self, mock_koji_session):
        mock_koji_session.newRepo.return_value = 555
        mock_koji_session.getTaskInfo.return_value = {"state": 2}  # closed
        builder = make_builder()

        with patch("vibebuild.builder.time.sleep"):
            result = builder.wait_for_repo()

        assert result is True
        mock_koji_session.newRepo.assert_called_once_with("fedora-build")

    def test_wait_for_repo_failure(self, mock_koji_session):
        mock_koji_session.newRepo.return_value = 555
        mock_koji_session.getTaskInfo.return_value = {"state": 5}  # failed
        builder = make_builder()

        with patch("vibebuild.builder.time.sleep"):
            result = builder.wait_for_repo()

        assert result is False


class TestSubmitBuild:
    def test_submit_build_returns_building_status(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = make_builder()

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
        builder = make_builder(cert="/p/c.pem", serverca="/p/ca.crt")

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
        builder = make_builder()

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
        builder = make_builder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            task = builder._submit_build(str(srpm))

        assert task.task_id == 42

    def test_submit_build_file_not_found(self):
        builder = make_builder()

        with pytest.raises(FileNotFoundError):
            builder._submit_build("/nonexistent/path.src.rpm")

    def test_submit_build_failure_raises(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.side_effect = koji.GenericError("Build submission failed")
        builder = make_builder()

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            with pytest.raises(KojiBuildError):
                builder._submit_build(str(srpm))


class TestSpecPatchingOptIn:
    """Specs are only patched (rpmbuild runs locally) when patch_specs is set."""

    @pytest.fixture
    def srpm(self, tmp_path):
        path = tmp_path / "test-pkg-1.0-1.src.rpm"
        path.write_text("fake srpm")
        return path

    @pytest.fixture(autouse=True)
    def package_info(self):
        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            yield mock_info

    def test_not_patched_by_default(self, srpm, mock_koji_session):
        with patch("vibebuild.builder.patch_srpm") as mock_patch:
            make_builder()._submit_build(str(srpm))

        mock_patch.assert_not_called()
        assert mock_koji_session.uploadWrapper.call_args.args[0] == str(srpm)

    def test_patched_copy_is_uploaded_and_cleaned_up(self, srpm, tmp_path, mock_koji_session):
        patched = tmp_path / "vibebuild-patch-x" / srpm.name
        with (
            patch("vibebuild.builder.patch_srpm", return_value=str(patched)) as mock_patch,
            patch("vibebuild.builder.cleanup_patched") as mock_cleanup,
        ):
            task = make_builder(patch_specs=True)._submit_build(
                str(srpm), add_unpackaged_macro=True
            )

        mock_patch.assert_called_once_with(str(srpm), add_unpackaged_macro=True)
        assert mock_koji_session.uploadWrapper.call_args.args[0] == str(patched)
        mock_cleanup.assert_called_once_with(str(patched))
        assert task.srpm_path == str(srpm)

    def test_patched_copy_is_cleaned_up_when_submit_fails(self, srpm, mock_koji_session):
        mock_koji_session.build.side_effect = koji.GenericError("boom")
        with (
            patch("vibebuild.builder.patch_srpm", return_value="/tmp/p/x.src.rpm"),
            patch("vibebuild.builder.cleanup_patched") as mock_cleanup,
        ):
            with pytest.raises(KojiBuildError):
                make_builder(patch_specs=True)._submit_build(str(srpm))

        mock_cleanup.assert_called_once_with("/tmp/p/x.src.rpm")

    def test_patch_failure_falls_back_to_original(self, srpm, mock_koji_session):
        with (
            patch("vibebuild.builder.patch_srpm", side_effect=RuntimeError("no rpmbuild")),
            patch("vibebuild.builder.cleanup_patched") as mock_cleanup,
        ):
            make_builder(patch_specs=True)._submit_build(str(srpm))

        assert mock_koji_session.uploadWrapper.call_args.args[0] == str(srpm)
        mock_cleanup.assert_not_called()


class TestPollBuilds:
    # koji task state codes: 2=closed, 3=canceled, 5=failed, 1=open.
    _CLOSED = {"state": 2}
    _FAILED = {"state": 5}
    _CANCELED = {"state": 3}
    _OPEN = {"state": 1}

    def test_poll_builds_all_complete(self, mock_koji_session):
        builder = make_builder()
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
        builder = make_builder()
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
        builder = make_builder()
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
        builder = make_builder()
        tasks = [BuildTask("pkg1", "/p1", "t", task_id=None, status=BuildStatus.BUILDING)]

        builder._poll_builds(tasks)

        assert tasks[0].status == BuildStatus.BUILDING

    def test_poll_builds_canceled(self, mock_koji_session):
        builder = make_builder()
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
        builder = make_builder()

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
        builder = make_builder()
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
        builder = make_builder()

        with pytest.raises(FileNotFoundError):
            builder.build_with_deps("/nonexistent/path.src.rpm")

    def test_build_with_deps_returns_time(self, tmp_path, mock_subprocess_run):
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = make_builder()

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


class TestBuildWithDepsEdgeCases:
    def test_srpm_resolver_callback_success(self, tmp_path, mock_subprocess_run):
        """The srpm_resolver callback in build_with_deps should download SRPMs."""
        srpm = tmp_path / "test.src.rpm"
        srpm.write_text("fake srpm")
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "Created task: 12345"
        builder = make_builder()

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
        builder = make_builder()

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
        builder = make_builder()
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
        builder = make_builder()
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
        builder = make_builder()

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
        builder = make_builder()

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
        builder = make_builder()
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


class TestBuildPackageAddPkg:
    def test_packageListAdd_called_before_build(self, tmp_path, mock_koji_session):
        """build_package must call packageListAdd before session.build."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = make_builder(target="f42")

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

    def test_packageListAdd_uses_dest_tag(self, tmp_path, mock_koji_session):
        """The package goes to the target's destination tag, the build to the target."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.build.return_value = 12345
        builder = make_builder(target="f44", dest_tag="f44-updates-candidate")

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        mock_koji_session.packageListAdd.assert_called_once_with(
            "f44-updates-candidate", "test-pkg", owner="kojiadmin"
        )
        assert mock_koji_session.build.call_args[0][1] == "f44"

    def test_packageListAdd_already_exists_ignored(self, tmp_path, mock_koji_session):
        """`already exists` from packageListAdd must be silently ignored."""
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        mock_koji_session.packageListAdd.side_effect = koji.GenericError(
            "package already exists in tag"
        )
        mock_koji_session.build.return_value = 12345
        builder = make_builder(target="f42")

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
        builder = make_builder(target="f42")

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
        builder = make_builder()

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
        builder = make_builder()

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
        builder = make_builder()

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
        builder = make_builder()

        assert builder._task_has_unpackaged_files_error(600) is False

    def test_detect_returns_false_when_log_missing(self, mock_koji_session):
        mock_koji_session.listTasks.return_value = [{"id": 701}]
        mock_koji_session.listTaskOutput.return_value = {"state.log": {"st_size": "100"}}
        builder = make_builder()

        assert builder._task_has_unpackaged_files_error(700) is False
        mock_koji_session.downloadTaskOutput.assert_not_called()

    def test_build_package_retries_with_macro_on_unpackaged_error(
        self, tmp_path, mock_koji_session
    ):
        srpm = tmp_path / "pkg.src.rpm"
        srpm.write_text("fake")
        builder = make_builder(patch_specs=True)

        first = BuildTask(
            "pkg", str(srpm), "fedora-target", task_id=11, status=BuildStatus.BUILDING
        )
        retry = BuildTask(
            "pkg", str(srpm), "fedora-target", task_id=22, status=BuildStatus.BUILDING
        )
        submit_calls: list[bool] = []

        def fake_submit(path, add_unpackaged_macro=False, scratch=False):
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
        builder = make_builder()

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
        builder = make_builder(patch_specs=True)
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

    def test_no_retry_without_patch_specs(self, tmp_path, mock_koji_session, caplog):
        caplog.set_level("INFO", logger="vibebuild.builder")
        srpm = tmp_path / "dep.src.rpm"
        srpm.write_text("fake")
        builder = make_builder()
        failed_task = BuildTask(
            "dep", str(srpm), "fedora-target", task_id=42, status=BuildStatus.FAILED
        )

        with patch.object(builder, "_task_has_unpackaged_files_error", return_value=True):
            with patch.object(builder, "_submit_build") as mock_submit:
                builder._retry_unpackaged_failures([failed_task])

        mock_submit.assert_not_called()
        assert failed_task.status == BuildStatus.FAILED
        assert "--patch-specs" in caplog.text

    def test_retry_unpackaged_failures_skips_when_marker_absent(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "dep.src.rpm"
        srpm.write_text("fake")
        builder = make_builder()
        failed_task = BuildTask(
            "dep", str(srpm), "fedora-target", task_id=42, status=BuildStatus.FAILED
        )

        with patch.object(builder, "_task_has_unpackaged_files_error", return_value=False):
            with patch.object(builder, "_submit_build") as mock_submit:
                builder._retry_unpackaged_failures([failed_task])

        mock_submit.assert_not_called()
        assert failed_task.task_id == 42
        assert failed_task.status == BuildStatus.FAILED


class TestRepoFreshness:
    def test_ensure_repo_ready_skips_newrepo_when_current(self, mock_koji_session):
        builder = make_builder()

        with patch.object(builder.hub, "repo_is_current", return_value=True):
            builder._ensure_repo_ready()

        mock_koji_session.newRepo.assert_not_called()

    def test_ensure_repo_ready_regenerates_stale_repo(self, mock_koji_session):
        builder = make_builder()

        with patch.object(builder.hub, "repo_is_current", return_value=False):
            builder._ensure_repo_ready()

        mock_koji_session.newRepo.assert_called_once_with("fedora-build")


class TestRepoRegenerationBetweenLevels:
    def _run(self, tmp_path, chain, built):
        """Run build_with_deps over `chain`; packages in `built` complete, the rest fail."""
        srpm = tmp_path / "main.src.rpm"
        srpm.write_text("fake srpm")
        builder = make_builder()
        builder.resolver._dependency_graph = {
            name: DependencyNode(name=name, srpm_path=str(srpm))
            for level in chain
            for name in level
        }

        def submit(path, **kwargs):
            name = next(n for lvl in chain for n in lvl if n not in submitted)
            submitted.append(name)
            return BuildTask(package_name=name, srpm_path=path, target="t", task_id=1)

        def poll(tasks, **kwargs):
            for t in tasks:
                t.status = BuildStatus.COMPLETE if t.package_name in built else BuildStatus.FAILED

        submitted: list[str] = []
        target = BuildTask(
            package_name="main", srpm_path=str(srpm), target="t", status=BuildStatus.COMPLETE
        )
        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="main", version="1", release="1", build_requires=[], source_urls=[]
            )
            with (
                patch.object(builder.resolver, "build_dependency_graph"),
                patch.object(builder.resolver, "get_build_chain", return_value=chain),
                patch.object(builder, "_ensure_repo_ready"),
                patch.object(builder, "_submit_build", side_effect=submit),
                patch.object(builder, "_poll_builds", side_effect=poll),
                patch.object(builder, "_retry_unpackaged_failures"),
                patch.object(builder, "build_package", return_value=target),
                patch.object(builder, "wait_for_repo") as wait,
            ):
                builder.build_with_deps(str(srpm))
        return wait.call_count

    def test_one_level_one_newrepo(self, tmp_path):
        assert self._run(tmp_path, [["dep"]], built={"dep"}) == 1

    def test_two_levels_newrepo_between_and_before_target(self, tmp_path):
        assert self._run(tmp_path, [["a"], ["b"]], built={"a", "b"}) == 2

    def test_nothing_built_no_newrepo(self, tmp_path):
        assert self._run(tmp_path, [["dep"]], built=set()) == 0

    def test_no_dependencies_no_newrepo(self, tmp_path):
        assert self._run(tmp_path, [], built=set()) == 0


class TestScratchOnlyForTarget:
    def test_target_scratch_option(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "test-pkg-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        builder = make_builder(target="f44", scratch=True)

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="test-pkg", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder.build_package(str(srpm), wait=False)

        assert mock_koji_session.build.call_args[0][2] == {"scratch": True}

    def test_dependency_is_regular_build(self, tmp_path, mock_koji_session):
        srpm = tmp_path / "dep-1.0-1.src.rpm"
        srpm.write_text("fake srpm")
        builder = make_builder(target="f44", scratch=True)

        with patch("vibebuild.builder.get_package_info_from_srpm") as mock_info:
            mock_info.return_value = PackageInfo(
                name="dep", version="1.0", release="1", build_requires=[], source_urls=[]
            )
            builder._submit_build(str(srpm))

        assert mock_koji_session.build.call_args[0][2] == {}

    def test_scratch_target_not_reported_missing_from_tag(self, caplog):
        builder = make_builder(scratch=True)
        result = BuildResult(success=True, built_packages=["main"])

        with patch.object(builder.koji_client, "list_tagged_builds", return_value={}):
            builder._populate_tagged_builds(result, "main")

        assert "not found in tag" not in caplog.text
        assert result.tagged_target is None


def _info(name="test-pkg"):
    return PackageInfo(name=name, version="1.0", release="1", build_requires=[], source_urls=[])


class TestAlreadyBuilt:
    @pytest.fixture
    def srpm(self, tmp_path):
        path = tmp_path / "test-pkg-1.0-1.src.rpm"
        path.write_text("fake srpm")
        return path

    def test_nvr_latest_in_dest_tag_is_skipped(self, srpm, mock_koji_session):
        builder = make_builder(dest_tag="f44-updates")
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()),
            patch.object(
                builder.koji_client, "latest_build", return_value={"nvr": "test-pkg-1.0-1"}
            ) as latest,
        ):
            task = builder._submit_build(str(srpm))

        latest.assert_called_once_with("test-pkg", "f44-updates")
        assert task.status == BuildStatus.ALREADY_BUILT
        mock_koji_session.build.assert_not_called()

    def test_older_nvr_is_built(self, srpm, mock_koji_session):
        builder = make_builder()
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()),
            patch.object(
                builder.koji_client, "latest_build", return_value={"nvr": "test-pkg-0.9-1"}
            ),
        ):
            task = builder._submit_build(str(srpm))

        assert task.status == BuildStatus.BUILDING

    def test_scratch_is_not_checked(self, srpm, mock_koji_session):
        builder = make_builder()
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()),
            patch.object(builder.koji_client, "latest_build") as latest,
        ):
            builder._submit_build(str(srpm), scratch=True)

        latest.assert_not_called()

    def test_check_error_does_not_block_the_build(self, srpm, mock_koji_session):
        builder = make_builder()
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()),
            patch.object(builder.koji_client, "latest_build", side_effect=OSError("down")),
        ):
            task = builder._submit_build(str(srpm))

        assert task.status == BuildStatus.BUILDING

    def test_hub_refusing_existing_build(self, srpm, mock_koji_session):
        mock_koji_session.build.side_effect = koji.GenericError(
            "Build already exists (id=7, state=COMPLETE): {}"
        )
        with patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()):
            task = make_builder().build_package(str(srpm))

        assert task.status == BuildStatus.ALREADY_BUILT
        assert task.task_id is None

    def test_already_built_target_is_success(self, srpm):
        builder = make_builder()
        done = BuildTask("test-pkg", str(srpm), "t", status=BuildStatus.ALREADY_BUILT)
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()),
            patch.object(builder, "_ensure_repo_ready"),
            patch.object(builder, "resolve_build_chain", return_value=[]),
            patch.object(builder, "build_package", return_value=done),
        ):
            result = builder.build_with_deps(str(srpm))

        assert result.success is True
        assert result.built_packages == ["test-pkg"]

    def test_already_built_dependency_does_not_regenerate_repo(self, srpm):
        builder = make_builder()
        builder.resolver._dependency_graph = {"dep": DependencyNode("dep", srpm_path=str(srpm))}
        dep = BuildTask("dep", str(srpm), "t", status=BuildStatus.ALREADY_BUILT)
        target = BuildTask("test-pkg", str(srpm), "t", status=BuildStatus.COMPLETE)
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info()),
            patch.object(builder, "_ensure_repo_ready"),
            patch.object(builder, "resolve_build_chain", return_value=[["dep"]]),
            patch.object(builder, "_submit_build", return_value=dep),
            patch.object(builder, "build_package", return_value=target),
            patch.object(builder, "wait_for_repo") as wait,
        ):
            result = builder.build_with_deps(str(srpm))

        assert result.success is True
        assert result.built_packages == ["dep", "test-pkg"]
        wait.assert_not_called()


class TestBuildWithDepsFlow:
    @pytest.fixture
    def srpm(self, tmp_path):
        path = tmp_path / "main.src.rpm"
        path.write_text("fake srpm")
        return path

    def test_without_deps_skips_resolution_and_repo(self, srpm):
        builder = make_builder()
        target = BuildTask("main", str(srpm), "t", task_id=1, status=BuildStatus.COMPLETE)
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info("main")),
            patch.object(builder, "_ensure_repo_ready") as ensure,
            patch.object(builder, "resolve_build_chain") as resolve,
            patch.object(builder, "build_package", return_value=target),
        ):
            result = builder.build_with_deps(str(srpm), resolve_deps=False)

        ensure.assert_not_called()
        resolve.assert_not_called()
        assert result.success is True
        assert result.built_packages == ["main"]

    def test_nowait_target_counts_as_built(self, srpm):
        builder = make_builder(nowait=True)
        target = BuildTask("main", str(srpm), "t", task_id=1, status=BuildStatus.BUILDING)
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info("main")),
            patch.object(builder, "build_package", return_value=target),
        ):
            result = builder.build_with_deps(str(srpm), resolve_deps=False)

        assert result.success is True

    def test_dependency_submit_error_stops_the_target(self, srpm):
        builder = make_builder()
        builder.resolver._dependency_graph = {"dep": DependencyNode("dep", srpm_path=str(srpm))}
        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info("main")),
            patch.object(builder, "_ensure_repo_ready"),
            patch.object(builder, "resolve_build_chain", return_value=[["dep"]]),
            patch.object(builder, "_submit_build", side_effect=KojiBuildError("denied")),
            patch.object(builder, "build_package") as build_target,
        ):
            result = builder.build_with_deps(str(srpm))

        assert result.success is False
        assert result.failed_packages == ["dep"]
        build_target.assert_not_called()

    def test_failed_level_does_not_stop_later_levels(self, srpm):
        builder = make_builder()
        builder.resolver._dependency_graph = {
            name: DependencyNode(name, srpm_path=str(srpm)) for name in ("a", "b")
        }
        submitted = []

        def submit(path, **kwargs):
            name = "ab"[len(submitted)]
            submitted.append(name)
            return BuildTask(name, path, "t", task_id=len(submitted))

        def poll(tasks, **kwargs):
            for t in tasks:
                t.status = BuildStatus.FAILED if t.package_name == "a" else BuildStatus.COMPLETE

        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info("main")),
            patch.object(builder, "_ensure_repo_ready"),
            patch.object(builder, "resolve_build_chain", return_value=[["a"], ["b"]]),
            patch.object(builder, "_submit_build", side_effect=submit),
            patch.object(builder, "_poll_builds", side_effect=poll),
            patch.object(builder, "_retry_unpackaged_failures"),
            patch.object(builder, "wait_for_repo"),
            patch.object(builder, "build_package") as build_target,
        ):
            result = builder.build_with_deps(str(srpm))

        assert submitted == ["a", "b"]
        assert result.failed_packages == ["a"]
        assert result.built_packages == ["b"]
        build_target.assert_not_called()

    def test_session_is_opened_once_for_the_run(self, srpm, mock_koji_session):
        builder = make_builder()
        target = BuildTask("main", str(srpm), "t", task_id=1, status=BuildStatus.COMPLETE)

        def build_package(path, wait=True):
            with builder.hub:
                builder.hub.task_state(1)
            return target

        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info("main")),
            patch.object(builder, "build_package", side_effect=build_package),
        ):
            builder.build_with_deps(str(srpm), resolve_deps=False)

        mock_koji_session.ssl_login.assert_called_once()
        mock_koji_session.logout.assert_called_once()

    def test_resolve_build_chain_downloads_each_dependency_once(self, srpm):
        builder = make_builder()
        calls = []

        def graph(name, path, srpm_resolver):
            calls.append(srpm_resolver("dep"))
            calls.append(srpm_resolver("dep"))

        with (
            patch("vibebuild.builder.get_package_info_from_srpm", return_value=_info("main")),
            patch.object(builder.resolver, "build_dependency_graph", side_effect=graph),
            patch.object(builder.resolver, "get_build_chain", return_value=[["dep"]]),
            patch.object(builder.fetcher, "download_srpm", return_value="/d/dep.src.rpm") as dl,
        ):
            assert builder.resolve_build_chain(str(srpm)) == [["dep"]]

        assert calls == ["/d/dep.src.rpm", None]
        dl.assert_called_once_with("dep")
