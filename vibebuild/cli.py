"""
VibeBuild CLI - Koji extension for automatic dependency resolution.

Usage:
    vibebuild [OPTIONS] SRPM
    vibebuild [OPTIONS] TARGET SRPM
    vibebuild train [OPTIONS]

SRPM is a path to a .src.rpm file or a package name; a name is downloaded from
Fedora Koji first. Without TARGET, 'target' from ~/.koji/config [koji] is used.
"""

import argparse
import configparser
import json
import logging
import os
import re
import sys
from pathlib import Path

from vibebuild import __version__
from vibebuild.analyzer import get_package_info_from_srpm
from vibebuild.builder import BuildResult, BuildStatus, KojiBuilder
from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError
from vibebuild.fetcher import SRPMFetcher
from vibebuild.koji_client import KojiClient
from vibebuild.ml_resolver import default_model_path
from vibebuild.name_resolver import PackageNameResolver, create_name_resolver
from vibebuild.resolver import DEFAULT_MAX_PACKAGES, DependencyResolver

FEDORA_KOJI = "https://koji.fedoraproject.org/kojihub"


def load_koji_config() -> dict[str, str | None]:
    """Load server, cert, serverca, target and build_tag from ~/.koji/config, /etc/koji.conf."""
    out: dict[str, str | None] = {
        "server": None,
        "cert": None,
        "serverca": None,
        "target": None,
        "build_tag": None,
    }
    for path in [Path.home() / ".koji" / "config", Path("/etc/koji.conf")]:
        if not path.exists():
            continue
        config = configparser.ConfigParser()
        try:
            config.read(path, encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        if not config.has_section("koji"):
            continue
        for key, value in config["koji"].items():
            if key in out and value.strip() and not out[key]:
                value = value.strip()
                out[key] = os.path.expanduser(value) if key in ("cert", "serverca") else value
    return out


def setup_logging(verbose: bool = False, quiet: bool = False, json_logs: bool = False) -> None:
    """Configure the root logger; json_logs switches to one JSON object per line."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    handler = logging.StreamHandler()
    if json_logs:
        from vibebuild._logging import JsonFormatter

        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def detect_fedora_release(target: str | None) -> str | None:
    """Fedora release from a build target named like 'f44' -> '44'."""
    m = re.match(r"^f(\d+)$", target or "")
    return m.group(1) if m else None


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    koji_cfg = load_koji_config()

    parser = argparse.ArgumentParser(
        prog="vibebuild",
        description="Koji build with automatic dependency resolution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  vibebuild python-requests                 # target from ~/.koji/config
  vibebuild f44 my-package-1.0-1.src.rpm    # explicit target
  vibebuild --scratch f44 my-package.src.rpm
  vibebuild --dry-run f44 python-requests
  vibebuild --analyze-only f44 my-package.src.rpm
  vibebuild --download-only python-requests
  vibebuild train --release 44              # train the ML name resolver
""",
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress non-error output")
    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help="Log format: text (default) or json",
    )

    koji_group = parser.add_argument_group("Koji options")
    koji_group.add_argument(
        "--server",
        metavar="URL",
        default=koji_cfg["server"] or FEDORA_KOJI,
        help="Koji hub URL (default: from ~/.koji/config or Fedora Koji)",
    )
    koji_group.add_argument(
        "--cert",
        metavar="FILE",
        default=koji_cfg["cert"],
        help="Client certificate for authentication (default: from ~/.koji/config)",
    )
    koji_group.add_argument(
        "--serverca",
        metavar="FILE",
        default=koji_cfg["serverca"],
        help="CA certificate for server verification (default: from ~/.koji/config)",
    )
    koji_group.add_argument(
        "--build-tag",
        metavar="TAG",
        default=koji_cfg["build_tag"],
        help="Build tag for dependency checking (default: from ~/.koji/config or the target)",
    )
    koji_group.add_argument(
        "--no-ssl-verify",
        action="store_true",
        help="Do not verify the Koji hub's certificate (insecure; not used for Fedora downloads)",
    )

    build_group = parser.add_argument_group("Build options")
    build_group.add_argument(
        "--scratch",
        action="store_true",
        help="Scratch-build the target (not tagged); dependencies are still regular builds",
    )
    build_group.add_argument(
        "--nowait", action="store_true", help="Do not wait for the target build to complete"
    )
    build_group.add_argument(
        "--no-deps", action="store_true", help="Skip dependency resolution, just build the package"
    )
    build_group.add_argument(
        "--build-all-deps",
        action="store_true",
        help="Build entire dependency graph locally, including packages available in Fedora repos",
    )
    build_group.add_argument(
        "--patch-specs",
        action="store_true",
        help="Rebuild SRPMs locally without %%check, and retry builds failing on unpackaged "
        "files; runs rpmbuild on the spec, sandboxed with bwrap",
    )
    build_group.add_argument(
        "--max-deps",
        type=int,
        default=DEFAULT_MAX_PACKAGES,
        metavar="N",
        help=f"Stop if more than N dependencies need building (default: {DEFAULT_MAX_PACKAGES})",
    )
    build_group.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="With --patch-specs, run rpmbuild without a sandbox where bwrap cannot create one",
    )
    build_group.add_argument(
        "--download-dir",
        metavar="DIR",
        help="Directory for downloaded SRPMs (default: ~/.cache/vibebuild/srpms)",
    )
    build_group.add_argument(
        "--fedora-release",
        metavar="VER",
        help="Fedora release to fetch SRPMs from (e.g. 44). "
        "Default: from the target name (f44), else rawhide",
    )
    build_group.add_argument(
        "--no-name-resolution",
        action="store_true",
        help="Disable package name normalization (macros, virtual provides)",
    )
    build_group.add_argument(
        "--no-ml", action="store_true", help="Disable ML-based package name resolution"
    )
    build_group.add_argument(
        "--ml-model",
        metavar="PATH",
        help=f"ML model trained with 'vibebuild train' (default: {default_model_path()})",
    )

    mode_group = parser.add_argument_group("Mode options")
    mode_group.add_argument(
        "--analyze-only", action="store_true", help="Only analyze dependencies, do not build"
    )
    mode_group.add_argument(
        "--download-only", action="store_true", help="Only download SRPM, do not build"
    )
    mode_group.add_argument(
        "--dry-run", action="store_true", help="Show what would be built without actually building"
    )

    parser.add_argument("target", nargs="?", help="Build target (e.g. f44)")
    parser.add_argument(
        "srpm",
        nargs="?",
        help="Path to .src.rpm file or package name; a name is downloaded first",
    )
    return parser


