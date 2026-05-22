#!/usr/bin/env python3
"""
Wide-scale read-only validation of vibebuild on a representative sample of Fedora packages.

Does no submit/build, only:
  1) download the SRPM via vibebuild.fetcher (through the real Fedora Koji),
  2) analyze via get_package_info_from_srpm (analyzer),
  3) for each BR check that name_resolver/ML can resolve it meaningfully.

Goal: make sure vibebuild does not fail across the breadth of the catalog; collect statistics
on success and a list of packages with problems.

Usage:
    python3 scripts/prod_validation.py [--output report.json] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger("prod_validation")

# Curated list of 100 packages of various categories.
# The goal is to cover the main families of BR resolution.
PACKAGES = [
    # trivial
    "hello",
    "tree",
    "jq",
    # python: a wide stack
    "python-six",
    "python-attrs",
    "python-requests",
    "python-click",
    "python-jinja2",
    "python-pyyaml",
    "python-flask",
    "python-django",
    "python-pytest",
    "python-pip",
    "python-setuptools",
    "python-wheel",
    "python-virtualenv",
    "python-tox",
    "python-toml",
    "python-typing-extensions",
    "python-charset-normalizer",
    "python-urllib3",
    "python-idna",
    "python-certifi",
    "python-iso8601",
    "python-pretend",
    "python-hypothesis",
    "python-trustme",
    # python C-extensions
    "python-cryptography",
    "python-cffi",
    "python-numpy",
    "python-scipy",
    "python-pillow",
    "python-lxml",
    "python-psutil",
    "python-psycopg2",
    "python-pyzmq",
    "python-pycurl",
    # perl
    "perl",
    "perl-LWP-Protocol-https",
    "perl-Test-Simple",
    "perl-DBI",
    "perl-JSON",
    "perl-Module-Build",
    "perl-File-Path",
    "perl-Encode",
    # ruby
    "ruby",
    "rubygem-rake",
    "rubygem-rspec",
    "rubygem-bundler",
    # nodejs
    "nodejs",
    "nodejs-express",
    # system libraries
    "openssl",
    "glibc",
    "zlib",
    "libxml2",
    "libxslt",
    "libpng",
    "libjpeg-turbo",
    "ncurses",
    "readline",
    "sqlite",
    "pcre2",
    "expat",
    "curl",
    # build/dev tools
    "gcc",
    "make",
    "cmake",
    "meson",
    "ninja-build",
    "autoconf",
    "automake",
    "libtool",
    "pkgconf",
    "rust",
    "cargo",
    "golang",
    # servers
    "httpd",
    "nginx",
    "postgresql",
    "mariadb",
    "redis",
    "memcached",
    # tools
    "git",
    "vim",
    "tmux",
    "htop",
    "tar",
    "gzip",
    "bzip2",
    "xz",
    "rsync",
    "openssh",
    "sudo",
    "systemd",
    # graphics/multimedia (often exotic BR)
    "ImageMagick",
    "ffmpeg-free",
    "gstreamer1",
    "cairo",
    "pango",
    "gtk3",
    # background infrastructure
    "dbus",
    "polkit",
    "NetworkManager",
    "iptables",
    # exotic
    "texlive-base",
    "ghostscript",
    "graphviz",
    "doxygen",
]


def _import_vibebuild():
    """Import vibebuild (inside the container or in editable mode)."""
    try:
        from vibebuild.analyzer import get_package_info_from_srpm
        from vibebuild.fetcher import SRPMFetcher
        from vibebuild.name_resolver import PackageNameResolver

        try:
            from vibebuild.ml_resolver import MLPackageResolver
        except ImportError:
            MLPackageResolver = None  # type: ignore

        return get_package_info_from_srpm, SRPMFetcher, PackageNameResolver, MLPackageResolver
    except ImportError as exc:
        print(f"FATAL: failed to import vibebuild: {exc}", file=sys.stderr)
        sys.exit(2)


def validate_package(
    name: str,
    fetcher,
    name_resolver,
    ml_resolver,
    get_package_info,
    work_dir: Path,
) -> dict:
    """
    Run the full read-only cycle for a single package. Returns a dict with the result.
    """
    result: dict = {
        "package": name,
        "stage": "init",
        "success": False,
        "duration": 0.0,
        "error": None,
        "build_requires_count": 0,
        "changed_by_rules": 0,  # name_resolver returned a different name (virtual provide/macro)
        "changed_by_ml": 0,  # ML returned a different name
        "passthrough": 0,  # name left as is (often basic ones: gcc, make, glibc)
    }
    t0 = time.monotonic()
    try:
        # 1) Download the SRPM
        result["stage"] = "fetch"
        srpm_path = fetcher.download_srpm(name)
        if not srpm_path or not Path(srpm_path).exists():
            raise RuntimeError(f"SRPM not downloaded for {name}")

        # 2) Analysis
        result["stage"] = "analyze"
        pkg_info = get_package_info(srpm_path)
        result["nvr"] = pkg_info.nvr
        result["build_requires_count"] = len(pkg_info.build_requires)

        # 3) Resolve each BR
        result["stage"] = "resolve"
        rules_count = 0
        ml_count = 0
        passthrough = 0
        for br in pkg_info.build_requires:
            br_name = br.name
            resolved = name_resolver.resolve(br_name)
            if resolved and resolved != br_name:
                rules_count += 1
                continue
            # the rule left the same name; ML optionally suggests an alternative
            if ml_resolver and ml_resolver.is_available():
                pred = ml_resolver.predict(br_name)
                if pred and pred.get("rpm_name") and pred["rpm_name"] != br_name:
                    ml_count += 1
                    continue
            passthrough += 1
        result["changed_by_rules"] = rules_count
        result["changed_by_ml"] = ml_count
        result["passthrough"] = passthrough

        result["success"] = True
        result["stage"] = "done"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    finally:
        result["duration"] = round(time.monotonic() - t0, 3)
        # Clean up the downloaded SRPM so as not to clutter the disk
        for f in work_dir.glob("*.src.rpm"):
            try:
                f.unlink()
            except OSError:
                pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Wide-scale read-only validation")
    parser.add_argument(
        "--output", default="prod_validation_report.json", help="Where to save the JSON report"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit the number of packages (for a quick check)"
    )
    parser.add_argument("--release", default="rawhide", help="Fedora release (rawhide or f42)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)
        logger.setLevel(logging.INFO)

    get_package_info, SRPMFetcher, PackageNameResolver, MLPackageResolver = _import_vibebuild()

    work_dir = Path("/tmp/vb-prod-validation")
    work_dir.mkdir(parents=True, exist_ok=True)

    name_resolver = PackageNameResolver()
    fetcher = SRPMFetcher(
        download_dir=str(work_dir),
        fedora_release=args.release,
        name_resolver=name_resolver,
    )
    ml_resolver = None
    if MLPackageResolver is not None:
        try:
            ml_resolver = MLPackageResolver()
            if not ml_resolver.is_available():
                ml_resolver = None
        except Exception as exc:
            logger.warning("ML disabled: %s", exc)
            ml_resolver = None

    packages = PACKAGES[: args.limit] if args.limit else PACKAGES
    print(f"Running {len(packages)} packages through release={args.release}", file=sys.stderr)
    print(f"ML available: {ml_resolver is not None}", file=sys.stderr)

    results = []
    for i, pkg in enumerate(packages, 1):
        print(f"[{i:3d}/{len(packages)}] {pkg}...", file=sys.stderr, end=" ", flush=True)
        r = validate_package(
            pkg,
            fetcher,
            name_resolver,
            ml_resolver,
            get_package_info,
            work_dir,
        )
        results.append(r)
        if r["success"]:
            print(
                f"OK ({r['duration']}s, BR={r['build_requires_count']}, "
                f"rule={r['changed_by_rules']}, "
                f"ml={r['changed_by_ml']}, "
                f"pass={r['passthrough']})",
                file=sys.stderr,
            )
        else:
            print(f"FAIL @ {r['stage']}: {r['error']}", file=sys.stderr)

    # Summary
    n_total = len(results)
    n_ok = sum(1 for r in results if r["success"])
    n_fetch_fail = sum(1 for r in results if r["stage"] == "fetch" and not r["success"])
    n_analyze_fail = sum(1 for r in results if r["stage"] == "analyze" and not r["success"])
    n_resolve_fail = sum(1 for r in results if r["stage"] == "resolve" and not r["success"])
    total_br = sum(r["build_requires_count"] for r in results if r["success"])
    total_rule = sum(r["changed_by_rules"] for r in results if r["success"])
    total_ml = sum(r["changed_by_ml"] for r in results if r["success"])
    total_passthrough = sum(r["passthrough"] for r in results if r["success"])

    summary = {
        "total": n_total,
        "success": n_ok,
        "success_rate": round(n_ok / n_total * 100, 1) if n_total else 0,
        "fail_at_fetch": n_fetch_fail,
        "fail_at_analyze": n_analyze_fail,
        "fail_at_resolve": n_resolve_fail,
        "total_build_requires": total_br,
        "changed_by_rules": total_rule,
        "changed_by_ml": total_ml,
        "passthrough": total_passthrough,
        "ml_share_pct": round(total_ml / max(total_br, 1) * 100, 1),
        "rule_share_pct": round(total_rule / max(total_br, 1) * 100, 1),
        "passthrough_share_pct": round(total_passthrough / max(total_br, 1) * 100, 1),
    }

    report = {"summary": summary, "results": results}
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print("", file=sys.stderr)
    print(json.dumps(summary, indent=2), file=sys.stderr)
    print(f"\nReport: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
