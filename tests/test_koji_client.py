"""Tests for vibebuild.koji_client module."""

from pathlib import Path
from unittest.mock import MagicMock

import koji
import pytest

from vibebuild.exceptions import KojiBuildError, KojiConnectionError
from vibebuild.koji_client import KojiClient, KojiSession, poll_interval, task_state_name

HUB = "https://hub/kojihub"


def _build(name, version, release):
    return {
        "package_name": name,
        "nvr": f"{name}-{version}-{release}",
        "version": version,
        "release": release,
    }


@pytest.fixture
def hub(mocker):
    """The koji.ClientSession behind KojiClient."""
    session = MagicMock()
    mocker.patch("vibebuild.koji_client.koji.ClientSession", return_value=session)
    return session


class TestKojiClient:
    def test_session_verifies_with_serverca(self, mocker):
        cls = mocker.patch("vibebuild.koji_client.koji.ClientSession")
        _ = KojiClient(server="https://hub/kojihub", serverca="/ca.crt").session

        assert cls.call_args.args[0] == "https://hub/kojihub"
        assert cls.call_args.kwargs["opts"]["serverca"] == "/ca.crt"
        assert "no_ssl_verify" not in cls.call_args.kwargs["opts"]

    def test_session_no_ssl_verify(self, mocker):
        cls = mocker.patch("vibebuild.koji_client.koji.ClientSession")
        _ = KojiClient(HUB, serverca="/ca.crt", no_ssl_verify=True).session

        assert cls.call_args.kwargs["opts"]["no_ssl_verify"] is True
        assert "serverca" not in cls.call_args.kwargs["opts"]

    def test_session_created_once(self, mocker):
        cls = mocker.patch("vibebuild.koji_client.koji.ClientSession")
        client = KojiClient(HUB)

        assert client.session is client.session
        cls.assert_called_once()

    def test_list_packages(self, hub):
        hub.listPackages.return_value = [{"package_name": "gcc"}, {"package_name": "make"}]

        assert KojiClient(HUB).list_packages("f44") == ["gcc", "make"]
        hub.listPackages.assert_called_once_with(tagID="f44", inherited=True, with_blocked=False)

    def test_list_tagged_builds(self, hub):
        hub.listTagged.return_value = [_build("gcc", "15.1", "1.fc44")]

        assert KojiClient(HUB).list_tagged_builds("f44") == {"gcc": "gcc-15.1-1.fc44"}
        hub.listTagged.assert_called_once_with("f44", latest=True)

    def test_transport_error_becomes_connection_error(self, hub):
        hub.listPackages.side_effect = OSError("Connection refused")

        with pytest.raises(KojiConnectionError, match="listPackages"):
            KojiClient(HUB).list_packages("f44")

    def test_latest_build(self, hub):
        hub.getLatestBuilds.return_value = [_build("gcc", "15.1", "1.fc44")]

        assert KojiClient(HUB).latest_build("gcc", "f44") == {
            "nvr": "gcc-15.1-1.fc44",
            "version": "15.1",
            "release": "1.fc44",
        }
        hub.getLatestBuilds.assert_called_once_with("f44", package="gcc")

    def test_latest_build_none(self, hub):
        hub.getLatestBuilds.return_value = []

        assert KojiClient(HUB).latest_build("gcc", "f44") is None

    def test_latest_build_unknown_package(self, hub):
        hub.getLatestBuilds.side_effect = koji.GenericError("No such entry in table package")

        assert KojiClient(HUB).latest_build("nope", "f44") is None

    def test_latest_build_cached(self, hub):
        hub.getLatestBuilds.return_value = []
        client = KojiClient(HUB)

        client.latest_build("gcc", "f44")
        client.latest_build("gcc", "f44")
        assert hub.getLatestBuilds.call_count == 1

        client.clear_cache()
        client.latest_build("gcc", "f44")
        assert hub.getLatestBuilds.call_count == 2

    def test_package_exists(self, hub):
        hub.getLatestBuilds.side_effect = lambda tag, package: (
            [_build("gcc", "15.1", "1.fc44")] if package == "gcc" else []
        )
        client = KojiClient(HUB)

        assert client.package_exists("gcc", "f44") is True
        assert client.package_exists("missing", "f44") is False

    def test_prefetch_latest_builds(self, hub):
        found = MagicMock(result=[_build("gcc", "15.1", "1.fc44")])
        unknown = MagicMock()
        type(unknown).result = property(lambda self: (_ for _ in ()).throw(koji.GenericError()))
        multicall = hub.multicall.return_value.__enter__.return_value
        multicall.getLatestBuilds.side_effect = lambda tag, package: (
            found if package == "gcc" else unknown
        )
        client = KojiClient(HUB)

        client.prefetch_latest_builds(["gcc", "nope", "gcc"], "f44")

        assert multicall.getLatestBuilds.call_count == 2
        assert client.latest_build("gcc", "f44")["nvr"] == "gcc-15.1-1.fc44"
        assert client.latest_build("nope", "f44") is None
        hub.getLatestBuilds.assert_not_called()

    def test_prefetch_skips_cached(self, hub):
        hub.getLatestBuilds.return_value = []
        client = KojiClient(HUB)
        client.latest_build("gcc", "f44")

        client.prefetch_latest_builds(["gcc"], "f44")

        hub.multicall.assert_not_called()

    def test_prefetch_failure_falls_back(self, hub):
        hub.multicall.side_effect = OSError("Connection refused")
        hub.getLatestBuilds.return_value = []
        client = KojiClient(HUB)

        client.prefetch_latest_builds(["gcc"], "f44")

        assert client.latest_build("gcc", "f44") is None
        hub.getLatestBuilds.assert_called_once()

    def test_get_target(self, hub):
        hub.getBuildTarget.return_value = {
            "build_tag_name": "f44-build",
            "dest_tag_name": "f44-updates-candidate",
        }

        assert KojiClient(HUB).get_target("f44") == {
            "build_tag": "f44-build",
            "dest_tag": "f44-updates-candidate",
        }

    def test_get_target_missing(self, hub):
        hub.getBuildTarget.return_value = None

        assert KojiClient(HUB).get_target("nope") is None

    def test_has_external_repos(self, hub):
        hub.getExternalRepoList.return_value = [{"external_repo_name": "fedora-44"}]

        assert KojiClient(HUB).has_external_repos("f44-build") is True

    def test_has_no_external_repos(self, hub):
        hub.getExternalRepoList.return_value = []

        assert KojiClient(HUB).has_external_repos("f44-build") is False


