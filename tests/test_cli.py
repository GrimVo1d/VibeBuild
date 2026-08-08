"""Tests for vibebuild.cli module."""

import json
import logging
import subprocess  # nosec B404
import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

from vibebuild._logging import clean
from vibebuild.analyzer import BuildRequirement, PackageInfo
from vibebuild.builder import BuildResult, BuildStatus, BuildTask
from vibebuild.cli import (
    cmd_analyze,
    cmd_build,
    cmd_download,
    create_parser,
    detect_fedora_release,
    ensure_srpm_path,
    fedora_release,
    load_koji_config,
    main,
    name_resolver_for,
    print_build_result,
    setup_logging,
)
from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError

NO_KOJI_CONFIG = {
    "server": None,
    "cert": None,
    "serverca": None,
    "target": None,
    "build_tag": None,
}


@pytest.fixture(autouse=True)
def koji_config():
    """No ~/.koji/config from the host: tests set what they need."""
    config = dict(NO_KOJI_CONFIG)
    with patch("vibebuild.cli.load_koji_config", return_value=config):
        yield config


@pytest.fixture(autouse=True)
def koji_client():
    """cmd_build and cmd_analyze look the target up in Koji: answer like a real target."""
    with patch("vibebuild.cli.KojiClient") as client:
        client.return_value.get_target.return_value = {
            "build_tag": "target-build",
            "dest_tag": "target-dest",
        }
        yield client


def make_opts(*argv: str, **overrides):
    """Parsed command-line options, with attributes overridden."""
    opts = create_parser().parse_args(list(argv))
    for key, value in overrides.items():
        setattr(opts, key, value)
    return opts


def _info(name="test", requires=()):
    return PackageInfo(
        name=name, version="1.0", release="1", build_requires=list(requires), source_urls=[]
    )


@pytest.fixture
def srpm(tmp_path):
    path = tmp_path / "test.src.rpm"
    path.write_text("fake srpm")
    return path


class TestCreateParser:
    def test_positionals(self):
        args = create_parser().parse_args(["my-target", "my-package.src.rpm"])

        assert args.target == "my-target"
        assert args.srpm == "my-package.src.rpm"

    @pytest.mark.parametrize(
        ("flag", "attr"),
        [
            ("--scratch", "scratch"),
            ("--nowait", "nowait"),
            ("--no-deps", "no_deps"),
            ("--build-all-deps", "build_all_deps"),
            ("--patch-specs", "patch_specs"),
            ("--allow-unsandboxed", "allow_unsandboxed"),
            ("--no-ssl-verify", "no_ssl_verify"),
            ("--no-name-resolution", "no_name_resolution"),
            ("--no-ml", "no_ml"),
            ("--analyze-only", "analyze_only"),
            ("--download-only", "download_only"),
            ("--dry-run", "dry_run"),
            ("-v", "verbose"),
            ("-q", "quiet"),
        ],
    )
    def test_flags(self, flag, attr):
        args = create_parser().parse_args([flag, "target", "pkg.src.rpm"])

        assert getattr(args, attr) is True

    @pytest.mark.parametrize(
        ("option", "attr", "value"),
        [
            ("--server", "server", "https://custom.koji/kojihub"),
            ("--cert", "cert", "/path/to/cert.pem"),
            ("--serverca", "serverca", "/path/to/ca.crt"),
            ("--build-tag", "build_tag", "custom-build"),
            ("--download-dir", "download_dir", "/tmp/downloads"),
            ("--fedora-release", "fedora_release", "44"),
            ("--ml-model", "ml_model", "/m.npz"),
        ],
    )
    def test_options(self, option, attr, value):
        args = create_parser().parse_args([option, value, "target", "pkg.src.rpm"])

        assert getattr(args, attr) == value

    def test_defaults_without_config(self):
        args = create_parser().parse_args(["target", "pkg.src.rpm"])

        assert args.server == "https://koji.fedoraproject.org/kojihub"
        assert args.build_tag is None
        assert args.cert is None

    def test_defaults_from_config(self, koji_config):
        koji_config.update(server="https://hub/kojihub", cert="/c.pem", build_tag="f44-build")

        args = create_parser().parse_args(["target", "pkg.src.rpm"])

        assert args.server == "https://hub/kojihub"
        assert args.cert == "/c.pem"
        assert args.build_tag == "f44-build"

    def test_removed_options(self):
        for option in ("--web-url", "--force", "--no-idempotency", "--help-all"):
            with pytest.raises(SystemExit):
                create_parser().parse_args([option, "target", "pkg.src.rpm"])

    def test_help_lists_all_groups(self):
        text = create_parser().format_help()

        assert "Koji options" in text
        assert "Build options" in text
        assert "Mode options" in text


