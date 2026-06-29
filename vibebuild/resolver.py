"""
Dependency resolver - checks dependencies in Koji and builds DAG.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from vibebuild.analyzer import BuildRequirement, PackageInfo, get_build_requires
from vibebuild.exceptions import CircularDependencyError
from vibebuild.koji_client import KojiClient

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
    srpm_path: str | None = None
    package_info: PackageInfo | None = None
    dependencies: list[str] = field(default_factory=list)
    is_available: bool = False
    build_order: int = -1


class DependencyResolver:
    """
    Resolves build dependencies and creates build order.

    Uses Koji to check which packages are already available and
    builds a DAG of dependencies that need to be built.
    """

    def __init__(
        self,
        koji_client: KojiClient,
        koji_tag: str,
        name_resolver=None,
        build_all_deps: bool = False,
    ):
        self.koji = koji_client
        self.koji_tag = koji_tag
        self.name_resolver = name_resolver
        self.build_all_deps = build_all_deps
        self._available_packages: set[str] | None = None
        self._dependency_graph: dict[str, DependencyNode] = {}
        self._root: str | None = None
        self._has_external_repos: bool | None = None

    @property
    def dependency_graph(self) -> dict[str, DependencyNode]:
        """Nodes of the last build_dependency_graph run, by package name."""
        return self._dependency_graph

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
        required_op: str | None,
        required_ver: str | None,
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

    def _prefetch(self, deps: Sequence[str | BuildRequirement]) -> None:
        """Ask Koji about every name find_missing_deps may look up, in one request."""
        names: list[str] = []
        for dep in deps:
            name = dep.name if isinstance(dep, BuildRequirement) else dep
            names.append(name)
            if self.name_resolver:
                resolved = self.name_resolver.resolve(name)
                names.append(resolved)
                try:
                    names.extend(self.name_resolver.resolve_srpm_name(resolved))
                except (TypeError, AttributeError):
                    pass
        self.koji.prefetch_latest_builds([n for n in names if isinstance(n, str)], self.koji_tag)

    def find_missing_deps(
        self, deps: Sequence[str | BuildRequirement], check_provides: bool = True
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
        self._prefetch(deps)

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
        self, root_package: str, srpm_path: str, srpm_resolver: Callable | None = None
    ) -> dict[str, DependencyNode]:
        """
        Build complete dependency graph starting from root package.

        The root is always analyzed, even when a build of it is already in the tag:
        a new version may need dependencies the tagged one did not.

        Args:
            root_package: Name of the package to build
            srpm_path: Path to SRPM of root package
            srpm_resolver: Function to resolve SRPM path for a package name

        Returns:
            Dictionary mapping package names to DependencyNode objects
        """
        self._dependency_graph = {}
        self._root = root_package
        visited = set()

        def resolve_deps(pkg_name: str, pkg_srpm: str | None = None):
            if pkg_name in visited:
                return
            visited.add(pkg_name)
            is_root = pkg_name == root_package

            # Normalize dependency names; the root is an SRPM name already
            resolved_name = pkg_name
            if self.name_resolver and not is_root:
                resolved_name = self.name_resolver.resolve(pkg_name)

            if not is_root and self.koji.package_exists(resolved_name, self.koji_tag):
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

                        def _safe_resolve(d: str) -> str | None:
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

                except Exception as exc:
                    logger.warning("Could not analyze %s: %s", resolved_name, exc)
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
        Get the dependencies to build, grouped by build level (parallel builds).

        Packages in the same group can be built in parallel. The root package of
        the graph is not included: callers build it after the chain.

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
            if pkg != self._root:
                chains[level].append(pkg)

        return [chain for chain in chains if chain]

    def get_missing_packages(self) -> list[str]:
        """Get list of packages that need to be built."""
        return [
            name
            for name, node in self._dependency_graph.items()
            if not node.is_available and node.srpm_path is None
        ]