class TestHelpers:
    @pytest.mark.parametrize(
        ("elapsed", "expected"), [(0, 2.0), (29, 2.0), (30, 10.0), (179, 10.0), (180, 30.0)]
    )
    def test_poll_interval(self, elapsed, expected):
        assert poll_interval(elapsed) == expected

    def test_poll_interval_capped(self):
        assert poll_interval(0, max_interval=1) == 1.0
        assert poll_interval(500, max_interval=5) == 5.0

    def test_task_state_name(self):
        assert task_state_name(0) == "free"
        assert task_state_name(2) == "closed"
        assert task_state_name(None) == "unknown"
        assert task_state_name(42) == "unknown"


@pytest.fixture
def hub_session(mock_koji_session):
    """KojiSession over the conftest ClientSession stub."""
    return KojiSession(HUB, cert="/c.pem", serverca="/ca.crt")


class TestKojiSession:
    def test_logs_in_once_on_first_use(self, hub_session, mock_koji_session, mocker):
        cls = mocker.patch(
            "vibebuild.koji_client.koji.ClientSession", return_value=mock_koji_session
        )

        hub_session.task_state(1)
        hub_session.task_state(2)

        cls.assert_called_once_with(HUB, opts={})
        mock_koji_session.ssl_login.assert_called_once_with("/c.pem", "/ca.crt", "/ca.crt")

    def test_no_ssl_verify(self, mock_koji_session, mocker):
        cls = mocker.patch(
            "vibebuild.koji_client.koji.ClientSession", return_value=mock_koji_session
        )

        KojiSession(HUB, no_ssl_verify=True).task_state(1)

        assert cls.call_args.kwargs["opts"] == {"no_ssl_verify": True}

    def test_context_manager_logs_out_at_outermost_exit(self, hub_session, mock_koji_session):
        with hub_session:
            with hub_session:
                hub_session.task_state(1)
            mock_koji_session.logout.assert_not_called()
        mock_koji_session.logout.assert_called_once()

    def test_logout_without_login_is_noop(self, hub_session, mock_koji_session):
        hub_session.logout()

        mock_koji_session.logout.assert_not_called()

    def test_logout_error_is_ignored(self, hub_session, mock_koji_session):
        mock_koji_session.logout.side_effect = OSError("gone")
        hub_session.task_state(1)

        hub_session.logout()
        hub_session.task_state(1)

        assert mock_koji_session.ssl_login.call_count == 2