class TestSetupLogging:
    @pytest.mark.parametrize(
        ("kwargs", "level"),
        [
            ({}, logging.INFO),
            ({"verbose": True}, logging.DEBUG),
            ({"quiet": True}, logging.WARNING),
        ],
    )
    def test_level(self, kwargs, level):
        setup_logging(**kwargs)

        assert logging.getLogger().level == level

    def test_json_logs(self):
        from vibebuild._logging import JsonFormatter

        setup_logging(json_logs=True)

        assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_json_logs_drop_del_and_c1(self):
        setup_logging(json_logs=True)
        record = logging.LogRecord("x", logging.ERROR, "", 0, "a\x9b31m\x7fb", (), None)

        line = logging.getLogger().handlers[0].format(record)

        assert "\x9b" not in line and "\x7f" not in line
        assert json.loads(line)["message"] == "a?31m?b"

    def test_text_logs_drop_terminal_escapes(self):
        setup_logging()
        record = logging.LogRecord(
            "x", logging.ERROR, "", 0, "Build FAILED: %s", ("\x1b]0;pwn\x07",), None
        )

        line = logging.getLogger().handlers[0].format(record)

        assert "\x1b" not in line and "\x07" not in line
        assert line.endswith("Build FAILED: ?]0;pwn?")

    def test_text_log_lines_inside_a_message_are_indented(self):
        setup_logging()
        record = logging.LogRecord(
            "x", logging.ERROR, "", 0, "task failed: %s", ("oops\n12:00:00 [INFO] fine",), None
        )

        line = logging.getLogger().handlers[0].format(record)

        assert line.endswith("task failed: oops\n    12:00:00 [INFO] fine")

    def test_bidi_and_zero_width_marks_are_dropped(self):
        assert clean("a\u202eb\u200bc\u2066d") == "a?b?c?d"


class TestFedoraRelease:
    @pytest.mark.parametrize(
        ("target", "release"),
        [("f44", "44"), ("f44-candidate", None), ("rawhide", None), (None, None)],
    )
    def test_detect(self, target, release):
        assert detect_fedora_release(target) == release

    def test_explicit_wins(self):
        assert fedora_release(make_opts(fedora_release="43"), "f44") == "43"

    def test_from_target(self):
        assert fedora_release(make_opts(), "f44") == "44"

    def test_rawhide_fallback(self):
        assert fedora_release(make_opts(), "my-target") == "rawhide"
        assert fedora_release(make_opts(), None) == "rawhide"


class TestNameResolverFor:
    def test_disabled(self):
        assert name_resolver_for(make_opts("--no-name-resolution")) is None

    def test_passes_ml_options(self):
        with patch("vibebuild.cli.create_name_resolver") as create:
            resolver = name_resolver_for(make_opts("--no-ml", "--ml-model", "/m.npz"))

        create.assert_called_once_with(no_ml=True, ml_model_path="/m.npz")
        assert resolver is create.return_value


