"""
Dependency resolver - checks dependencies in Koji and builds DAG.
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from vibebuild.analyzer import BuildRequirement, PackageInfo, get_build_requires
from vibebuild.exceptions import CircularDependencyError, KojiConnectionError

logger = logging.getLogger(__name__)


@dataclass
class DependencyNode:
    """Node in dependency graph."""

    name: str
    srpm_path: Optional[str] = None
    package_info: Optional[PackageInfo] = None
    dependencies: list[str] = field(default_factory=list)
    is_available: bool = False
    build_order: int = -1


class KojiClient:
    """Client for interacting with Koji."""

    def __init__(
        self,
        server: str = "https://koji.fedoraproject.org/kojihub",
        web_url: str = "https://koji.fedoraproject.org/koji",
        cert: Optional[str] = None,
        serverca: Optional[str] = None,
        no_ssl_verify: bool = False,
    ):
        self.server = server
        self.web_url = web_url
        self.cert = cert
        self.serverca = serverca
        self.no_ssl_verify = no_ssl_verify

    def _get_env(self) -> Optional[dict]:
        """Get environment variables for subprocess, with SSL verification disabled if needed."""
        if self.no_ssl_verify:
            env = os.environ.copy()
            env["PYTHONHTTPSVERIFY"] = "0"
            env["REQUESTS_CA_BUNDLE"] = ""
            env["CURL_CA_BUNDLE"] = ""
            return env
        return None

    def _run_koji_command(self, *args) -> subprocess.CompletedProcess:
        """Run koji command with configured options."""
        cmd = ["koji", f"--server={self.server}"]

        if self.cert:
            cmd.append(f"--cert={self.cert}")
        # Note: --serverca is not supported on RHEL9/older koji CLI.
        # The serverca is read from ~/.koji/config instead.

        cmd.extend(args)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, env=self._get_env()
            )
            return result
        except subprocess.TimeoutExpired:
            raise KojiConnectionError(f"Koji command timed out: {' '.join(cmd)}")
        except Exception as e:
            raise KojiConnectionError(f"Failed to run koji command: {e}")

    def list_packages(self, tag: str) -> list[str]:
        """List all packages in a tag."""
        result = self._run_koji_command("list-pkgs", f"--tag={tag}", "--quiet")

        if result.returncode != 0:
            # "no matching packages" is normal for empty tags
            if "no matching packages" in (result.stderr + result.stdout):
                return []
            raise KojiConnectionError(f"Failed to list packages: {result.stderr}")

        packages = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split()
                if parts:
                    packages.append(parts[0])

        return packages

    def list_tagged_builds(self, tag: str) -> dict[str, str]:
        """List all builds in a tag, returns {package_name: nvr}."""
        result = self._run_koji_command("list-tagged", tag, "--quiet")

        if result.returncode != 0:
            raise KojiConnectionError(f"Failed to list builds: {result.stderr}")

        builds = {}
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split()
                if parts:
                    nvr = parts[0]
                    name = "-".join(nvr.rsplit("-", 2)[:-2])
                    builds[name] = nvr

        return builds

    def package_exists(self, package: str, tag: str) -> bool:
        """Check if package has a build tagged in the given tag (including inherited)."""
        result = self._run_koji_command("list-tagged", tag, package, "--quiet", "--inherit")
        return bool(result.stdout.strip())


    def has_external_repos(self, tag: str) -> bool:
        """Check if a tag has external repos configured."""
        result = self._run_koji_command("list-external-repos", f"--tag={tag}")
        if result.returncode != 0:
            return False
        # Output has header lines; actual repos have URL-like content
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and "http" in line:
                return True
        return False

    def search_package(self, pattern: str) -> list[str]:
        """Search for packages by pattern."""
        result = self._run_koji_command("search", "package", pattern)

        if result.returncode != 0:
            return []

        return [line.strip() for line in result.stdout.strip().split("\n") if line]
