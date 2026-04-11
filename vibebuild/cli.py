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


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure logging based on verbosity."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )


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


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point."""
    parser = create_parser()
    opts = parser.parse_args(args)
    setup_logging(opts.verbose, opts.quiet)
    parser.error("not yet implemented")
    return 1


if __name__ == "__main__":
    sys.exit(main())