class TestPrintBuildResult:
    def test_prints_success(self, capsys):
        print_build_result(
            BuildResult(success=True, built_packages=["pkg1", "pkg2"], total_time=60.0)
        )

        out = capsys.readouterr().out
        assert "SUCCESS" in out
        assert "pkg1" in out
        assert "pkg2" in out
        assert "60.0" in out

    def test_prints_failure(self, capsys):
        print_build_result(
            BuildResult(success=False, built_packages=["pkg1"], failed_packages=["pkg2"])
        )

        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "pkg2" in out

    def test_already_built_is_not_counted_as_built(self, capsys):
        print_build_result(BuildResult(success=True, already_built=["main"]))

        out = capsys.readouterr().out
        assert "Packages built: 0" in out
        assert "Already built: 1" in out
        assert "Already built, nothing submitted:\n  = main" in out
        assert "Successfully built" not in out

    def test_untrusted_text_cannot_send_terminal_escapes(self, capsys):
        task = BuildTask(
            "pkg\x1b[2J", "/p", "t", status=BuildStatus.FAILED, error_message="bad\x1b[31m\nnext"
        )
        print_build_result(
            BuildResult(success=False, failed_packages=[task.package_name], tasks=[task])
        )

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "✗ pkg?[2J" in out
        assert "Error: bad?[31m next" in out

    def test_untrusted_text_cannot_fake_summary_lines(self, capsys):
        task = BuildTask(
            "pkg", "/p", "t", status=BuildStatus.FAILED, error_message="boom\nStatus: SUCCESS ✓"
        )
        print_build_result(BuildResult(success=False, failed_packages=["pkg"], tasks=[task]))

        lines = capsys.readouterr().out.splitlines()
        assert "Status: FAILED ✗" in lines
        assert "Status: SUCCESS ✓" not in lines

    def test_prints_skipped_target(self, capsys):
        print_build_result(
            BuildResult(success=False, failed_packages=["dep"], skipped_packages=["main"])
        )

        out = capsys.readouterr().out
        assert "Not built, a dependency failed:\n  - main" in out

    def test_prints_tasks(self, capsys):
        tasks = [
            BuildTask("built", "/p", "t", task_id=12345, status=BuildStatus.COMPLETE),
            BuildTask("skipped", "/p", "t", status=BuildStatus.ALREADY_BUILT),
            BuildTask("bad", "/p", "t", status=BuildStatus.FAILED, error_message="Dep failed"),
        ]
        print_build_result(BuildResult(success=False, tasks=tasks))

        out = capsys.readouterr().out
        assert "✓ built: complete" in out
        assert "12345" in out
        assert "= skipped: already_built" in out
        assert "Dep failed" in out

    def test_prints_tagged_builds_and_toolchains(self, capsys):
        print_build_result(
            BuildResult(
                success=True,
                tagged_target="main-1-1",
                tagged_deps=["dep-1-1"],
                toolchains=["Python"],
            )
        )

        out = capsys.readouterr().out
        assert "main-1-1" in out
        assert "Tagged dependencies (1)" in out
        assert "Python" in out


class TestCmdAnalyze:
    def test_analyze_success(self, srpm, capsys):
        with (
            patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()),
            patch("vibebuild.cli.DependencyResolver") as mock_resolver,
        ):
            mock_resolver.return_value.find_missing_deps.return_value = []
            result = cmd_analyze(make_opts(build_tag="tag"), str(srpm), None)

        assert result == 0
        out = capsys.readouterr().out
        assert "NVR: test-1.0-1" in out
        assert "All dependencies available" in out

    def test_analyze_with_missing_deps(self, srpm, capsys):
        with (
            patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()),
            patch("vibebuild.cli.DependencyResolver") as mock_resolver,
        ):
            mock_resolver.return_value.find_missing_deps.return_value = ["missing-dep"]
            result = cmd_analyze(make_opts(build_tag="tag"), str(srpm), None)

        assert result == 0
        assert "✗ missing-dep" in capsys.readouterr().out

    def test_analyze_uses_build_resolver(self, srpm, koji_client):
        requires = [BuildRequirement(name="python3dist(foo)", operator=">=", version="2")]
        opts = make_opts("--build-all-deps", "--server", "https://hub/kojihub", build_tag="tag")

        with (
            patch(
                "vibebuild.cli.get_package_info_from_srpm", return_value=_info(requires=requires)
            ),
            patch("vibebuild.cli.create_name_resolver") as mock_nr,
            patch("vibebuild.cli.DependencyResolver") as mock_resolver,
        ):
            mock_resolver.return_value.find_missing_deps.return_value = []
            cmd_analyze(opts, str(srpm), None)

        koji_client.assert_called_once_with(
            server="https://hub/kojihub", serverca=None, no_ssl_verify=False
        )
        mock_resolver.assert_called_once_with(
            koji_client=ANY,
            koji_tag="tag",
            name_resolver=mock_nr.return_value,
            build_all_deps=True,
        )
        mock_resolver.return_value.find_missing_deps.assert_called_once_with(requires)

    def test_analyze_no_name_resolution(self, srpm):
        with (
            patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()),
            patch("vibebuild.cli.create_name_resolver") as mock_nr,
            patch("vibebuild.cli.DependencyResolver") as mock_resolver,
        ):
            mock_resolver.return_value.find_missing_deps.return_value = []
            cmd_analyze(make_opts("--no-name-resolution", build_tag="tag"), str(srpm), None)

        mock_nr.assert_not_called()
        assert mock_resolver.call_args.kwargs["name_resolver"] is None

    def test_analyze_build_tag_from_target(self, srpm, capsys, koji_client):
        with (
            patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()),
            patch("vibebuild.cli.DependencyResolver") as mock_resolver,
        ):
            mock_resolver.return_value.find_missing_deps.return_value = []
            result = cmd_analyze(make_opts(), str(srpm), "f44")

        assert result == 0
        koji_client.return_value.get_target.assert_called_once_with("f44")
        assert mock_resolver.call_args.kwargs["koji_tag"] == "target-build"
        assert "Build tag: target-build" in capsys.readouterr().out

    def test_analyze_without_build_tag_or_target(self, srpm):
        with patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()):
            assert cmd_analyze(make_opts(), str(srpm), None) == 1

    def test_analyze_failure(self, srpm):
        with patch("vibebuild.cli.get_package_info_from_srpm", side_effect=Exception("bad")):
            assert cmd_analyze(make_opts(build_tag="tag"), str(srpm), None) == 1


