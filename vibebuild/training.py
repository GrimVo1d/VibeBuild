"""
Training data and training for the ML name resolver (`vibebuild train`).

Training data are provide -> (rpm, srpm) pairs from the primary metadata of a
Fedora repository. The chain is verified: the metalink from
mirrors.fedoraproject.org (TLS) gives the sha256 of repomd.xml, repomd.xml gives
the sha256 of the primary metadata, and mirrors are only used over https.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import shutil
import subprocess  # nosec B404
import tempfile
import xml.etree.ElementTree as ET  # nosec B405
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, cast

import requests

from vibebuild import __version__
from vibebuild.exceptions import VibeBuildError
from vibebuild.ml_resolver import MLPackageResolver

logger = logging.getLogger(__name__)

METALINK_URL = "https://mirrors.fedoraproject.org/metalink"
MAX_PRIMARY_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024  # metalink and repomd.xml are tens of kilobytes
MAX_MIRRORS = 5

METALINK_NS = "{http://www.metalinker.org/}"
REPO_NS = "{http://linux.duke.edu/metadata/repo}"
COMMON_NS = "{http://linux.duke.edu/metadata/common}"
RPM_NS = "{http://linux.duke.edu/metadata/rpm}"

# Prefixed provide names worth mapping when they differ from the RPM name
_PROVIDE_PREFIXES = (
    "python2-",
    "python3-",
    "perl-",
    "rubygem-",
    "golang-",
    "nodejs-",
    "php-",
    "R-",
    "ghc-",
    "rust-",
    "ocaml-",
    "texlive-",
)


def _https_only(resp: requests.Response, *args, **kwargs) -> None:
    """Response hook, run for every redirect hop too: never follow a redirect off https."""
    if not resp.url.startswith("https://"):
        raise VibeBuildError(f"Refusing a redirect to plain HTTP: {resp.url}")


_session = requests.Session()
_session.headers["User-Agent"] = f"vibebuild/{__version__}"
_session.hooks["response"].append(_https_only)


def _get(url: str, timeout: int = 60) -> bytes:
    """The body of url, at most MAX_METADATA_BYTES."""
    if not url.startswith("https://"):
        raise VibeBuildError(f"Refusing to download over plain HTTP: {url}")
    content = bytearray()
    try:
        with _session.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(1 << 16):
                content += chunk
                if len(content) > MAX_METADATA_BYTES:
                    raise VibeBuildError(f"{url} is larger than {MAX_METADATA_BYTES} bytes")
    except requests.RequestException as exc:
        raise VibeBuildError(f"Cannot fetch {url}: {exc}") from exc
    return bytes(content)


def repomd_mirrors(release: str, arch: str) -> tuple[str, list[str]]:
    """
    Ask the Fedora metalink for repomd.xml of the Everything repo.

    Returns:
        (sha256 of repomd.xml, https URLs of repomd.xml on mirrors, best first)
    """
    repo = "rawhide" if release == "rawhide" else f"fedora-{release}"
    content = _get(f"{METALINK_URL}?repo={repo}&arch={arch}", timeout=30)
    try:
        # Fetched over TLS from Fedora
        root = ET.fromstring(content)  # nosec B314
    except ET.ParseError as exc:
        raise VibeBuildError(f"Cannot parse the metalink for {repo}: {exc}") from exc

    file = root.find(f"{METALINK_NS}files/{METALINK_NS}file[@name='repomd.xml']")
    if file is None:
        raise VibeBuildError(f"The metalink for {repo}/{arch} lists no repomd.xml")
    digest = file.findtext(f"{METALINK_NS}verification/{METALINK_NS}hash[@type='sha256']")
    urls = [
        url.text.strip()
        for url in file.iter(f"{METALINK_NS}url")
        if url.text and url.text.strip().startswith("https://")
    ]
    if not digest or not urls:
        raise VibeBuildError(f"The metalink for {repo}/{arch} has no sha256 or https mirrors")
    return digest.strip(), urls


def fetch_repomd(release: str, arch: str) -> tuple[str, bytes]:
    """repomd.xml checked against the metalink: (repo base URL, repomd.xml bytes)."""
    digest, urls = repomd_mirrors(release, arch)
    for url in urls[:MAX_MIRRORS]:
        try:
            content = _get(url, timeout=30)
        except VibeBuildError as exc:
            logger.info("Mirror failed: %s", exc)
            continue
        if hashlib.sha256(content).hexdigest() != digest:
            logger.info("Mirror %s has a different repomd.xml (stale?), skipping", url)
            continue
        return url.rsplit("/repodata/repomd.xml", 1)[0], content
    raise VibeBuildError(f"No mirror served the expected repomd.xml for {release}/{arch}")


def primary_location(repomd: bytes) -> tuple[str, str]:
    """(href, sha256) of the primary metadata listed in repomd.xml."""
    try:
        # Checked against the metalink sha256
        root = ET.fromstring(repomd)  # nosec B314
    except ET.ParseError as exc:
        raise VibeBuildError(f"Cannot parse repomd.xml: {exc}") from exc
    for data in root.findall(f"{REPO_NS}data"):
        if data.get("type") != "primary":
            continue
        location = data.find(f"{REPO_NS}location")
        checksum = data.find(f"{REPO_NS}checksum")
        href = location.get("href") if location is not None else None
        if href and checksum is not None and checksum.get("type") == "sha256" and checksum.text:
            return href, checksum.text.strip()
    raise VibeBuildError("repomd.xml has no primary metadata with a sha256 checksum")


def download_primary(base_url: str, href: str, digest: str, dest: Path) -> None:
    """Download the primary metadata to dest and check its sha256."""
    url = f"{base_url}/{href}"
    if not url.startswith("https://"):
        raise VibeBuildError(f"Refusing to download over plain HTTP: {url}")
    logger.info("Downloading %s", url)
    sha = hashlib.sha256()
    size = 0
    try:
        with _session.get(url, stream=True, timeout=300) as resp, open(dest, "wb") as out:
            resp.raise_for_status()
            for chunk in resp.iter_content(1 << 20):
                size += len(chunk)
                if size > MAX_PRIMARY_BYTES:
                    raise VibeBuildError(f"{url} is larger than {MAX_PRIMARY_BYTES} bytes")
                sha.update(chunk)
                out.write(chunk)
    except requests.RequestException as exc:
        raise VibeBuildError(f"Cannot download {url}: {exc}") from exc
    if sha.hexdigest() != digest:
        raise VibeBuildError(f"sha256 mismatch for {url}")


@contextmanager
def _decompressed(path: Path) -> Iterator[IO[bytes]]:
    """The primary metadata as a decompressed stream (gzip or zstd)."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"\x1f\x8b":
        with gzip.open(path, "rb") as stream:
            yield cast(IO[bytes], stream)
    elif magic == b"(\xb5/\xfd":
        try:
            from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+
        except ImportError:
            if not shutil.which("zstd"):
                raise VibeBuildError("zstd metadata needs Python 3.14 or the zstd tool") from None
            # List argv without a shell; zstd was found in PATH above
            argv = ["zstd", "-d", "-c", str(path)]
            with subprocess.Popen(argv, stdout=subprocess.PIPE) as proc:  # nosec B603
                stdout = cast(IO[bytes], proc.stdout)  # stdout=PIPE, never None
                yield stdout
                stdout.close()
        else:
            with zstd.open(path, "rb") as stream:
                yield cast(IO[bytes], stream)
    else:
        raise VibeBuildError("Primary metadata is neither gzip nor zstd")