class TestSubmit:
    def test_registers_uploads_and_builds(self, hub_session, mock_koji_session):
        task_id = hub_session.submit(Path("/x/foo-1-1.src.rpm"), "foo", "f44", "f44-updates")

        assert task_id == 12345
        mock_koji_session.packageListAdd.assert_called_once_with(
            "f44-updates", "foo", owner="kojiadmin"
        )
        upload_path, server_dir = mock_koji_session.uploadWrapper.call_args.args
        assert upload_path == "/x/foo-1-1.src.rpm"
        assert server_dir.startswith("cli-build/vibebuild-")
        mock_koji_session.build.assert_called_once_with(f"{server_dir}/foo-1-1.src.rpm", "f44", {})

    def test_owner_is_the_logged_in_user(self, hub_session, mock_koji_session):
        mock_koji_session.getLoggedInUser.return_value = {"name": "alice"}

        hub_session.submit(Path("/x/foo.src.rpm"), "foo", "f44", "f44")

        assert mock_koji_session.packageListAdd.call_args.kwargs["owner"] == "alice"

    def test_scratch(self, hub_session, mock_koji_session):
        hub_session.submit(Path("/x/foo.src.rpm"), "foo", "f44", "f44", scratch=True)

        assert mock_koji_session.build.call_args.args[2] == {"scratch": True}

    def test_package_already_listed(self, hub_session, mock_koji_session):
        mock_koji_session.packageListAdd.side_effect = koji.GenericError(
            "package foo already exists in tag f44"
        )

        assert hub_session.submit(Path("/x/foo.src.rpm"), "foo", "f44", "f44") == 12345

    def test_package_list_error(self, hub_session, mock_koji_session):
        mock_koji_session.packageListAdd.side_effect = koji.GenericError("no such tag")

        with pytest.raises(KojiBuildError, match="packageListAdd failed"):
            hub_session.submit(Path("/x/foo.src.rpm"), "foo", "f44", "f44")

    def test_build_already_exists(self, hub_session, mock_koji_session):
        mock_koji_session.build.side_effect = koji.GenericError(
            "Build already exists (id=7, state=COMPLETE): {...}"
        )

        assert hub_session.submit(Path("/x/foo.src.rpm"), "foo", "f44", "f44") is None

    def test_build_error(self, hub_session, mock_koji_session):
        mock_koji_session.build.side_effect = koji.GenericError("policy violation")

        with pytest.raises(KojiBuildError, match="policy violation"):
            hub_session.submit(Path("/x/foo.src.rpm"), "foo", "f44", "f44")