def print_build_result(result: BuildResult) -> None:
    """Print build result summary."""
    print("\n" + "=" * 60)
    print("BUILD SUMMARY")
    print("=" * 60)

    print("Status: SUCCESS ✓" if result.success else "Status: FAILED ✗")
    print(f"Total time: {result.total_time:.1f} seconds")
    print(f"Packages built: {len(result.built_packages)}")
    if result.already_built:
        print(f"Already built: {len(result.already_built)}")
    print(f"Packages failed: {len(result.failed_packages)}")

    if result.tagged_target:
        print("\nTagged packages:")
        print(f"  ✓ {result.tagged_target}")

    if result.tagged_deps:
        print(f"\nTagged dependencies ({len(result.tagged_deps)}):")
        for nvr in result.tagged_deps:
            print(f"  ✓ {nvr}")

    if result.toolchains:
        print(f"\nLanguages / Toolchains detected: {', '.join(result.toolchains)}")

    if result.built_packages:
        print("\nSuccessfully built:")
        for pkg in result.built_packages:
            print(f"  ✓ {pkg}")

    if result.already_built:
        print("\nAlready built, nothing submitted:")
        for pkg in result.already_built:
            print(f"  = {pkg}")

    if result.failed_packages:
        print("\nFailed packages:")
        for pkg in result.failed_packages:
            print(f"  ✗ {pkg}")

    if result.skipped_packages:
        print("\nNot built, a dependency failed:")
        for pkg in result.skipped_packages:
            print(f"  - {pkg}")

    if result.tasks:
        print("\nBuild tasks:")
        icons = {
            BuildStatus.COMPLETE: "✓",
            BuildStatus.ALREADY_BUILT: "=",
            BuildStatus.FAILED: "✗",
            BuildStatus.BUILDING: "⏳",
            BuildStatus.PENDING: "○",
            BuildStatus.CANCELED: "⊘",
        }
        for task in result.tasks:
            print(f"  {icons.get(task.status, '?')} {task.package_name}: {task.status.value}")
            if task.task_id:
                print(f"      Task ID: {task.task_id}")
            if task.error_message:
                print(f"      Error: {task.error_message[:300]}")

    print("=" * 60)


def resolve_target_tags(
    koji_client: KojiClient, target: str, build_tag: str | None = None
) -> tuple[str, str]:
    """Return (build_tag, dest_tag) of a Koji target; an explicit build tag wins."""
    info = koji_client.get_target(target)
    if info is None:
        raise VibeBuildError(f"Build target not found in Koji: {target}")
    return build_tag or info["build_tag"], info["dest_tag"]


def fedora_release(opts: argparse.Namespace, target: str | None) -> str:
    """Release to fetch SRPMs from: --fedora-release, else the target's (f44), else rawhide."""
    return opts.fedora_release or detect_fedora_release(target) or "rawhide"


def name_resolver_for(opts: argparse.Namespace) -> PackageNameResolver | None:
    """The name resolver the options ask for (None with --no-name-resolution)."""
    if opts.no_name_resolution:
        return None
    return create_name_resolver(no_ml=opts.no_ml, ml_model_path=opts.ml_model)