def srpm_name(sourcerpm: str) -> str:
    """Package name of a source RPM file name: python-requests-2.31.0-1.fc40.src.rpm."""
    name = sourcerpm.removesuffix(".src.rpm")
    parts = name.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else name


def is_interesting_provide(provide: str, rpm_name: str) -> bool:
    """Virtual provides (with parentheses) and prefixed names that differ from the RPM."""
    if not provide or provide.startswith("/"):
        return False
    if provide.startswith("lib") and ".so" in provide:
        return False
    if "(" in provide and ")" in provide:
        return True
    return provide.startswith(_PROVIDE_PREFIXES) and provide != rpm_name


def parse_primary(stream: IO[bytes]) -> list[dict]:
    """provide -> rpm/srpm pairs of the binary packages in primary.xml, without duplicates."""
    mappings = []
    seen = set()
    # The primary metadata is checked against the repomd.xml sha256
    for _, elem in ET.iterparse(stream, events=("end",)):  # nosec B314
        if elem.tag != f"{COMMON_NS}package":
            continue
        rpm_name = elem.findtext(f"{COMMON_NS}name") or ""
        fmt = elem.find(f"{COMMON_NS}format")
        is_binary = elem.get("type") == "rpm" and elem.findtext(f"{COMMON_NS}arch") != "src"
        if is_binary and fmt is not None:
            sourcerpm = fmt.findtext(f"{RPM_NS}sourcerpm")
            srpm = srpm_name(sourcerpm) if sourcerpm else rpm_name
            for entry in fmt.iterfind(f"{RPM_NS}provides/{RPM_NS}entry"):
                provide = entry.get("name", "")
                if is_interesting_provide(provide, rpm_name) and (provide, rpm_name) not in seen:
                    seen.add((provide, rpm_name))
                    mappings.append({"provide": provide, "rpm_name": rpm_name, "srpm_name": srpm})
        elem.clear()
    return mappings