class TestCmdDownload:
    def test_download_success(self, tmp_path, capsys):
        opts = make_opts("--download-dir", str(tmp_path), "--no-ssl-verify")
        with (
            patch("vibebuild.cli.create_name_resolver") as mock_nr,
            patch("vibebuild.cli.SRPMFetcher") as mock_fetcher,
        ):
            mock_fetcher.return_value.download_srpm.return_value = "/path/to/pkg.src.rpm"
            result = cmd_download(opts, "python-requests", "44")

        assert result == 0
        mock_fetcher.assert_called_once_with(
            download_dir=str(tmp_path),
            fedora_release="44",
            name_resolver=mock_nr.return_value,
        )
        assert "Downloaded: /path/to/pkg.src.rpm" in capsys.readouterr().out

    def test_download_failure(self):
        with patch("vibebuild.cli.SRPMFetcher") as mock_fetcher:
            mock_fetcher.return_value.download_srpm.side_effect = Exception("Not found")
            assert cmd_download(make_opts(), "nonexistent-pkg", "rawhide") == 1


class TestCmdBuild:
    def test_builder_options(self, srpm, koji_client):
        opts = make_opts(
            "--server",
            "https://hub/kojihub",
            "--cert",
            "/c.pem",
            "--scratch",
            "--patch-specs",
            "--no-name-resolution",
        )
        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            mock_builder.return_value.build_with_deps.return_value = BuildResult(success=True)
            cmd_build(opts, "f44", str(srpm), "44")

        mock_builder.assert_called_once_with(
            koji_server="https://hub/kojihub",
            target="f44",
            build_tag="target-build",
            dest_tag="target-dest",
            cert="/c.pem",
            serverca=None,
            no_ssl_verify=False,
            scratch=True,
            nowait=False,
            build_all_deps=False,
            patch_specs=True,
            allow_unsandboxed=False,
            max_deps=100,
            name_resolver=None,
            download_dir=None,
            fedora_release="44",
        )

    def test_explicit_build_tag_wins(self, srpm):
        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            mock_builder.return_value.build_with_deps.return_value = BuildResult(success=True)
            cmd_build(make_opts(build_tag="my-build"), "f44", str(srpm), "44")

        kwargs = mock_builder.call_args.kwargs
        assert kwargs["build_tag"] == "my-build"
        assert kwargs["dest_tag"] == "target-dest"

    def test_unknown_target(self, srpm, koji_client):
        koji_client.return_value.get_target.return_value = None

        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            result = cmd_build(make_opts(), "nope", str(srpm), "rawhide")

        assert result == 1
        mock_builder.assert_not_called()

    def test_file_not_found(self):
        assert cmd_build(make_opts(), "f44", "/nonexistent/path.src.rpm", "44") == 1

    def test_build_with_deps(self, srpm, capsys):
        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            mock_builder.return_value.build_with_deps.return_value = BuildResult(
                success=True, built_packages=["test"], total_time=30.0
            )
            result = cmd_build(make_opts(), "f44", str(srpm), "44")

        assert result == 0
        mock_builder.return_value.build_with_deps.assert_called_once_with(
            str(srpm), resolve_deps=True
        )
        assert "BUILD SUMMARY" in capsys.readouterr().out

    def test_no_deps(self, srpm):
        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            mock_builder.return_value.build_with_deps.return_value = BuildResult(success=True)
            cmd_build(make_opts("--no-deps"), "f44", str(srpm), "44")

        mock_builder.return_value.build_with_deps.assert_called_once_with(
            str(srpm), resolve_deps=False
        )

    def test_failed_build_returns_1(self, srpm):
        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            mock_builder.return_value.build_with_deps.return_value = BuildResult(success=False)
            assert cmd_build(make_opts(), "f44", str(srpm), "44") == 1

    @pytest.mark.parametrize("error", [VibeBuildError("vibe error"), RuntimeError("unexpected")])
    def test_errors_return_1(self, srpm, error):
        with patch("vibebuild.cli.KojiBuilder") as mock_builder:
            mock_builder.return_value.build_with_deps.side_effect = error
            assert cmd_build(make_opts(), "f44", str(srpm), "44") == 1


