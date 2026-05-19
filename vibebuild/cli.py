#!/usr/bin/env python3
"""
VibeBuild CLI - Koji extension for automatic dependency resolution.

Usage:
    vibebuild [OPTIONS] SRPM
    vibebuild [OPTIONS] TARGET SRPM

    SRPM can be a path to a .src.rpm file or a package name (e.g. python3).
    If a package name is given, the SRPM is downloaded from Koji and then built.
    When TARGET is omitted, it is read from 'target' key in ~/.koji/config [koji].

Examples:
    vibebuild python-requests
    vibebuild fedora-target python3
    vibebuild fedora-target my-package.src.rpm
    vibebuild --scratch fedora-target python-requests
    vibebuild --server https://my-koji/kojihub fedora-target pkg.src.rpm
"""

import argparse
import configparser
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from vibebuild import __version__
from vibebuild.analyzer import get_package_info_from_srpm
from vibebuild.builder import BuildResult, BuildStatus, KojiBuilder
from vibebuild.exceptions import VibeBuildError
from vibebuild.fetcher import SRPMFetcher
from vibebuild.name_resolver import PackageNameResolver
from vibebuild.resolver import DependencyResolver, KojiClient


def load_koji_config() -> dict[str, Optional[str]]:
    """Load server, weburl, cert, serverca, target from ~/.koji/config and /etc/koji.conf."""
    out: dict[str, Optional[str]] = {
        "server": None,
        "web_url": None,
        "cert": None,
        "serverca": None,
        "target": None,
        "build_tag": None,
    }
    config = configparser.ConfigParser()
    for path in [
        Path.home() / ".koji" / "config",
        Path("/etc/koji.conf"),
    ]:
        if not path.exists():
            continue
        try:
            config.read(path, encoding="utf-8")
            if config.has_section("koji"):
                s = config["koji"]
                if s.get("server") and not out["server"]:
                    out["server"] = s["server"].strip()
                if s.get("weburl") and not out["web_url"]:
                    out["web_url"] = s["weburl"].strip()
                if s.get("cert") and not out["cert"]:
                    out["cert"] = os.path.expanduser(s["cert"].strip())
                if s.get("serverca") and not out["serverca"]:
                    out["serverca"] = os.path.expanduser(s["serverca"].strip())
                if s.get("target") and not out["target"]:
                    out["target"] = s["target"].strip()
                if s.get("build_tag") and not out["build_tag"]:
                    out["build_tag"] = s["build_tag"].strip()
        except (configparser.Error, OSError):
            pass
    return out


def create_name_resolver(
    no_ml: bool = False, ml_model_path: Optional[str] = None
) -> PackageNameResolver:
    """Create a PackageNameResolver with optional ML fallback."""
    ml_resolver = None
    if not no_ml:
        try:
            from vibebuild.ml_resolver import MLPackageResolver

            ml_resolver = MLPackageResolver(model_path=ml_model_path)
            if not ml_resolver.is_available():
                ml_resolver = None
        except ImportError:
            ml_resolver = None
    return PackageNameResolver(ml_resolver=ml_resolver)


