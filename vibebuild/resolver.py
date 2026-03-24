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


class DependencyResolver:
    """
    Resolves build dependencies and creates build order.

    Uses Koji to check which packages are already available and
    builds a DAG of dependencies that need to be built.
    """

    def __init__(
        self,
        koji_client: Optional[KojiClient] = None,
        koji_tag: str = "fedora-build",
        name_resolver=None,
    ):
        self.koji = koji_client or KojiClient()
        self.koji_tag = koji_tag
        self.name_resolver = name_resolver
        self._available_packages: Optional[set[str]] = None
        self._dependency_graph: dict[str, DependencyNode] = {}
        self._has_external_repos: Optional[bool] = None

    @property
    def available_packages(self) -> set[str]:
        """Lazily load available packages from Koji."""
        if self._available_packages is None:
            self._available_packages = set(self.koji.list_packages(self.koji_tag))
        return self._available_packages

    def refresh_available_packages(self) -> None:
        """Force refresh of available packages cache."""
        self._available_packages = None

    def _check_external_repos(self) -> bool:
        """Check if the build tag has external repos configured (cached)."""
        if self._has_external_repos is None:
            try:
                self._has_external_repos = self.koji.has_external_repos(self.koji_tag)
            except Exception:
                self._has_external_repos = False
        return self._has_external_repos


    def _is_our_package(self, package_name: str) -> bool:
        """Check if a package is registered in our local Koji.

        When external repos are configured, only packages registered in
        our local Koji need to be built by us. Everything else is assumed
        to come from the external repos (e.g. Fedora base packages).
        """
        if package_name in self.available_packages:
            return True
        if self.name_resolver:
            # Try virtual provide resolution (e.g. python3dist(foo) -> python3-foo)
            resolved = self.name_resolver.resolve(package_name)
            if resolved in self.available_packages:
                return True
            # Try RPM -> SRPM name mapping (e.g. python3-foo -> python-foo)
            try:
                for candidate in self.name_resolver.resolve_srpm_name(resolved):
                    if candidate in self.available_packages:
                        return True
            except (TypeError, AttributeError):
                pass
        return False

    def find_missing_deps(
        self, deps: list[str | BuildRequirement], check_provides: bool = True
    ) -> list[str]:
        """
        Find dependencies that are not available in Koji tag.

        Args:
            deps: List of dependency names or BuildRequirement objects
            check_provides: Also check if dep is provided by another package

        Returns:
            List of missing package names
        """
        missing = []
        has_ext_repos = self._check_external_repos()

        for dep in deps:
            name = dep.name if isinstance(dep, BuildRequirement) else dep

            # Normalize the name using resolver
            resolved_name = name
            if self.name_resolver:
                resolved_name = self.name_resolver.resolve(name)

            if resolved_name in self.available_packages:
                continue

            if self.koji.package_exists(resolved_name, self.koji_tag):
                continue

            # Also try original name if different from resolved
            if resolved_name != name:
                if name in self.available_packages:
                    continue
                if self.koji.package_exists(name, self.koji_tag):
                    continue

            # Try SRPM name mapping (e.g. python3-tomli -> python-tomli)
            srpm_name = None
            if self.name_resolver:
                try:
                    for candidate in self.name_resolver.resolve_srpm_name(resolved_name):
                        if candidate in self.available_packages:
                            srpm_name = candidate
                            break
                        if self.koji.package_exists(candidate, self.koji_tag):
                            srpm_name = candidate
                            break
                except (TypeError, AttributeError):
                    pass

            # If we found the SRPM name and it has a build, it's available
            if srpm_name and self.koji.package_exists(srpm_name, self.koji_tag):
                continue

            # If external repos are configured, deps NOT registered in our
            # local Koji are assumed available from the external repos.
            # Only deps that ARE our packages (registered but not yet built)
            # are truly missing.  Since we already checked package_exists
            # above and it returned False, any dep not in our package list
            # must come from external repos.
            if has_ext_repos:
                check_name = resolved_name if resolved_name != name else name
                if not self._is_our_package(check_name):
                    logger.debug(
                        f"  {check_name}: not in local Koji, assuming available via external repos"
                    )
                    continue

            # Use SRPM name for the missing list
            missing.append(srpm_name or resolved_name)

        return missing

    def build_dependency_graph(
        self, root_package: str, srpm_path: str, srpm_resolver: Optional[callable] = None
    ) -> dict[str, DependencyNode]:
        """
        Build complete dependency graph starting from root package.

        Args:
            root_package: Name of the package to build
            srpm_path: Path to SRPM of root package
            srpm_resolver: Function to resolve SRPM path for a package name

        Returns:
            Dictionary mapping package names to DependencyNode objects
        """
        self._dependency_graph = {}
        visited = set()

        def resolve_deps(pkg_name: str, pkg_srpm: Optional[str] = None):
            if pkg_name in visited:
                return
            visited.add(pkg_name)

            # Normalize the package name using resolver
            resolved_name = pkg_name
            if self.name_resolver:
                resolved_name = self.name_resolver.resolve(pkg_name)

            if self.koji.package_exists(resolved_name, self.koji_tag):
                self._dependency_graph[resolved_name] = DependencyNode(
                    name=resolved_name, is_available=True
                )
                return

            node = DependencyNode(name=resolved_name, srpm_path=pkg_srpm)

            if pkg_srpm:
                try:
                    requires = get_build_requires(pkg_srpm)
                    missing = self.find_missing_deps(requires)
                    node.dependencies = missing

                    for dep in missing:
                        dep_srpm = None
                        if srpm_resolver:
                            dep_srpm = srpm_resolver(dep)
                        resolve_deps(dep, dep_srpm)

                except Exception:
                    node.dependencies = []

            self._dependency_graph[resolved_name] = node

        resolve_deps(root_package, srpm_path)
        return self._dependency_graph

    def topological_sort(self) -> list[str]:
        """
        Return packages in build order (dependencies first).

        Returns:
            List of package names in order they should be built

        Raises:
            CircularDependencyError: If circular dependency detected
        """
        if not self._dependency_graph:
            return []

        adj: dict[str, list[str]] = defaultdict(list)
        for node in self._dependency_graph.values():
            for dep in node.dependencies:
                if dep in self._dependency_graph:
                    adj[dep].append(node.name)

        in_degree: dict[str, int] = {}
        for name, node in self._dependency_graph.items():
            in_degree[name] = sum(
                1
                for dep in node.dependencies
                if dep in self._dependency_graph and not self._dependency_graph[dep].is_available
            )

        queue = [
            name
            for name, degree in in_degree.items()
            if degree == 0
            and name in self._dependency_graph
            and not self._dependency_graph[name].is_available
        ]

        result = []
        order = 0

        while queue:
            pkg = queue.pop(0)

            if pkg in self._dependency_graph and not self._dependency_graph[pkg].is_available:  # pragma: no branch
                result.append(pkg)
                self._dependency_graph[pkg].build_order = order
                order += 1

            for dependent in adj.get(pkg, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        needs_build = [
            name for name, node in self._dependency_graph.items() if not node.is_available
        ]

        if len(result) < len(needs_build):
            missing = set(needs_build) - set(result)
            raise CircularDependencyError(f"Circular dependency detected involving: {missing}")

        return result

    def get_build_chain(self) -> list[list[str]]:
        """
        Get packages grouped by build level (parallel builds).

        Packages in the same group can be built in parallel.

        Returns:
            List of lists, each inner list contains packages that can be built together
        """
        sorted_packages = self.topological_sort()

        if not sorted_packages:
            return []

        levels: dict[str, int] = {}

        for pkg in sorted_packages:
            node = self._dependency_graph.get(pkg)
            if not node:
                continue

            if not node.dependencies:
                levels[pkg] = 0
            else:
                max_dep_level = -1
                for dep in node.dependencies:
                    if dep in levels:
                        max_dep_level = max(max_dep_level, levels[dep])
                levels[pkg] = max_dep_level + 1

        max_level = max(levels.values()) if levels else 0
        chains: list[list[str]] = [[] for _ in range(max_level + 1)]

        for pkg, level in levels.items():
            chains[level].append(pkg)

        return [chain for chain in chains if chain]

    def get_missing_packages(self) -> list[str]:
        """Get list of packages that need to be built."""
        return [
            name
            for name, node in self._dependency_graph.items()
            if not node.is_available and node.srpm_path is None
        ]