def test_unusable_download_dir_is_an_error_not_a_traceback(srpm, caplog):
    with (
        patch("vibebuild.cli.resolve_target_tags", return_value=("f44-build", "f44")),
        patch("vibebuild.cli.KojiClient"),
        patch("vibebuild.cli.KojiBuilder", side_effect=VibeBuildError("Download dir /x is shared")),
    ):
        assert cmd_build(make_opts(), "f44", str(srpm), "44") == 1

    assert "Download dir /x is shared" in caplog.text


class TestDryRun:
    def test_with_dependencies(self, srpm, capsys):
        with (
            patch("vibebuild.cli.KojiBuilder") as mock_builder,
            patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()),
        ):
            mock_builder.return_value.resolve_build_chain.return_value = [["dep1"], ["dep2"]]
            result = cmd_build(make_opts("--dry-run"), "f44", str(srpm), "44")

        assert result == 0
        mock_builder.return_value.build_with_deps.assert_not_called()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "Dependencies to build (2)" in out
        assert "Level 2: dep2" in out
        assert "Then: test-1.0-1" in out

    def test_without_dependencies(self, srpm, capsys):
        with (
            patch("vibebuild.cli.KojiBuilder") as mock_builder,
            patch(
                "vibebuild.cli.get_package_info_from_srpm",
                return_value=_info(requires=["gcc", "make"]),
            ),
        ):
            mock_builder.return_value.resolve_build_chain.return_value = []
            cmd_build(make_opts("--dry-run"), "f44", str(srpm), "44")

        out = capsys.readouterr().out
        assert "No dependencies to build: all 2 BuildRequires are available" in out

    def test_no_deps_skips_resolution(self, srpm, capsys):
        with (
            patch("vibebuild.cli.KojiBuilder") as mock_builder,
            patch("vibebuild.cli.get_package_info_from_srpm", return_value=_info()),
        ):
            cmd_build(make_opts("--dry-run", "--no-deps"), "f44", str(srpm), "44")

        mock_builder.return_value.resolve_build_chain.assert_not_called()
        assert "Target package: test-1.0-1" in capsys.readouterr().out


class TestEnsureSrpmPath:
    def test_existing_file(self, srpm):
        assert ensure_srpm_path(make_opts(), str(srpm), "44") == str(srpm.resolve())

    def test_downloads_by_name(self, tmp_path):
        with (
            patch("vibebuild.cli.create_name_resolver"),
            patch("vibebuild.cli.SRPMFetcher") as mock_fetcher,
        ):
            mock_fetcher.return_value.download_srpm.return_value = "/path/to/downloaded.src.rpm"
            result = ensure_srpm_path(make_opts(), "python3", "44")

        assert result == "/path/to/downloaded.src.rpm"
        assert mock_fetcher.call_args.kwargs["fedora_release"] == "44"

    @pytest.mark.parametrize("arg", ["./missing.src.rpm", "missing-1.0-1.src.rpm", "/no/such"])
    def test_missing_path_is_not_downloaded(self, arg):
        with patch("vibebuild.cli.SRPMFetcher") as mock_fetcher:
            with pytest.raises(SRPMNotFoundError, match="SRPM not found"):
                ensure_srpm_path(make_opts(), arg, "44")

        mock_fetcher.assert_not_called()