def ensure_srpm_path(opts: argparse.Namespace, srpm_arg: str, release: str) -> str:
    """Path of the SRPM: srpm_arg itself if it is a file, else the downloaded package."""
    p = Path(srpm_arg)
    if p.exists():
        return str(p.resolve())
    if "/" in srpm_arg or srpm_arg.endswith(".rpm"):
        # A path, not a package name: do not ask Fedora for a package called "x.src.rpm"
        raise SRPMNotFoundError(f"SRPM not found: {srpm_arg}")
    logging.info("Downloading SRPM for: %s", srpm_arg)
    fetcher = SRPMFetcher(
        download_dir=opts.download_dir,
        fedora_release=release,
        name_resolver=name_resolver_for(opts),
    )
    path = fetcher.download_srpm(srpm_arg)
    logging.info("Downloaded: %s", path)
    return path


def cmd_analyze(opts: argparse.Namespace, srpm_path: str, target: str | None) -> int:
    """Analyze SRPM dependencies the same way a build would, without downloading or building."""
    print(f"Analyzing: {srpm_path}")

    try:
        package_info = get_package_info_from_srpm(srpm_path)
        print(f"\nPackage: {package_info.name}")
        print(f"Version: {package_info.version}")
        print(f"Release: {package_info.release}")
        print(f"NVR: {package_info.nvr}")

        print(f"\nBuildRequires ({len(package_info.build_requires)}):")
        for req in package_info.build_requires:
            print(f"  - {req}")

        print("\nChecking availability in Koji...")
        koji_client = KojiClient(
            server=opts.server, serverca=opts.serverca, no_ssl_verify=opts.no_ssl_verify
        )
        build_tag = opts.build_tag
        if not build_tag:
            if not target:
                raise VibeBuildError("No build tag: pass --build-tag or a target")
            build_tag, _ = resolve_target_tags(koji_client, target)
        print(f"Build tag: {build_tag}")

        resolver = DependencyResolver(
            koji_client=koji_client,
            koji_tag=build_tag,
            name_resolver=name_resolver_for(opts),
            build_all_deps=opts.build_all_deps,
        )
        missing = resolver.find_missing_deps(package_info.build_requires)

        if missing:
            print(f"\nMissing dependencies ({len(missing)}):")
            for dep in missing:
                print(f"  ✗ {dep}")
        else:
            print("\n✓ All dependencies available")
        return 0

    except Exception as e:
        logging.error("Analysis failed: %s", e)
        return 1


def cmd_download(opts: argparse.Namespace, package_name: str, release: str) -> int:
    """Download SRPM from Fedora."""
    print(f"Downloading SRPM for: {package_name}")
    try:
        fetcher = SRPMFetcher(
            download_dir=opts.download_dir,
            fedora_release=release,
            name_resolver=name_resolver_for(opts),
        )
        srpm_path = fetcher.download_srpm(package_name)
    except Exception as e:
        logging.error("Download failed: %s", e)
        return 1
    print(f"✓ Downloaded: {srpm_path}")
    return 0


def cmd_build(opts: argparse.Namespace, target: str, srpm_path: str, release: str) -> int:
    """Build package with dependency resolution (or show the plan with --dry-run)."""
    if not Path(srpm_path).exists():
        logging.error("SRPM not found: %s", srpm_path)
        return 1

    try:
        koji_client = KojiClient(
            server=opts.server, serverca=opts.serverca, no_ssl_verify=opts.no_ssl_verify
        )
        build_tag, dest_tag = resolve_target_tags(koji_client, target, opts.build_tag)
    except VibeBuildError as e:
        logging.error("%s", e)
        return 1

    builder = KojiBuilder(
        koji_server=opts.server,
        target=target,
        build_tag=build_tag,
        dest_tag=dest_tag,
        cert=opts.cert,
        serverca=opts.serverca,
        no_ssl_verify=opts.no_ssl_verify,
        scratch=opts.scratch,
        nowait=opts.nowait,
        build_all_deps=opts.build_all_deps,
        patch_specs=opts.patch_specs,
        allow_unsandboxed=opts.allow_unsandboxed,
        max_deps=opts.max_deps,
        name_resolver=name_resolver_for(opts),
        download_dir=opts.download_dir,
        fedora_release=release,
    )

    try:
        if opts.dry_run:
            print("DRY RUN - showing what would be built:\n")
            nvr = get_package_info_from_srpm(srpm_path).nvr
            print(f"Target package: {nvr}")
            if not opts.no_deps:
                build_chain = builder.resolve_build_chain(srpm_path)
                if build_chain:
                    print(f"\nDependencies to build ({sum(len(lvl) for lvl in build_chain)}):")
                    for level_idx, level in enumerate(build_chain):
                        print(f"  Level {level_idx + 1}: {', '.join(level)}")
                    print(f"Then: {nvr}")
                else:
                    print("\nNo dependencies to build")
            return 0

        result = builder.build_with_deps(srpm_path, resolve_deps=not opts.no_deps)
        print_build_result(result)
        return 0 if result.success else 1

    except VibeBuildError as e:
        logging.error("Build failed: %s", e)
        return 1
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        return 1


