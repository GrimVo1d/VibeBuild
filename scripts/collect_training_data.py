#!/usr/bin/env python3
"""
Collect training data for the ML package name resolver.

Gathers provides->package name mappings from Fedora repositories by parsing
repodata primary.xml.gz. Falls back to `dnf repoquery` when available.

Usage:
    python scripts/collect_training_data.py --output training_data.json
    python scripts/collect_training_data.py --output training_data.json --release 40 --arch x86_64
"""

import argparse
import gzip
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# XML namespaces used in primary.xml
RPM_NS = "http://linux.duke.edu/metadata/rpm"
COMMON_NS = "http://linux.duke.edu/metadata/common"
REPO_NS = "http://linux.duke.edu/metadata/repo"
METALINK_NS = "http://www.metalinker.org/"

# Patterns that indicate virtual provides worth mapping
VIRTUAL_PROVIDE_PATTERN = re.compile(
    r"^(python[23]?dist|pkgconfig|perl|rubygem|golang|npm|mvn|osgi|tex|font|cmake)\(.*\)$"
)

# Complex names that are also worth mapping (multi-word, prefixed, etc.)
COMPLEX_NAME_PATTERN = re.compile(
    r"^(python[23]?-|perl-|rubygem-|golang-|nodejs-|php-|R-|ghc-|rust-|ocaml-|texlive-)"
)


def discover_mirror(release: int, arch: str) -> str | None:
    """
    Discover a Fedora mirror URL using the metalink service.

    Args:
        release: Fedora release number (e.g. 40).
        arch: Architecture (e.g. x86_64).

    Returns:
        Base URL of a mirror, or None if discovery fails.
    """
    metalink_url = f"https://mirrors.fedoraproject.org/metalink?repo=fedora-{release}&arch={arch}"
    logger.info("Fetching metalink from %s", metalink_url)

    try:
        req = Request(metalink_url, headers={"User-Agent": "vibebuild/0.1"})
        with urlopen(req, timeout=30) as resp:
            content = resp.read()
    except (URLError, OSError) as e:
        logger.error("Failed to fetch metalink: %s", e)
        return None

    try:
        root = ET.fromstring(content)
        # Try to find URLs in metalink XML.
        # Filter by http(s): metalink also lists rsync:// and ftp:// which urlopen can't handle.
        for url_elem in root.iter(f"{{{METALINK_NS}}}url"):
            url = url_elem.text
            if (
                url
                and url.startswith(("http://", "https://"))
                and url.endswith("/repodata/repomd.xml")
            ):
                base_url = url.rsplit("/repodata/repomd.xml", 1)[0]
                logger.info("Discovered mirror: %s", base_url)
                return base_url

        # Fallback: look for any http URL in resources
        for url_elem in root.iter(f"{{{METALINK_NS}}}url"):
            url = url_elem.text
            if url and url.startswith(("http://", "https://")):
                # Strip trailing path to get base
                if "/repodata/" in url:
                    base_url = url.rsplit("/repodata/", 1)[0]
                else:
                    base_url = url.rstrip("/")
                logger.info("Discovered mirror (fallback): %s", base_url)
                return base_url
    except ET.ParseError as e:
        logger.error("Failed to parse metalink XML: %s", e)

    # Well-known fallback
    fallback = (
        f"https://dl.fedoraproject.org/pub/fedora/linux/releases/{release}/Everything/{arch}/os"
    )
    logger.info("Using fallback mirror: %s", fallback)
    return fallback


def find_primary_xml_url(base_url: str) -> str | None:
    """
    Parse repomd.xml to find the location of primary metadata (primary.xml.gz or primary.xml.zst).

    Args:
        base_url: Base URL of the Fedora repository.

    Returns:
        Full URL to primary.xml.gz or primary.xml.zst, or None if not found.
    """
    repomd_url = f"{base_url}/repodata/repomd.xml"
    logger.info("Fetching repomd.xml from %s", repomd_url)

    try:
        req = Request(repomd_url, headers={"User-Agent": "vibebuild/0.1"})
        with urlopen(req, timeout=30) as resp:
            content = resp.read()
    except (URLError, OSError) as e:
        logger.error("Failed to fetch repomd.xml: %s", e)
        return None

    try:
        root = ET.fromstring(content)
        for data_elem in root.findall(f"{{{REPO_NS}}}data"):
            if data_elem.get("type") == "primary":
                location = data_elem.find(f"{{{REPO_NS}}}location")
                if location is not None:
                    href = location.get("href")
                    if href:
                        primary_url = f"{base_url}/{href}"
                        logger.info("Found primary: %s", primary_url)
                        return primary_url
    except ET.ParseError as e:
        logger.error("Failed to parse repomd.xml: %s", e)

    return None


