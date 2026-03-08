"""
SRPM fetcher - downloads source RPMs from Fedora and other sources.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import requests  # pragma: no cover

    HAS_REQUESTS = True  # pragma: no cover
except ImportError:  # pragma: no cover
    requests = None
    HAS_REQUESTS = False

from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError


@dataclass
class SRPMSource:
    """Configuration for an SRPM source."""

    name: str
    base_url: str
    koji_server: Optional[str] = None
    priority: int = 100


class SRPMFetcher:
    """
    Fetches SRPMs from various sources.

    Primary source is Fedora's Koji, but can also use
    src.fedoraproject.org and other mirrors.
    """

    DEFAULT_SOURCES = [
        SRPMSource(
            name="fedora-koji",
            base_url="https://kojipkgs.fedoraproject.org/packages",
            koji_server="https://koji.fedoraproject.org/kojihub",
            priority=10,
        ),
        SRPMSource(name="fedora-src", base_url="https://src.fedoraproject.org/rpms", priority=20),
    ]

    def __init__(
        self,
        download_dir: Optional[str] = None,
        sources: Optional[list[SRPMSource]] = None,
        fedora_release: str = "rawhide",
        no_ssl_verify: bool = False,
        name_resolver=None,
    ):
        self.download_dir = (
            Path(download_dir) if download_dir else Path(tempfile.gettempdir()) / "vibebuild"
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.sources = sorted(sources or self.DEFAULT_SOURCES, key=lambda s: s.priority)
        self.fedora_release = fedora_release
        self.no_ssl_verify = no_ssl_verify
        self.name_resolver = name_resolver
        self._cache: dict[str, str] = {}

    def _get_env(self) -> Optional[dict]:
        """Get environment variables for subprocess, with SSL verification disabled if needed."""
        if self.no_ssl_verify:
            env = os.environ.copy()
            env["PYTHONHTTPSVERIFY"] = "0"
            env["REQUESTS_CA_BUNDLE"] = ""
            env["CURL_CA_BUNDLE"] = ""
            return env
        return None

    def download_srpm(self, package_name: str, version: Optional[str] = None) -> str:
        """
        Download SRPM for a package.

        Tries each configured source until SRPM is found.
        If a name_resolver is configured, tries multiple SRPM name variants.

        Args:
            package_name: Name of the package
            version: Optional specific version to download

        Returns:
            Path to downloaded SRPM file

        Raises:
            SRPMNotFoundError: If SRPM cannot be found in any source
        """
        # Get SRPM name variants to try (ML + rule-based via resolver, or just the name)
        if self.name_resolver:
            names_to_try = self.name_resolver.get_download_candidates(package_name)
        else:
            names_to_try = [package_name]

        last_errors: list[str] = []
        for name in names_to_try:
            cache_key = f"{name}-{version or 'latest'}"
            if cache_key in self._cache:
                cached_path = self._cache[cache_key]
                if Path(cached_path).exists():
                    return cached_path

            errors = []

            for source in self.sources:
                try:
                    if source.koji_server:
                        srpm_path = self._download_from_koji(name, version, source)
                    else:
                        srpm_path = self._download_from_src(name, version, source)

                    self._cache[cache_key] = srpm_path
                    return srpm_path

                except Exception as e:
                    errors.append(f"{source.name}: {str(e)}")
                    continue

            last_errors = errors

        msg = f"Could not find SRPM for {package_name} (tried: {names_to_try})"
        if last_errors:
            msg += "\n" + "\n".join(f"  {e}" for e in last_errors)
        raise SRPMNotFoundError(msg)

    def _resolve_rawhide_tag(self, server) -> Optional[str]:
        """Determine the current rawhide Fedora version tag by querying Koji."""
        try:
            # getBuildTarget('rawhide') returns the target info including dest_tag
            target_info = server.getBuildTarget("rawhide")
            if target_info and target_info.get("dest_tag_name"):
                # dest_tag_name is like "f44" for current rawhide
                return target_info["dest_tag_name"]
        except Exception:
            pass
        # Fallback: probe tags from high to low
        for ver in range(45, 38, -1):
            try:
                tag = server.getTag(f"f{ver}")
                if tag:
                    return f"f{ver}"
            except Exception:
                continue
        return None

    def _download_from_koji(
        self, package_name: str, version: Optional[str], source: SRPMSource
    ) -> str:
        """Download SRPM from Koji using XML-RPC API (no CLI needed)."""
        import xmlrpc.client

        try:
            server = xmlrpc.client.ServerProxy(source.koji_server, allow_none=True)
        except Exception as e:
            raise SRPMNotFoundError(f"Cannot connect to Koji: {e}")

        # Find the latest build
        if self.fedora_release == "rawhide":
            tag = self._resolve_rawhide_tag(server)
        else:
            tag = f"f{self.fedora_release}" if not self.fedora_release.startswith("f") else self.fedora_release
        try:
            if version:
                build_info = server.getBuild(f"{package_name}-{version}")
            else:
                builds = server.getLatestBuilds(tag, None, package_name)
                if not builds:
                    builds = server.getLatestBuilds("rawhide", None, package_name)
                if not builds:
                    # Try the previous release tag as well
                    tag_num = int(tag.lstrip("f")) if tag.startswith("f") and tag[1:].isdigit() else 0
                    if tag_num > 0:
                        for prev in range(tag_num - 1, tag_num - 4, -1):
                            builds = server.getLatestBuilds(f"f{prev}", None, package_name)
                            if builds:
                                break
                if not builds:
                    raise SRPMNotFoundError(f"Package {package_name} not found in Koji")
                build_info = builds[0]
        except SRPMNotFoundError:
            raise
        except Exception as e:
            raise SRPMNotFoundError(f"Package {package_name} not found in Koji")

        if not build_info:
            raise SRPMNotFoundError(f"Package {package_name} not found in Koji")

        nvr = build_info["nvr"]
        name = build_info["name"]
        ver = build_info["version"]
        rel = build_info["release"]

        # Construct download URL
        base_url = source.base_url.rstrip("/")
        srpm_url = f"{base_url}/{name}/{ver}/{rel}/src/{nvr}.src.rpm"

        download_path = self.download_dir / package_name
        download_path.mkdir(parents=True, exist_ok=True)
        dest = download_path / f"{nvr}.src.rpm"

        if dest.exists():
            return str(dest)

        self._download_file(srpm_url, dest)

        if not dest.exists():
            raise SRPMNotFoundError(f"Failed to download SRPM from {srpm_url}")

        return str(dest)

    def _download_from_src(
        self, package_name: str, version: Optional[str], source: SRPMSource
    ) -> str:
        """Download spec and sources from src.fedoraproject.org."""
        if not HAS_REQUESTS:
            raise VibeBuildError("requests library required for src.fedoraproject.org")

        spec_url = (
            f"{source.base_url}/{package_name}/raw/{self.fedora_release}/f/{package_name}.spec"
        )

        response = requests.get(spec_url, timeout=30, verify=not self.no_ssl_verify)
        if response.status_code != 200:
            raise SRPMNotFoundError(f"Spec not found at {spec_url}")

        spec_content = response.text

        work_dir = self.download_dir / package_name / "build"
        work_dir.mkdir(parents=True, exist_ok=True)

        spec_path = work_dir / f"{package_name}.spec"
        spec_path.write_text(spec_content)

        sources = self._extract_sources(spec_content)
        sources_dir = work_dir / "SOURCES"
        sources_dir.mkdir(exist_ok=True)

        for source_file in sources:
            if source_file.startswith(("http://", "https://", "ftp://")):
                self._download_file(source_file, sources_dir / Path(source_file).name)
            else:
                lookaside_url = (
                    f"https://src.fedoraproject.org/lookaside/pkgs/{package_name}/{source_file}"
                )
                try:
                    self._download_file(lookaside_url, sources_dir / source_file)
                except Exception:
                    pass

        result = subprocess.run(
            [
                "rpmbuild",
                "-bs",
                "--define",
                f"_topdir {work_dir}",
                "--define",
                f"_sourcedir {sources_dir}",
                "--define",
                f"_srcrpmdir {work_dir}",
                str(spec_path),
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise VibeBuildError(f"Failed to build SRPM: {result.stderr}")

        srpms = list(work_dir.glob("*.src.rpm"))
        if not srpms:
            raise SRPMNotFoundError(f"No SRPM created for {package_name}")

        return str(srpms[0])

    def _extract_sources(self, spec_content: str) -> list[str]:
        """Extract source URLs from spec content."""
        sources = []
        pattern = re.compile(r"^Source\d*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

        for match in pattern.finditer(spec_content):
            source = match.group(1).strip()
            sources.append(source)

        return sources

    def _download_file(self, url: str, dest: Path) -> None:
        """Download a file from URL."""
        if not HAS_REQUESTS:
            cmd = ["curl", "-L", "-o", str(dest), url]
            if self.no_ssl_verify:
                cmd.insert(1, "-k")
            subprocess.run(cmd, check=True, timeout=300, env=self._get_env())
            return

        response = requests.get(url, stream=True, timeout=300, verify=not self.no_ssl_verify)
        response.raise_for_status()

        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

    def search_fedora_src(self, name: str) -> list[str]:
        """
        Search for packages in Fedora src.

        Args:
            name: Package name or pattern

        Returns:
            List of matching package names
        """
        if not HAS_REQUESTS:
            result = subprocess.run(
                [
                    "koji",
                    "--server=https://koji.fedoraproject.org/kojihub",
                    "search",
                    "package",
                    f"*{name}*",
                ],
                capture_output=True,
                text=True,
                env=self._get_env(),
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.strip().split("\n") if line]
            return []

        url = f"https://src.fedoraproject.org/api/0/projects?pattern=*{name}*&namespace=rpms"

        try:
            response = requests.get(url, timeout=30, verify=not self.no_ssl_verify)
            if response.status_code == 200:
                data = response.json()
                return [p["name"] for p in data.get("projects", [])]
        except Exception:
            pass

        return []

    def get_package_versions(self, package_name: str) -> list[str]:
        """Get available versions for a package."""
        result = subprocess.run(
            [
                "koji",
                "--server=https://koji.fedoraproject.org/kojihub",
                "list-builds",
                "--package",
                package_name,
                "--quiet",
            ],
            capture_output=True,
            text=True,
            env=self._get_env(),
        )

        versions = []
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    nvr = line.split()[0]
                    parts = nvr.rsplit("-", 2)
                    if len(parts) >= 2:
                        versions.append(parts[-2])

        return list(set(versions))

    def clear_cache(self) -> None:
        """Clear downloaded SRPM cache."""
        self._cache.clear()

    def cleanup(self) -> None:
        """Remove all downloaded files."""
        import shutil

        if self.download_dir.exists():
            shutil.rmtree(self.download_dir)
        self._cache.clear()