class TestMain:
    @pytest.mark.parametrize("argv", [["--analyze-only"], ["--download-only"], []])
    def test_missing_srpm_is_usage_error(self, argv):
        with pytest.raises(SystemExit) as exc_info:
            main(argv)

        assert exc_info.value.code == 2

    def test_single_arg_without_config_target_fails(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["only-package"])

        assert exc_info.value.code == 2

    def test_single_arg_uses_config_target(self, srpm, koji_config):
        koji_config["target"] = "f44"

        with patch("vibebuild.cli.cmd_build", return_value=0) as mock_cmd:
            assert main([str(srpm)]) == 0

        _, target, path, release = mock_cmd.call_args.args
        assert target == "f44"
        assert path == str(srpm.resolve())
        assert release == "44"

    def test_two_args_explicit_target(self, srpm, koji_config):
        koji_config["target"] = "config-target"

        with patch("vibebuild.cli.cmd_build", return_value=0) as mock_cmd:
            main(["explicit-target", str(srpm)])

        assert mock_cmd.call_args.args[1] == "explicit-target"

    def test_package_name_is_downloaded_for_the_target_release(self):
        with (
            patch("vibebuild.cli.ensure_srpm_path", return_value="/d/x.src.rpm") as ensure,
            patch("vibebuild.cli.cmd_build", return_value=0) as mock_cmd,
        ):
            main(["f44", "python-requests"])

        assert ensure.call_args.args[1:] == ("python-requests", "44")
        assert mock_cmd.call_args.args[2] == "/d/x.src.rpm"

    def test_download_failure(self):
        with patch("vibebuild.cli.ensure_srpm_path", side_effect=Exception("download failed")):
            assert main(["target", "nonexistent-pkg"]) == 1

    def test_analyze_only_with_build_tag(self, srpm):
        with patch("vibebuild.cli.cmd_analyze", return_value=0) as mock_cmd:
            assert main(["--analyze-only", "--build-tag", "f44-build", str(srpm)]) == 0

        opts, path, target = mock_cmd.call_args.args
        assert opts.build_tag == "f44-build"
        assert path == str(srpm.resolve())
        assert target is None

    def test_analyze_only_with_target(self, srpm):
        with patch("vibebuild.cli.cmd_analyze", return_value=0) as mock_cmd:
            main(["--analyze-only", "f44", str(srpm)])

        assert mock_cmd.call_args.args[2] == "f44"

    def test_analyze_only_with_config_target(self, srpm, koji_config):
        koji_config["target"] = "f44"

        with patch("vibebuild.cli.cmd_analyze", return_value=0) as mock_cmd:
            main(["--analyze-only", str(srpm)])

        assert mock_cmd.call_args.args[2] == "f44"

    def test_analyze_only_without_build_tag_or_target(self, srpm):
        with pytest.raises(SystemExit) as exc_info:
            main(["--analyze-only", str(srpm)])

        assert exc_info.value.code == 2

    def test_download_only(self):
        with patch("vibebuild.cli.cmd_download", return_value=0) as mock_cmd:
            assert main(["--download-only", "python-requests"]) == 0

        _, package, release = mock_cmd.call_args.args
        assert package == "python-requests"
        assert release == "rawhide"

    def test_download_only_with_target(self):
        with patch("vibebuild.cli.cmd_download", return_value=0) as mock_cmd:
            main(["--download-only", "f44", "python-requests"])

        assert mock_cmd.call_args.args[1:] == ("python-requests", "44")

    @pytest.mark.parametrize(("flag", "expected"), [("-v", (True, False)), ("-q", (False, True))])
    def test_logging_flags(self, srpm, flag, expected):
        with (
            patch("vibebuild.cli.cmd_build", return_value=0),
            patch("vibebuild.cli.setup_logging") as mock_logging,
        ):
            main([flag, "target", str(srpm)])

        mock_logging.assert_called_with(*expected, json_logs=False)


