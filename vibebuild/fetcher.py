"""
SRPM fetcher - downloads source RPMs from Fedora Koji.

Every download is checked against the digest Koji recorded for the build
(`payloadhash`, the MD5 of the RPM header and payload). The digest is computed
from the file's bytes, not read from its signature header, so a cached or freshly
downloaded file is used only when it is exactly the SRPM Fedora built.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
import tempfile
from pathlib import Path

import koji  # type: ignore[import-untyped]
import requests

from vibebuild._retry import with_retry
from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError

logger = logging.getLogger(__name__)

FEDORA_KOJI = "https://koji.fedoraproject.org/kojihub"
FEDORA_PACKAGES = "https://kojipkgs.fedoraproject.org/packages"

# Fedora SRPMs are rarely over 1 GiB; anything far beyond that is not an SRPM.
MAX_SRPM_BYTES = 4 * 1024**3

# Characters allowed in a Koji package name and NVR; anything else could escape
# the download directory or the URL path.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~^-]*$")


def default_download_dir() -> Path:
    """Per-user SRPM cache: $XDG_CACHE_HOME/vibebuild/srpms (~/.cache by default)."""
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "vibebuild" / "srpms"


_RPM_LEAD_MAGIC = b"\xed\xab\xee\xdb"
_RPM_HEADER_MAGIC = b"\x8e\xad\xe8\x01\x00\x00\x00\x00"


def srpm_digest(path: Path) -> str:
    """MD5 of an RPM's header and payload, which Koji records as `payloadhash`.

    That is everything after the 96-byte lead and the signature header. The
    SIGMD5 tag holds the same value, but it is only what the file claims, and
    rpm 6 no longer checks it.
    """
    try:
        with path.open("rb") as f:
            lead = f.read(96)
            intro = f.read(16)
            if lead[:4] != _RPM_LEAD_MAGIC or intro[:8] != _RPM_HEADER_MAGIC:
                raise VibeBuildError(f"Not an RPM file: {path.name}")
            count, size = struct.unpack(">II", intro[8:])
            signature = 16 * count + size
            f.seek(signature + (-signature) % 8, os.SEEK_CUR)  # padded to 8 bytes
            # Koji records only MD5; it is compared against Koji's value, not trusted alone
            return hashlib.file_digest(f, "md5").hexdigest()  # nosec B324
    except OSError as e:
        raise VibeBuildError(f"Cannot read {path.name}: {e}") from e


class SRPMFetcher:
    """Fetches SRPMs of the latest Fedora builds from Fedora Koji."""

    def __init__(
        self,
        download_dir: str | None = None,
        fedora_release: str = "rawhide",
        no_ssl_verify: bool = False,
        name_resolver=None,
        koji_server: str = FEDORA_KOJI,
        packages_url: str = FEDORA_PACKAGES,
    ):
        if download_dir:
            self.download_dir = Path(download_dir)
            self.download_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.download_dir = default_download_dir()
            self.download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fedora_release = fedora_release
        self.no_ssl_verify = no_ssl_verify
        self.name_resolver = name_resolver
        self.koji_server = koji_server
        self.packages_url = packages_url.rstrip("/")
        self._cache: dict[str, str] = {}

    def _session(self) -> koji.ClientSession:
        """A fresh anonymous session: downloads run in parallel threads."""
        opts: dict = {"anon_retry": True, "max_retries": 3, "retry_interval": 2}
        if self.no_ssl_verify:
            opts["no_ssl_verify"] = True
        return koji.ClientSession(self.koji_server, opts=opts)

    def download_srpm(self, package_name: str, version: str | None = None) -> str:
        """
        Download the SRPM of a package's latest build (or of `version`).

        If a name_resolver is configured, tries its SRPM name candidates in order.

        Args:
            package_name: Package (or RPM/provide) name
            version: Optional "version-release" of the build to download

        Returns:
            Path to the verified SRPM file

        Raises:
            SRPMNotFoundError: If no candidate name has a build in Fedora Koji
        """
        if self.name_resolver:
            names_to_try = self.name_resolver.get_download_candidates(package_name)
        else:
            names_to_try = [package_name]

        errors: list[str] = []
        for name in names_to_try:
            cache_key = f"{name}-{version or 'latest'}"
            cached = self._cache.get(cache_key)
            if cached and Path(cached).exists():
                return cached
            try:
                path = self._download_from_koji(name, version)
            except SRPMNotFoundError as e:
                errors.append(str(e))
                continue
            self._cache[cache_key] = path
            return path

        msg = f"Could not find SRPM for {package_name} (tried: {names_to_try})"
        if errors:
            msg += "\n" + "\n".join(f"  {e}" for e in errors)
        raise SRPMNotFoundError(msg)

    def _release_tags(self, session: koji.ClientSession) -> list[str]:
        """Tags to look the latest build up in, most specific first."""
        if self.fedora_release == "rawhide":
            target = session.getBuildTarget("rawhide")
            if not target:
                raise SRPMNotFoundError("Fedora Koji has no rawhide target")
            return [target["build_tag_name"]]
        tag = self.fedora_release
        if not tag.startswith("f"):
            tag = f"f{tag}"
        # f<N>-updates inherits f<N>: it sees stable updates as well as the GA builds
        return [f"{tag}-updates", tag]

    def _find_build(self, session: koji.ClientSession, name: str, version: str | None):
        if version:
            return session.getBuild(f"{name}-{version}")
        for tag in self._release_tags(session):
            try:
                builds = session.getLatestBuilds(tag, package=name)
            except koji.GenericError:  # unknown tag or package
                continue
            if builds:
                return builds[0]
        return None

    def _download_from_koji(self, package_name: str, version: str | None) -> str:
        """Find the build in Koji, download its SRPM and check it against Koji's digest."""
        if not _SAFE_NAME.match(package_name):
            raise SRPMNotFoundError(f"Invalid package name: {package_name!r}")
        session = self._session()
        try:
            build = self._find_build(session, package_name, version)
            if not build:
                raise SRPMNotFoundError(f"Package {package_name} not found in Fedora Koji")
            srpms = session.listRPMs(buildID=build["build_id"], arches="src")
        except SRPMNotFoundError:
            raise
        except Exception as e:
            raise SRPMNotFoundError(f"Koji lookup for {package_name} failed: {e}") from e
        if not srpms:
            raise SRPMNotFoundError(f"Build {build['nvr']} has no SRPM")

        nvr, digest = build["nvr"], srpms[0]["payloadhash"]
        if not (_SAFE_NAME.match(build["name"]) and _SAFE_NAME.match(nvr)):
            raise SRPMNotFoundError(f"Unexpected build name from Koji: {nvr!r}")
        dest = self.download_dir / f"{nvr}.src.rpm"

        if dest.is_file() and not dest.is_symlink():
            if srpm_digest(dest) == digest:
                logger.debug("Using cached %s", dest.name)
                return str(dest)
            logger.warning("Cached %s does not match Koji, downloading again", dest.name)

        url = f"{self.packages_url}/{build['name']}/{build['version']}/{build['release']}/src/"
        self._download_verified(f"{url}{nvr}.src.rpm", dest, digest)
        return str(dest)

    def _download_verified(self, url: str, dest: Path, digest: str) -> None:
        """Download into a temp file next to dest, check the digest, then move into place."""
        fd, tmp_name = tempfile.mkstemp(prefix=".download-", dir=self.download_dir)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                self._download_file(url, f)
            actual = srpm_digest(tmp)
            if actual != digest:
                raise VibeBuildError(
                    f"{dest.name} does not match Koji (got {actual}, expected {digest})"
                )
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

    @with_retry(attempts=3, initial_delay=2.0, backoff=2.0)
    def _download_file(self, url: str, out) -> None:
        """Stream url into the open binary file `out`, at most MAX_SRPM_BYTES."""
        if not url.startswith("https://"):
            raise VibeBuildError(f"Refusing to download over plain HTTP: {url}")
        out.seek(0)
        out.truncate()
        with requests.get(url, stream=True, timeout=300, verify=not self.no_ssl_verify) as r:
            r.raise_for_status()
            size = 0
            for chunk in r.iter_content(chunk_size=1 << 16):
                size += len(chunk)
                if size > MAX_SRPM_BYTES:
                    raise VibeBuildError(f"{url} is larger than {MAX_SRPM_BYTES} bytes")
                out.write(chunk)

    def clear_cache(self) -> None:
        """Forget which names were already downloaded in this run."""
        self._cache.clear()