class TestTasks:
    def test_task_state(self, hub_session, mock_koji_session):
        mock_koji_session.getTaskInfo.return_value = {"state": 5}

        assert hub_session.task_state(1) == "failed"

    def test_task_state_error(self, hub_session, mock_koji_session):
        mock_koji_session.getTaskInfo.side_effect = OSError("timeout")

        assert hub_session.task_state(1) is None

    def test_subtasks(self, hub_session, mock_koji_session):
        mock_koji_session.listTasks.return_value = [{"id": 2}]

        assert hub_session.subtasks(1) == [{"id": 2}]
        mock_koji_session.listTasks.assert_called_once_with(opts={"parent": 1})

    def test_subtasks_error(self, hub_session, mock_koji_session):
        mock_koji_session.listTasks.side_effect = OSError("timeout")

        assert hub_session.subtasks(1) == []

    def test_log_tail_reads_from_offset(self, hub_session, mock_koji_session):
        mock_koji_session.listTaskOutput.return_value = {"build.log": {"st_size": 1000}}
        mock_koji_session.downloadTaskOutput.return_value = b"tail"

        assert hub_session.task_log_tail(3, "build.log", 100) == "tail"
        mock_koji_session.downloadTaskOutput.assert_called_once_with(
            3, "build.log", offset=900, size=-1
        )

    def test_log_tail_small_file(self, hub_session, mock_koji_session):
        mock_koji_session.listTaskOutput.return_value = {"build.log": {"st_size": 10}}
        mock_koji_session.downloadTaskOutput.return_value = b"all"

        hub_session.task_log_tail(3, "build.log", 100)

        assert mock_koji_session.downloadTaskOutput.call_args.kwargs["offset"] == 0

    def test_log_tail_missing_file(self, hub_session, mock_koji_session):
        mock_koji_session.listTaskOutput.return_value = {"root.log": {"st_size": 10}}

        assert hub_session.task_log_tail(3, "build.log", 100) is None
        mock_koji_session.downloadTaskOutput.assert_not_called()

    def test_log_tail_error(self, hub_session, mock_koji_session):
        mock_koji_session.listTaskOutput.side_effect = koji.GenericError("no such task")

        assert hub_session.task_log_tail(3, "build.log", 100) is None

    def test_wait_task(self, hub_session, mock_koji_session, mocker):
        mocker.patch("vibebuild.koji_client.time.sleep")
        mock_koji_session.getTaskInfo.side_effect = [{"state": 1}, None, {"state": 2}]

        assert hub_session.wait_task(1) == "closed"

    def test_wait_task_timeout(self, hub_session, mock_koji_session, mocker):
        mocker.patch("vibebuild.koji_client.time.sleep")
        mocker.patch("vibebuild.koji_client.time.time", side_effect=[0, 0, 0, 100])
        mock_koji_session.getTaskInfo.return_value = {"state": 1}

        assert hub_session.wait_task(1, timeout=10) == "timeout"


class TestRepo:
    @pytest.fixture
    def tags(self, mock_koji_session):
        mock_koji_session.getRepo.return_value = {"id": 1, "create_event": 500}
        mock_koji_session.getTag.return_value = {"id": 10}
        mock_koji_session.getFullInheritance.return_value = [{"parent_id": 11}]
        mock_koji_session.tagChangedSinceEvent.return_value = False
        return mock_koji_session

    def test_repo_is_current(self, hub_session, tags):
        assert hub_session.repo_is_current("f44-build") is True
        tags.tagChangedSinceEvent.assert_called_once_with(500, [10, 11])

    def test_repo_changed(self, hub_session, tags):
        tags.tagChangedSinceEvent.return_value = True

        assert hub_session.repo_is_current("f44-build") is False

    def test_no_repo(self, hub_session, tags):
        tags.getRepo.return_value = None

        assert hub_session.repo_is_current("f44-build") is False

    def test_repo_check_error(self, hub_session, tags):
        tags.getTag.side_effect = koji.GenericError("No such tag")

        assert hub_session.repo_is_current("f44-build") is False

    def test_regenerate_repo(self, hub_session, mock_koji_session):
        assert hub_session.regenerate_repo("f44-build") is True
        mock_koji_session.newRepo.assert_called_once_with("f44-build")

    def test_regenerate_repo_fails_to_start(self, hub_session, mock_koji_session):
        mock_koji_session.newRepo.side_effect = koji.GenericError("denied")

        assert hub_session.regenerate_repo("f44-build") is False

    def test_regenerate_repo_task_fails(self, hub_session, mock_koji_session):
        mock_koji_session.getTaskInfo.return_value = {"state": 5}

        assert hub_session.regenerate_repo("f44-build") is False