class TestLoadKojiConfig:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        """A home directory with ~/.koji, and no /etc/koji.conf."""
        (tmp_path / ".koji").mkdir()
        real_exists = Path.exists
        monkeypatch.setattr("vibebuild.cli.Path.home", lambda: tmp_path)
        monkeypatch.setattr(Path, "exists", lambda p: str(p) != "/etc/koji.conf" and real_exists(p))
        return tmp_path

    def test_reads_koji_section(self, home):
        (home / ".koji" / "config").write_text(
            "[koji]\n"
            "server = https://my-koji/kojihub\n"
            "weburl = https://my-koji/koji\n"
            "cert = ~/client.pem\n"
            "serverca = ~/serverca.pem\n"
            "target = f44\n"
            "build_tag = f44-build\n"
        )

        result = load_koji_config()

        assert result == {
            "server": "https://my-koji/kojihub",
            "cert": str(Path("~/client.pem").expanduser()),
            "serverca": str(Path("~/serverca.pem").expanduser()),
            "target": "f44",
            "build_tag": "f44-build",
        }

    def test_no_files(self, home):
        assert load_koji_config() == NO_KOJI_CONFIG

    def test_percent_in_a_value_is_literal(self, home):
        (home / ".koji" / "config").write_text("[koji]\nserver = https://koji/hub?a=%z\n")

        assert load_koji_config()["server"] == "https://koji/hub?a=%z"

    def test_corrupt_file(self, home):
        (home / ".koji" / "config").write_text("THIS IS NOT\nVALID INI\n[[[broken")

        assert load_koji_config() == NO_KOJI_CONFIG

    def test_without_koji_section(self, home):
        (home / ".koji" / "config").write_text("[other]\nserver = x\n")

        assert load_koji_config() == NO_KOJI_CONFIG

    def test_empty_values(self, home):
        (home / ".koji" / "config").write_text("[koji]\nserver =\n")

        assert load_koji_config() == NO_KOJI_CONFIG


class TestTrainSubcommand:
    """The vibebuild train subcommand."""

    METRICS = {
        "train_size": 100,
        "test_size": 10,
        "predicted": 9,
        "coverage": 0.9,
        "rpm_accuracy": 0.5,
        "srpm_accuracy": 0.7,
    }

    def test_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["train", "--help"])

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "vibebuild train" in out
        assert "--release" in out

    def test_needs_release_or_data(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["train"])

        assert exc_info.value.code == 2

    def test_collect_and_train(self, mocker, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        collect = mocker.patch("vibebuild.training.collect", return_value=[{"provide": "x"}])
        train = mocker.patch("vibebuild.training.train", return_value=self.METRICS)
        saved = tmp_path / "data.json"

        assert main(["train", "--release", "44", "--save-data", str(saved)]) == 0

        collect.assert_called_once_with("44", "x86_64")
        assert train.call_args.args == ([{"provide": "x"}], str(tmp_path / "vibebuild/model.npz"))
        assert json.loads(saved.read_text()) == [{"provide": "x"}]
        assert "SRPM accuracy: 70.0%" in capsys.readouterr().out

    def test_train_from_saved_data(self, mocker):
        load = mocker.patch("vibebuild.training.load_training_data", return_value=[{"p": 1}])
        collect = mocker.patch("vibebuild.training.collect")
        train = mocker.patch(
            "vibebuild.training.train", return_value={"train_size": 1, "test_size": 0}
        )

        assert main(["train", "--data", "d.json", "--output", "/m.npz", "--seed", "7"]) == 0

        load.assert_called_once_with("d.json")
        collect.assert_not_called()
        assert train.call_args.args == ([{"p": 1}], "/m.npz")
        assert train.call_args.kwargs["seed"] == 7

    def test_failure(self, mocker):
        mocker.patch("vibebuild.training.collect", side_effect=VibeBuildError("no mirror"))

        assert main(["train", "--release", "44"]) == 1


def test_missing_koji_module_is_reported_without_traceback():
    # A fresh interpreter where `import koji` fails, as on a host without python3-koji
    code = "import sys; sys.modules['koji'] = None; import vibebuild.cli"
    proc = subprocess.run(  # nosec B603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert proc.returncode == 1
    assert "needs the koji Python module" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_broken_koji_module_shows_the_real_error(tmp_path):
    # koji is installed, but one of its own imports fails
    (tmp_path / "koji").mkdir()
    (tmp_path / "koji" / "__init__.py").write_text("import vibebuild_missing_dep\n")
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import vibebuild.cli"
    proc = subprocess.run(  # nosec B603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert proc.returncode == 1
    assert "No module named 'vibebuild_missing_dep'" in proc.stderr
    assert "needs the koji Python module" not in proc.stderr