def setup_logging(
    verbose: bool = False,
    quiet: bool = False,
    json_logs: bool = False,
) -> None:
    """Configure logging based on verbosity.

    json_logs=True switches to the JSON format for prod parsing.
    """
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
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
            )
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class _HelpAllArgumentParser(argparse.ArgumentParser):
    """Parser that shows short help by default and full help with --help-all."""

    def format_help(self) -> str:
        if "--help-all" in sys.argv:
            return super().format_help()
        short = (
            f"{self.description}\n\n"
            "usage: vibebuild [OPTIONS] SRPM\n"
            "       vibebuild [OPTIONS] TARGET SRPM\n\n"
            "  SRPM    Path to .src.rpm or package name (e.g. python-requests);\n"
            "          if a name, SRPM is downloaded from Fedora then built.\n"
            "  TARGET  Build target (e.g. fedora-target).\n"
            "          If omitted, read from 'target' in ~/.koji/config [koji].\n\n"
            "Examples:\n"
            "  vibebuild python-requests\n"
            "  vibebuild fedora-target python-requests\n"
            "  vibebuild --scratch fedora-target my-pkg.src.rpm\n\n"
            "Modes:\n"
            "  --analyze-only     Only analyze dependencies, do not build\n"
            "  --download-only    Only download SRPM, do not build\n"
            "  --dry-run          Show what would be built without actually building\n\n"
            "Common options:\n"
            "  -v, --verbose      Enable verbose output\n"
            "  -q, --quiet        Suppress non-error output\n"
            "  --scratch          Perform scratch build (not tagged)\n"
            "  --no-deps          Skip dependency resolution, just build the package\n"
            "  --server URL       Koji hub URL (default: from ~/.koji/config or Fedora Koji)\n\n"
            "Full list of options: vibebuild --help-all\n"
        )
        return short


