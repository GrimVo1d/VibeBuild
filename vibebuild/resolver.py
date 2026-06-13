"""
Dependency resolver - checks dependencies in Koji and builds DAG.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from vibebuild._retry import with_retry
from vibebuild.analyzer import BuildRequirement, PackageInfo, get_build_requires
from vibebuild.exceptions import CircularDependencyError, KojiConnectionError

logger = logging.getLogger(__name__)

try:
    import rpm as _rpm  # type: ignore

    HAS_RPM = True
except ImportError:  # pragma: no cover - rpm may be missing on a macOS host
    HAS_RPM = False


def _compare_versions(a: tuple[str, str, str], b: tuple[str, str, str]) -> int:
    """
    Compare (epoch, version, release). Uses rpm.labelCompare if available,
    otherwise does a good enough comparison via string tuples.
    """
    if HAS_RPM:
        return _rpm.labelCompare(a, b)
    # Fallback: character by character on version, then on release; epoch as a number
    ea = int(a[0] or "0")
    eb = int(b[0] or "0")
    if ea != eb:
        return (ea > eb) - (ea < eb)
    for x, y in ((a[1], b[1]), (a[2], b[2])):
        if x == y:
            continue
        # Compare tokens split into digits/letters
        import re as _re

        xa = _re.findall(r"\d+|[A-Za-z]+", x)
        ya = _re.findall(r"\d+|[A-Za-z]+", y)
        for xs, ys in zip(xa, ya):
            if xs.isdigit() and ys.isdigit():
                ix, iy = int(xs), int(ys)
                if ix != iy:
                    return (ix > iy) - (ix < iy)
            elif xs != ys:
                return (xs > ys) - (xs < ys)
        if len(xa) != len(ya):
            return (len(xa) > len(ya)) - (len(xa) < len(ya))
    return 0


_OP_FN = {
    ">=": lambda c: c >= 0,
    ">": lambda c: c > 0,
    "=": lambda c: c == 0,
    "==": lambda c: c == 0,
    "<=": lambda c: c <= 0,
    "<": lambda c: c < 0,
}


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

    @with_retry(attempts=3, initial_delay=1.0, backoff=2.0)
    def _run_koji_command(self, *args) -> subprocess.CompletedProcess:
        """Run koji command with configured options.

        Wrapped in retry: on network/timeout errors it retries up to 3 times
        with exponential backoff (1s → 2s → 4s). Programming errors (broken
        arguments, missing tag) are not retried: KojiConnectionError
        carries text that does not match the transient markers.
        """
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

    def latest_build(self, package: str, tag: str) -> Optional[dict[str, str]]:
        """
        Return the NVR and parsed version/release of the latest build of a package in the tag.

        Returns:
            {"nvr": "...", "version": "...", "release": "..."} or None if there is no build.
        """
        result = self._run_koji_command(
            "list-tagged", tag, package, "--quiet", "--inherit", "--latest"
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
        if not line:
            return None
        nvr = line.split()[0]
        # Parse NVR from the end: <release> (maybe with dist suffix), <version>, rest is the name
        parts = nvr.rsplit("-", 2)
        if len(parts) < 3:
            return None
        return {"nvr": nvr, "version": parts[-2], "release": parts[-1]}

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
        build_all_deps: bool = False,
    ):
        self.koji = koji_client or KojiClient()
        self.koji_tag = koji_tag
        self.name_resolver = name_resolver
        self.build_all_deps = build_all_deps
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

    def _version_ok(
        self,
        package: str,
        required_op: Optional[str],
        required_ver: Optional[str],
    ) -> bool:
        """
        Check that the latest build of a package satisfies required_op required_ver.

        Without a constraint (op/ver = None) it is always True. If the latest build
        could not be fetched, optimistically return True so as not to block
        the build because of temporary Koji unavailability.
        """
        if not required_op or not required_ver:
            return True
        try:
            latest = self.koji.latest_build(package, self.koji_tag)
        except Exception as exc:
            logger.debug("Failed to get latest_build for %s: %s", package, exc)
            return True
        if not latest:
            return True
        # required_ver may contain a release ("1.12-2"); if not, there is no need to compare release
        if "-" in required_ver:
            ver_part, rel_part = required_ver.split("-", 1)
            actual = ("0", latest["version"], latest["release"])
            wanted = ("0", ver_part, rel_part)
        else:
            # Compare only version, set the release to the same value
            ver_part = required_ver
            actual = ("0", latest["version"], "0")
            wanted = ("0", ver_part, "0")
        cmp_fn = _OP_FN.get(required_op)
        if cmp_fn is None:
            return True
        result = cmp_fn(_compare_versions(actual, wanted))
        if not result:
            logger.debug(
                "version check fail: %s has %s but requires %s %s",
                package,
                latest["nvr"],
                required_op,
                required_ver,
            )
        return result

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
            required_op = dep.operator if isinstance(dep, BuildRequirement) else None
            required_ver = dep.version if isinstance(dep, BuildRequirement) else None

            # Normalize the name using resolver
            resolved_name = name
            if self.name_resolver:
                resolved_name = self.name_resolver.resolve(name)

            if resolved_name in self.available_packages:
                # Whitelisted in our koji but no build yet -> vibebuild must build it.
                # Without this check a whitelisted package with no build counts as available
                # and is quietly taken from external_repo, bypassing the local build and tagging.
                if self.koji.latest_build(resolved_name, self.koji_tag) is None:
                    logger.info(
                        "  %s: whitelisted in %s but no build exists, will be built",
                        resolved_name,
                        self.koji_tag,
                    )
                    missing.append(resolved_name)
                    continue
                if self._version_ok(resolved_name, required_op, required_ver):
                    continue
                logger.info(
                    "  %s: version constraint %s %s is not satisfied by the current build",
                    resolved_name,
                    required_op,
                    required_ver,
                )
                missing.append(resolved_name)
                continue

            if self.koji.package_exists(resolved_name, self.koji_tag):
                if self._version_ok(resolved_name, required_op, required_ver):
                    continue
                logger.info(
                    "  %s: version constraint %s %s is not satisfied",
                    resolved_name,
                    required_op,
                    required_ver,
                )
                missing.append(resolved_name)
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
            # When build_all_deps=True this assumption is bypassed — every dep
            # in the graph is fetched from Fedora and built locally.
            if has_ext_repos and not self.build_all_deps:
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
        self, root_package: str, srpm_path: str, srpm_resolver: Optional[Callable] = None
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

                    # Download SRPMs for all new missing deps in parallel,
                    # then walk them recursively in a deterministic order.
                    if srpm_resolver and missing:
                        from concurrent.futures import ThreadPoolExecutor

                        def _safe_resolve(d: str) -> Optional[str]:
                            try:
                                return srpm_resolver(d)
                            except Exception as exc:
                                logger.debug("srpm_resolver(%s) failed: %s", d, exc)
                                return None

                        with ThreadPoolExecutor(max_workers=5) as ex:
                            dep_srpms = list(ex.map(_safe_resolve, missing))
                    else:
                        dep_srpms = [None] * len(missing)

                    for dep, dep_srpm in zip(missing, dep_srpms):
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

            if (
                pkg in self._dependency_graph and not self._dependency_graph[pkg].is_available
            ):  # pragma: no branch
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