def _open_primary_stream(tmp_path: str):
    """
    Open downloaded primary file as a decompressed binary stream (gzip or zstd).
    Returns (stream, close_func). Call close_func when done to avoid leaking processes.
    """
    with open(tmp_path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"\x1f\x8b":
        f = gzip.open(tmp_path, "rb")
        return f, f.close
    if magic[:4] == b"(\xb5/\xfd":
        # Zstandard: decompress with zstd -d -c
        proc = subprocess.Popen(
            ["zstd", "-d", "-c", tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if proc.stdout is None:
            raise OSError("zstd produced no stdout")

        def close_zstd():
            proc.stdout.close()
            proc.wait()

        return proc.stdout, close_zstd
    raise ValueError("Unknown compression (not gzip or zstd)")


def download_and_parse_primary(primary_url: str) -> list[dict]:
    """
    Download primary metadata (primary.xml.gz or .zst) and extract provides-to-package mappings.

    Args:
        primary_url: URL to primary.xml.gz or primary.xml.zst file.

    Returns:
        List of dicts with keys "provide", "rpm_name", "srpm_name".
    """
    logger.info("Downloading primary metadata (this may take a while)...")

    with tempfile.NamedTemporaryFile(suffix=".primary", delete=False) as tmp:
        tmp_path = tmp.name
        try:
            req = Request(primary_url, headers={"User-Agent": "vibebuild/0.1"})
            with urlopen(req, timeout=300) as resp:
                shutil.copyfileobj(resp, tmp)
        except (URLError, OSError) as e:
            logger.error("Failed to download primary: %s", e)
            return []

    logger.info("Parsing primary metadata...")
    mappings = []
    stream = None
    close_stream = None

    try:
        stream, close_stream = _open_primary_stream(tmp_path)
        # Use iterparse for memory efficiency on large XML
        context = ET.iterparse(stream, events=("end",))
        for _event, elem in context:
            if elem.tag == f"{{{COMMON_NS}}}package" and elem.get("type") == "rpm":
                rpm_name = _extract_text(elem, f"{{{COMMON_NS}}}name")
                arch = _extract_text(elem, f"{{{COMMON_NS}}}arch")

                # Skip source RPMs in the binary repo listing
                if arch == "src":
                    elem.clear()
                    continue

                # Extract source RPM name
                fmt_elem = elem.find(f"{{{COMMON_NS}}}format")
                srpm_full = ""
                if fmt_elem is not None:
                    srpm_elem = fmt_elem.find(f"{{{RPM_NS}}}sourcerpm")
                    if srpm_elem is not None and srpm_elem.text:
                        srpm_full = srpm_elem.text

                srpm_name = _parse_srpm_name(srpm_full) if srpm_full else rpm_name

                # Extract provides
                if fmt_elem is not None:
                    provides_elem = fmt_elem.find(f"{{{RPM_NS}}}provides")
                    if provides_elem is not None:
                        for entry in provides_elem.findall(f"{{{RPM_NS}}}entry"):
                            provide_name = entry.get("name", "")
                            if _is_interesting_provide(provide_name, rpm_name):
                                mappings.append(
                                    {
                                        "provide": provide_name,
                                        "rpm_name": rpm_name,
                                        "srpm_name": srpm_name,
                                    }
                                )

                # Free memory
                elem.clear()
    except (ValueError, gzip.BadGzipFile, ET.ParseError, OSError) as e:
        logger.error("Failed to parse primary: %s", e)
    finally:
        if close_stream is not None:
            close_stream()
        Path(tmp_path).unlink(missing_ok=True)

    logger.info("Extracted %d provide mappings", len(mappings))
    return mappings


def _extract_text(parent: ET.Element, tag: str) -> str:
    """Extract text content from a child element, or empty string."""
    elem = parent.find(tag)
    if elem is not None and elem.text:
        return elem.text
    return ""


def _parse_srpm_name(srpm_string: str) -> str:
    """
    Extract the package name from a source RPM filename.

    Example: "python-requests-2.31.0-1.fc40.src.rpm" -> "python-requests"
    """
    # Remove .src.rpm suffix
    name = srpm_string
    if name.endswith(".src.rpm"):
        name = name[:-8]

    # Split off version-release by finding the last two dashes
    # that precede version-like segments
    parts = name.rsplit("-", 2)
    if len(parts) >= 3:
        return parts[0]
    elif len(parts) == 2:
        # Check if last part looks like a version
        if parts[1] and parts[1][0].isdigit():
            return parts[0]
    return name


def _is_interesting_provide(provide_name: str, rpm_name: str) -> bool:
    """
    Determine if a provide string is worth including in training data.

    We want virtual provides (python3dist(...), pkgconfig(...), etc.) and
    complex package names that differ from the RPM name itself.
    """
    if not provide_name:
        return False

    # Skip filesystem provides
    if provide_name.startswith("/"):
        return False

    # Skip bare library sonames (libfoo.so.1)
    if provide_name.startswith("lib") and ".so" in provide_name:
        return False

    # Include virtual provides with parentheses
    if VIRTUAL_PROVIDE_PATTERN.match(provide_name):
        return True

    # Include complex prefixed names that differ from the RPM name
    if COMPLEX_NAME_PATTERN.match(provide_name) and provide_name != rpm_name:
        return True

    # Include any provide with parentheses (other virtual provides)
    if "(" in provide_name and ")" in provide_name:
        return True

    return False


def collect_via_dnf(release: int, arch: str) -> list[dict]:
    """
    Fallback: collect provides using dnf repoquery.

    Args:
        release: Fedora release number.
        arch: Architecture.

    Returns:
        List of dicts with keys "provide", "rpm_name", "srpm_name".
    """
    if not shutil.which("dnf"):
        logger.warning("dnf not found, cannot use dnf repoquery fallback")
        return []

    logger.info("Collecting data via dnf repoquery (release=%d, arch=%s)...", release, arch)

    try:
        # Get all provides
        result = subprocess.run(
            # dnf5 disallows combining --provides with --queryformat;
            # %{provides} inside the format string already yields the data.
            [
                "dnf",
                "repoquery",
                f"--releasever={release}",
                f"--forcearch={arch}",
                "--queryformat",
                "%{name}|%{sourcerpm}|%{provides}",
                "*",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error("dnf repoquery failed: %s", e)
        return []

    if result.returncode != 0:
        logger.error("dnf repoquery returned non-zero: %s", result.stderr[:500])
        return []

    mappings = []
    seen = set()

    # %{provides} expands to one provide per line, so a single package produces
    # one header line `name|sourcerpm|first-provide` followed by continuation lines
    # without delimiters. Track the current package across continuations.
    cur_rpm: str | None = None
    cur_srpm: str | None = None

    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        if "|" in line:
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            cur_rpm = parts[0]
            cur_srpm = _parse_srpm_name(parts[1]) if parts[1] else cur_rpm
            provide_name = parts[2].strip()
        else:
            if cur_rpm is None:
                continue
            provide_name = line

        if not provide_name:
            continue

        if _is_interesting_provide(provide_name, cur_rpm):
            key = (provide_name, cur_rpm)
            if key not in seen:
                seen.add(key)
                mappings.append(
                    {
                        "provide": provide_name,
                        "rpm_name": cur_rpm,
                        "srpm_name": cur_srpm or cur_rpm,
                    }
                )

    logger.info("Collected %d mappings via dnf repoquery", len(mappings))
    return mappings


def deduplicate(mappings: list[dict]) -> list[dict]:
    """Remove exact duplicates (provide, rpm_name), keeping the first one seen.

    SRPM-level aggregation is intentionally not applied: the model relies on
    multiple representations of the same provide in different SRPM subpackages,
    which gives a correct majority when searching for nearest neighbors.
    """
    seen = set()
    unique = []
    for entry in mappings:
        key = (entry["provide"], entry["rpm_name"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def main() -> None:
    """Main entry point for training data collection."""
    parser = argparse.ArgumentParser(
        description="Collect provides-to-package training data from Fedora repos"
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output JSON file path for training data",
    )
    parser.add_argument(
        "--release",
        type=int,
        default=40,
        help="Fedora release number (default: 40)",
    )
    parser.add_argument(
        "--arch",
        type=str,
        default="x86_64",
        help="Architecture (default: x86_64)",
    )
    args = parser.parse_args()

    mappings = []

    # Primary method: download and parse primary.xml.gz
    logger.info("Attempting primary.xml.gz method...")
    base_url = discover_mirror(args.release, args.arch)
    if base_url:
        primary_url = find_primary_xml_url(base_url)
        if primary_url:
            mappings = download_and_parse_primary(primary_url)

    # Fallback: dnf repoquery
    if not mappings:
        logger.info("Primary method yielded no data, trying dnf repoquery fallback...")
        mappings = collect_via_dnf(args.release, args.arch)

    if not mappings:
        logger.error("No training data collected. Check network and try again.")
        sys.exit(1)

    mappings = deduplicate(mappings)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mappings, f, indent=2)

    logger.info("Saved %d mappings to %s", len(mappings), args.output)

    # Print summary statistics
    provide_types = {}
    for entry in mappings:
        prov = entry["provide"]
        if "(" in prov:
            ptype = prov.split("(")[0]
        else:
            ptype = prov.split("-")[0] if "-" in prov else "other"
        provide_types[ptype] = provide_types.get(ptype, 0) + 1

    logger.info("--- Summary ---")
    logger.info("Total mappings: %d", len(mappings))
    logger.info("Unique RPM names: %d", len({e["rpm_name"] for e in mappings}))
    logger.info("Unique SRPM names: %d", len({e["srpm_name"] for e in mappings}))
    logger.info("Provide types:")
    for ptype, count in sorted(provide_types.items(), key=lambda x: -x[1])[:15]:
        logger.info("  %-25s %d", ptype, count)


if __name__ == "__main__":
    main()