def _train_subcommand(argv: list[str]) -> int:
    """vibebuild train: collect training data from Fedora repodata and train the ML model."""
    sub = argparse.ArgumentParser(
        prog="vibebuild train",
        description="Train the ML name resolver on the provides of a Fedora release",
    )
    source = sub.add_mutually_exclusive_group(required=True)
    source.add_argument("--release", help="Fedora release to collect data from (44, rawhide)")
    source.add_argument("--data", metavar="FILE", help="Train on data saved with --save-data")
    sub.add_argument("--arch", default="x86_64", help="Architecture (default x86_64)")
    sub.add_argument(
        "--output",
        metavar="FILE",
        default=str(default_model_path()),
        help="Where to save the model (default: %(default)s)",
    )
    sub.add_argument("--save-data", metavar="FILE", help="Also save the collected data")
    sub.add_argument("--test-split", type=float, default=0.05, help="Fraction used for testing")
    sub.add_argument(
        "--eval-sample", type=int, default=500, help="How many random test samples to evaluate"
    )
    sub.add_argument("--seed", type=int, default=42, help="Random seed")
    sub.add_argument("-v", "--verbose", action="store_true")
    opts = sub.parse_args(argv)

    setup_logging(opts.verbose, False)

    from vibebuild import training

    try:
        if opts.data:
            data = training.load_training_data(opts.data)
        else:
            data = training.collect(opts.release, opts.arch)
            if opts.save_data:
                Path(opts.save_data).write_text(json.dumps(data), encoding="utf-8")
        metrics = training.train(
            data,
            opts.output,
            test_split=opts.test_split,
            eval_sample=opts.eval_sample,
            seed=opts.seed,
        )
    except (VibeBuildError, OSError) as exc:
        logging.error("Training failed: %s", exc)
        return 1

    print(f"Train: {metrics['train_size']}, Test: {metrics['test_size']}")
    if "predicted" in metrics:
        print(f"Coverage: {metrics['coverage'] * 100:.1f}%")
        print(f"RPM accuracy: {metrics['rpm_accuracy'] * 100:.1f}%")
        print(f"SRPM accuracy: {metrics['srpm_accuracy'] * 100:.1f}%")
    print(f"Model: {opts.output}")
    return 0


def main(args: list[str] | None = None) -> int:
    """Main entry point.

    Positional arguments:
      vibebuild SRPM          -- target is read from ~/.koji/config [koji]
      vibebuild TARGET SRPM   -- explicit target
      vibebuild train [...]   -- train the ML resolver
    """
    argv = list(args) if args is not None else sys.argv[1:]

    # `train` is dispatched before parsing, so it does not clash with TARGET/SRPM
    if argv and argv[0] == "train":
        return _train_subcommand(argv[1:])

    parser = create_parser()
    opts = parser.parse_args(argv)

    setup_logging(opts.verbose, opts.quiet, json_logs=(opts.log_format == "json"))

    if opts.no_ssl_verify:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # With one positional argument it is the SRPM and the target comes from the config
    if opts.srpm:
        target, srpm_arg = opts.target, opts.srpm
    else:
        target, srpm_arg = load_koji_config()["target"], opts.target
    release = fedora_release(opts, target)

    if opts.download_only:
        if not srpm_arg:
            parser.error("--download-only requires package name")
        return cmd_download(opts, srpm_arg, release)

    if opts.analyze_only:
        if not srpm_arg:
            parser.error("--analyze-only requires SRPM path or package name")
        if not opts.build_tag and not target:
            parser.error(
                "--analyze-only needs a build tag: pass --build-tag, a target "
                "(vibebuild --analyze-only TARGET SRPM) or 'target' in ~/.koji/config"
            )
    else:
        if not srpm_arg:
            parser.error("SRPM (or package name) is required for building")
        if not target:
            parser.error(
                "Build target not specified.\n"
                "  Option 1: vibebuild TARGET PACKAGE\n"
                "  Option 2: Add 'target = f44' to ~/.koji/config under [koji]"
            )

    try:
        srpm_path = ensure_srpm_path(opts, srpm_arg, release)
    except Exception as e:
        logging.error("Failed to obtain SRPM: %s", e)
        return 1

    if opts.analyze_only:
        return cmd_analyze(opts, srpm_path, target)
    return cmd_build(opts, target, srpm_path, release)