def collect(release: str, arch: str) -> list[dict]:
    """Training data from the Everything repository of a Fedora release (or rawhide)."""
    base_url, repomd = fetch_repomd(release, arch)
    href, digest = primary_location(repomd)
    with tempfile.TemporaryDirectory(prefix="vibebuild-train-") as tmp:
        path = Path(tmp) / "primary"
        download_primary(base_url, href, digest, path)
        try:
            with _decompressed(path) as stream:
                mappings = parse_primary(stream)
        except (OSError, EOFError, ET.ParseError) as exc:
            raise VibeBuildError(f"Cannot parse primary metadata: {exc}") from exc
    if not mappings:
        raise VibeBuildError("Primary metadata gave no training data")
    logger.info("Collected %d provide mappings", len(mappings))
    return mappings


def load_training_data(path: str) -> list[dict]:
    """Training data saved by `vibebuild train --save-data`."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VibeBuildError(f"Cannot read training data {path}: {exc}") from exc
    keys = {"provide", "rpm_name", "srpm_name"}
    if not isinstance(data, list) or not all(
        isinstance(e, dict) and keys <= e.keys() for e in data
    ):
        raise VibeBuildError(f"{path} is not a list of {sorted(keys)} records")
    return data


def evaluate(resolver: MLPackageResolver, test_data: list[dict]) -> dict:
    """Coverage and top-1 accuracy of the resolver on held-out records."""
    predicted = correct_rpm = correct_srpm = 0
    for entry in test_data:
        result = resolver.predict(entry["provide"])
        if result is None:
            continue
        predicted += 1
        correct_rpm += result["rpm_name"] == entry["rpm_name"]
        correct_srpm += result["srpm_name"] == entry["srpm_name"]
    total = len(test_data)
    return {
        "predicted": predicted,
        "coverage": predicted / total if total else 0.0,
        "rpm_accuracy": correct_rpm / predicted if predicted else 0.0,
        "srpm_accuracy": correct_srpm / predicted if predicted else 0.0,
    }


def train(
    data: list[dict],
    output: str,
    test_split: float = 0.05,
    eval_sample: int = 500,
    seed: int = 42,
) -> dict:
    """Train on data (minus a held-out test split), evaluate, save to output. Returns metrics."""
    # Shuffles the train/test split, not used for crypto
    rng = random.Random(seed)  # nosec B311
    data = list(data)
    rng.shuffle(data)
    train_data, test_data = data, []
    if test_split > 0 and len(data) > 10:
        split = max(1, int(len(data) * (1 - test_split)))
        train_data, test_data = data[:split], data[split:]

    resolver = MLPackageResolver()
    resolver.train(train_data)
    metrics: dict = {"train_size": len(train_data), "test_size": len(test_data)}
    if test_data:
        if eval_sample and len(test_data) > eval_sample:
            test_data = rng.sample(test_data, eval_sample)
        metrics.update(evaluate(resolver, test_data))
    resolver.save(output)
    return metrics