def detect_fedora_release(target: str) -> Optional[str]:
    """Auto-detect Fedora release from build target name (e.g. 'f42' -> '42')."""
    import re

    m = re.match(r"^f(\d+)$", target)
    if m:
        return m.group(1)
    return None


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    koji_cfg = load_koji_config()
    default_server = koji_cfg.get("server") or "https://koji.fedoraproject.org/kojihub"
    default_web_url = koji_cfg.get("web_url") or "https://koji.fedoraproject.org/koji"

    parser = _HelpAllArgumentParser(
        prog="vibebuild",
        description="Koji build with automatic dependency resolution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build package (target from ~/.koji/config)
  vibebuild python-requests

  # Build with explicit target
  vibebuild fedora-target my-package-1.0-1.fc40.src.rpm

  # Scratch build (not tagged)
  vibebuild --scratch fedora-target my-package.src.rpm

  # Use custom Koji server
  vibebuild --server https://koji.internal/kojihub fedora-target pkg.src.rpm

  # Analyze dependencies without building
  vibebuild --analyze-only my-package.src.rpm

  # Download SRPM from Fedora
  vibebuild --download-only python-requests
""",
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--help-all",
        action="store_true",
        help="Show all options (default help shows only common ones)",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress non-error output")

    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help="Log format: text (default) or json for prod parsers",
    )

    koji_group = parser.add_argument_group("Koji options")

    koji_group.add_argument(
        "--server",
        metavar="URL",
        default=default_server,
        help="Koji hub URL (default: from ~/.koji/config or Fedora Koji)",
    )

    koji_group.add_argument(
        "--web-url",
        metavar="URL",
        default=default_web_url,
        help="Koji web URL (default: from ~/.koji/config or Fedora)",
    )

    koji_group.add_argument(
        "--cert",
        metavar="FILE",
        default=koji_cfg.get("cert"),
        help="Client certificate for authentication (default: from ~/.koji/config)",
    )

    koji_group.add_argument(
        "--serverca",
        metavar="FILE",
        default=koji_cfg.get("serverca"),
        help="CA certificate for server verification (default: from ~/.koji/config)",
    )

    koji_group.add_argument(
        "--build-tag",
        metavar="TAG",
        default=koji_cfg.get("build_tag") or "fedora-build",
        help="Build tag for dependency checking (default: from ~/.koji/config or fedora-build)",
    )

    koji_group.add_argument(
        "--no-ssl-verify",
        action="store_true",
        help="Disable SSL certificate verification (insecure)",
    )

    build_group = parser.add_argument_group("Build options")

    build_group.add_argument(
        "--scratch", action="store_true", help="Perform scratch build (not tagged)"
    )

    build_group.add_argument(
        "--nowait", action="store_true", help="Do not wait for builds to complete"
    )

    build_group.add_argument(
        "--no-deps", action="store_true", help="Skip dependency resolution, just build the package"
    )

    build_group.add_argument(
        "--force", action="store_true",
        help="Rebuild the package even if an identical NVR is already in the target tag",
    )
    build_group.add_argument(
        "--no-idempotency", action="store_true",
        help="Disable the idempotency pre-check (by default vibebuild skips already built packages)",
    )

    build_group.add_argument("--download-dir", metavar="DIR", help="Directory for downloaded SRPMs")

    build_group.add_argument(
        "--no-name-resolution",
        action="store_true",
        help="Disable package name normalization (macros, virtual provides)",
    )

    build_group.add_argument(
        "--no-ml", action="store_true", help="Disable ML-based package name resolution"
    )

    build_group.add_argument(
        "--ml-model", metavar="PATH", help="Path to ML model file (default: built-in)"
    )

    build_group.add_argument(
        "--fedora-release",
        metavar="VER",
        help="Fedora release to fetch SRPMs from (e.g. 42). Default: auto-detect from target, fallback to rawhide",
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

    parser.add_argument("target", nargs="?", help="Build target (e.g., fedora-target)")

    parser.add_argument(
        "srpm",
        nargs="?",
        help="Path to .src.rpm file or package name (e.g. python3); if name, SRPM is downloaded then built",
    )

    return parser


def print_build_result(result: BuildResult) -> None:
    """Print build result summary."""
    print("\n" + "=" * 60)
    print("BUILD SUMMARY")
    print("=" * 60)

    if result.success:
        print("Status: SUCCESS ✓")
    else:
        print("Status: FAILED ✗")

    print(f"Total time: {result.total_time:.1f} seconds")
    print(f"Packages built: {len(result.built_packages)}")
    print(f"Packages failed: {len(result.failed_packages)}")

    if result.built_packages:
        print("\nSuccessfully built:")
        for pkg in result.built_packages:
            print(f"  ✓ {pkg}")

    if result.failed_packages:
        print("\nFailed packages:")
        for pkg in result.failed_packages:
            print(f"  ✗ {pkg}")

    if result.tasks:
        print("\nBuild tasks:")
        for task in result.tasks:
            status_icon = {
                BuildStatus.COMPLETE: "✓",
                BuildStatus.FAILED: "✗",
                BuildStatus.BUILDING: "⏳",
                BuildStatus.PENDING: "○",
                BuildStatus.CANCELED: "⊘",
            }.get(task.status, "?")

            print(f"  {status_icon} {task.package_name}: {task.status.value}")
            if task.task_id:
                print(f"      Task ID: {task.task_id}")
            if task.error_message:
                print(f"      Error: {task.error_message[:100]}")

    print("=" * 60)


def cmd_analyze(
    srpm_path: str,
    server: str,
    build_tag: str,
    cert: Optional[str],
    serverca: Optional[str],
    no_ssl_verify: bool = False,
) -> int:
    """Analyze SRPM dependencies."""
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
            server=server, cert=cert, serverca=serverca, no_ssl_verify=no_ssl_verify
        )
        resolver = DependencyResolver(koji_client=koji_client, koji_tag=build_tag)

        missing = resolver.find_missing_deps([r.name for r in package_info.build_requires])

        if missing:
            print(f"\nMissing dependencies ({len(missing)}):")
            for dep in missing:
                print(f"  ✗ {dep}")
        else:
            print("\n✓ All dependencies available")

        return 0

    except Exception as e:
        logging.error(f"Analysis failed: {e}")
        return 1


def ensure_srpm_path(
    srpm_arg: str,
    download_dir: Optional[str],
    no_ssl_verify: bool,
    no_ml: bool,
    ml_model_path: Optional[str],
    fedora_release: str = "rawhide",
) -> str:
    """
    Return path to an SRPM file. If srpm_arg is an existing path, return it.
    Otherwise treat as package name, download SRPM, and return its path.
    """
    p = Path(srpm_arg)
    if p.exists():
        return str(p.resolve())
    # Package name: download first
    logging.info("Downloading SRPM for: %s", srpm_arg)
    name_resolver = create_name_resolver(no_ml=no_ml, ml_model_path=ml_model_path)
    fetcher = SRPMFetcher(
        download_dir=download_dir,
        fedora_release=fedora_release,
        no_ssl_verify=no_ssl_verify,
        name_resolver=name_resolver,
    )
    path = fetcher.download_srpm(srpm_arg)
    logging.info("Downloaded: %s", path)
    return path


def cmd_download(
    package_name: str,
    download_dir: Optional[str],
    no_ssl_verify: bool = False,
    no_ml: bool = False,
    ml_model_path: Optional[str] = None,
    fedora_release: str = "rawhide",
) -> int:
    """Download SRPM from Fedora."""
    print(f"Downloading SRPM for: {package_name}")

    try:
        name_resolver = create_name_resolver(no_ml=no_ml, ml_model_path=ml_model_path)
        fetcher = SRPMFetcher(
            download_dir=download_dir,
            fedora_release=fedora_release,
            no_ssl_verify=no_ssl_verify,
            name_resolver=name_resolver,
        )
        srpm_path = fetcher.download_srpm(package_name)

        print(f"✓ Downloaded: {srpm_path}")
        return 0

    except Exception as e:
        logging.error(f"Download failed: {e}")
        return 1


def cmd_build(
    target: str,
    srpm_path: str,
    server: str,
    web_url: str,
    cert: Optional[str],
    serverca: Optional[str],
    build_tag: str,
    scratch: bool,
    nowait: bool,
    no_deps: bool,
    download_dir: Optional[str],
    dry_run: bool,
    no_ssl_verify: bool = False,
    no_name_resolution: bool = False,
    no_ml: bool = False,
    ml_model_path: Optional[str] = None,
    fedora_release: str = "rawhide",
    force: bool = False,
    no_idempotency: bool = False,
) -> int:
    """Build package with dependency resolution."""
    srpm = Path(srpm_path)
    if not srpm.exists():
        logging.error(f"SRPM not found: {srpm_path}")
        return 1

    builder = KojiBuilder(
        koji_server=server,
        koji_web_url=web_url,
        cert=cert,
        serverca=serverca,
        target=target,
        build_tag=build_tag,
        scratch=scratch,
        nowait=nowait,
        download_dir=download_dir,
        no_ssl_verify=no_ssl_verify,
        no_name_resolution=no_name_resolution,
        no_ml=no_ml,
        ml_model_path=ml_model_path,
        fedora_release=fedora_release,
        force=force,
        idempotent=not no_idempotency,
    )

    if dry_run:
        print("DRY RUN - showing what would be built:\n")

        package_info = get_package_info_from_srpm(str(srpm))
        print(f"Target package: {package_info.nvr}")

        if not no_deps:

            def srpm_resolver(pkg: str) -> Optional[str]:
                try:
                    return builder.fetcher.download_srpm(pkg)
                except Exception:
                    return None

            builder.resolver.build_dependency_graph(
                package_info.name, str(srpm), srpm_resolver=srpm_resolver
            )

            build_chain = builder.resolver.get_build_chain()

            if build_chain:
                print(f"\nBuild order ({sum(len(lvl) for lvl in build_chain)} packages):")
                for level_idx, level in enumerate(build_chain):
                    print(f"  Level {level_idx + 1}: {', '.join(level)}")
            else:
                print("\nNo additional dependencies to build")

        return 0

    try:
        if no_deps:
            task = builder.build_package(str(srpm), wait=not nowait)
            result = BuildResult(
                success=task.status == BuildStatus.COMPLETE,
                tasks=[task],
                built_packages=[task.package_name] if task.status == BuildStatus.COMPLETE else [],
                failed_packages=[task.package_name] if task.status != BuildStatus.COMPLETE else [],
            )
        else:
            result = builder.build_with_deps(str(srpm))

        print_build_result(result)

        return 0 if result.success else 1

    except VibeBuildError as e:
        logging.error(f"Build failed: {e}")
        return 1
    except Exception as e:
        logging.exception(f"Unexpected error: {e}")
        return 1


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point.

    Positional argument resolution:
      vibebuild SRPM          -- target is read from ~/.koji/config [koji]
      vibebuild TARGET SRPM   -- explicit target (backwards-compatible)
    """
    parser = create_parser()
    opts = parser.parse_args(args)

    if getattr(opts, "help_all", False):
        print(parser.format_help())
        return 0

    setup_logging(opts.verbose, opts.quiet, json_logs=(opts.log_format == "json"))

    if opts.no_ssl_verify:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if opts.analyze_only:
        srpm_arg = opts.srpm or opts.target
        if not srpm_arg:
            parser.error("--analyze-only requires SRPM path or package name")

        # Determine fedora_release for potential download
        analyze_release = getattr(opts, "fedora_release", None) or "rawhide"

        try:
            srpm_path = ensure_srpm_path(
                srpm_arg,
                opts.download_dir,
                opts.no_ssl_verify,
                getattr(opts, "no_ml", False),
                getattr(opts, "ml_model", None),
                fedora_release=analyze_release,
            )
        except Exception as e:
            logging.error("Failed to obtain SRPM: %s", e)
            return 1

        return cmd_analyze(
            srpm_path, opts.server, opts.build_tag, opts.cert, opts.serverca, opts.no_ssl_verify
        )

    if opts.download_only:
        package_name = opts.srpm or opts.target
        if not package_name:
            parser.error("--download-only requires package name")
        dl_release = getattr(opts, "fedora_release", None) or "rawhide"
        return cmd_download(
            package_name,
            opts.download_dir,
            opts.no_ssl_verify,
            no_ml=getattr(opts, "no_ml", False),
            ml_model_path=getattr(opts, "ml_model", None),
            fedora_release=dl_release,
        )

    # Resolve target and srpm from positional arguments:
    #   vibebuild SRPM          -> target from ~/.koji/config
    #   vibebuild TARGET SRPM   -> explicit target (backwards-compatible)
    koji_cfg = load_koji_config()
    if opts.target and opts.srpm:
        target = opts.target
        srpm_arg = opts.srpm
    elif opts.target and not opts.srpm:
        srpm_arg = opts.target
        target = koji_cfg.get("target")
    else:
        target = None
        srpm_arg = None

    if not srpm_arg:
        parser.error("SRPM (or package name) is required for building")

    if not target:
        parser.error(
            "Build target not specified.\n"
            "  Option 1: vibebuild TARGET PACKAGE\n"
            "  Option 2: Add 'target = fedora-target' to ~/.koji/config under [koji]"
        )

    # Determine fedora_release: explicit flag > auto-detect from target > rawhide
    fedora_release = getattr(opts, "fedora_release", None)
    if not fedora_release:
        fedora_release = detect_fedora_release(target)
    if not fedora_release:
        fedora_release = "rawhide"

    try:
        srpm_path = ensure_srpm_path(
            srpm_arg,
            opts.download_dir,
            opts.no_ssl_verify,
            getattr(opts, "no_ml", False),
            getattr(opts, "ml_model", None),
            fedora_release=fedora_release,
        )
    except Exception as e:
        logging.error("Download failed: %s", e)
        return 1

    return cmd_build(
        target=target,
        srpm_path=srpm_path,
        server=opts.server,
        web_url=opts.web_url,
        cert=opts.cert,
        serverca=opts.serverca,
        build_tag=opts.build_tag,
        scratch=opts.scratch,
        nowait=opts.nowait,
        no_deps=opts.no_deps,
        download_dir=opts.download_dir,
        dry_run=opts.dry_run,
        no_ssl_verify=opts.no_ssl_verify,
        no_name_resolution=getattr(opts, "no_name_resolution", False),
        no_ml=getattr(opts, "no_ml", False),
        ml_model_path=getattr(opts, "ml_model", None),
        fedora_release=fedora_release,
        force=getattr(opts, "force", False),
        no_idempotency=getattr(opts, "no_idempotency", False),
    )


if __name__ == "__main__":
    sys.exit(main())
