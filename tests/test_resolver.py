"""Tests for vibebuild.resolver module."""

from unittest.mock import MagicMock, Mock, patch

import koji
import pytest

from vibebuild.analyzer import BuildRequirement
from vibebuild.exceptions import (
    CircularDependencyError,
    KojiConnectionError,
)
from vibebuild.resolver import (
    DependencyNode,
    DependencyResolver,
    KojiClient,
)


def _build(name, version, release):
    return {
        "package_name": name,
        "nvr": f"{name}-{version}-{release}",
        "version": version,
        "release": release,
    }


@pytest.fixture
def hub(mocker):
    """The koji.ClientSession behind KojiClient."""
    session = MagicMock()
    mocker.patch("vibebuild.resolver.koji.ClientSession", return_value=session)
    return session


class TestKojiClient:
    def test_default_server(self):
        client = KojiClient()

        assert client.server == "https://koji.fedoraproject.org/kojihub"
        assert client.web_url == "https://koji.fedoraproject.org/koji"

    def test_session_verifies_with_serverca(self, mocker):
        cls = mocker.patch("vibebuild.resolver.koji.ClientSession")
        KojiClient(server="https://hub/kojihub", serverca="/ca.crt").session

        assert cls.call_args.args[0] == "https://hub/kojihub"
        assert cls.call_args.kwargs["opts"]["serverca"] == "/ca.crt"
        assert "no_ssl_verify" not in cls.call_args.kwargs["opts"]

    def test_session_no_ssl_verify(self, mocker):
        cls = mocker.patch("vibebuild.resolver.koji.ClientSession")
        KojiClient(serverca="/ca.crt", no_ssl_verify=True).session

        assert cls.call_args.kwargs["opts"]["no_ssl_verify"] is True
        assert "serverca" not in cls.call_args.kwargs["opts"]

    def test_session_created_once(self, mocker):
        cls = mocker.patch("vibebuild.resolver.koji.ClientSession")
        client = KojiClient()

        assert client.session is client.session
        cls.assert_called_once()

    def test_list_packages(self, hub):
        hub.listPackages.return_value = [{"package_name": "gcc"}, {"package_name": "make"}]

        assert KojiClient().list_packages("f44") == ["gcc", "make"]
        hub.listPackages.assert_called_once_with(tagID="f44", inherited=True, with_blocked=False)

    def test_list_tagged_builds(self, hub):
        hub.listTagged.return_value = [_build("gcc", "15.1", "1.fc44")]

        assert KojiClient().list_tagged_builds("f44") == {"gcc": "gcc-15.1-1.fc44"}
        hub.listTagged.assert_called_once_with("f44", latest=True)

    def test_transport_error_becomes_connection_error(self, hub):
        hub.listPackages.side_effect = OSError("Connection refused")

        with pytest.raises(KojiConnectionError, match="listPackages"):
            KojiClient().list_packages("f44")

    def test_latest_build(self, hub):
        hub.getLatestBuilds.return_value = [_build("gcc", "15.1", "1.fc44")]

        assert KojiClient().latest_build("gcc", "f44") == {
            "nvr": "gcc-15.1-1.fc44",
            "version": "15.1",
            "release": "1.fc44",
        }
        hub.getLatestBuilds.assert_called_once_with("f44", package="gcc")

    def test_latest_build_none(self, hub):
        hub.getLatestBuilds.return_value = []

        assert KojiClient().latest_build("gcc", "f44") is None

    def test_latest_build_unknown_package(self, hub):
        hub.getLatestBuilds.side_effect = koji.GenericError("No such entry in table package")

        assert KojiClient().latest_build("nope", "f44") is None

    def test_latest_build_cached(self, hub):
        hub.getLatestBuilds.return_value = []
        client = KojiClient()

        client.latest_build("gcc", "f44")
        client.latest_build("gcc", "f44")
        assert hub.getLatestBuilds.call_count == 1

        client.clear_cache()
        client.latest_build("gcc", "f44")
        assert hub.getLatestBuilds.call_count == 2

    def test_package_exists(self, hub):
        hub.getLatestBuilds.side_effect = lambda tag, package: (
            [_build("gcc", "15.1", "1.fc44")] if package == "gcc" else []
        )
        client = KojiClient()

        assert client.package_exists("gcc", "f44") is True
        assert client.package_exists("missing", "f44") is False

    def test_prefetch_latest_builds(self, hub):
        found = MagicMock(result=[_build("gcc", "15.1", "1.fc44")])
        unknown = MagicMock()
        type(unknown).result = property(lambda self: (_ for _ in ()).throw(koji.GenericError()))
        multicall = hub.multicall.return_value.__enter__.return_value
        multicall.getLatestBuilds.side_effect = lambda tag, package: (
            found if package == "gcc" else unknown
        )
        client = KojiClient()

        client.prefetch_latest_builds(["gcc", "nope", "gcc"], "f44")

        assert multicall.getLatestBuilds.call_count == 2
        assert client.latest_build("gcc", "f44")["nvr"] == "gcc-15.1-1.fc44"
        assert client.latest_build("nope", "f44") is None
        hub.getLatestBuilds.assert_not_called()

    def test_prefetch_skips_cached(self, hub):
        hub.getLatestBuilds.return_value = []
        client = KojiClient()
        client.latest_build("gcc", "f44")

        client.prefetch_latest_builds(["gcc"], "f44")

        hub.multicall.assert_not_called()

    def test_prefetch_failure_falls_back(self, hub):
        hub.multicall.side_effect = OSError("Connection refused")
        hub.getLatestBuilds.return_value = []
        client = KojiClient()

        client.prefetch_latest_builds(["gcc"], "f44")

        assert client.latest_build("gcc", "f44") is None
        hub.getLatestBuilds.assert_called_once()

    def test_get_target(self, hub):
        hub.getBuildTarget.return_value = {
            "build_tag_name": "f44-build",
            "dest_tag_name": "f44-updates-candidate",
        }

        assert KojiClient().get_target("f44") == {
            "build_tag": "f44-build",
            "dest_tag": "f44-updates-candidate",
        }

    def test_get_target_missing(self, hub):
        hub.getBuildTarget.return_value = None

        assert KojiClient().get_target("nope") is None

    def test_has_external_repos(self, hub):
        hub.getExternalRepoList.return_value = [{"external_repo_name": "fedora-44"}]

        assert KojiClient().has_external_repos("f44-build") is True

    def test_has_no_external_repos(self, hub):
        hub.getExternalRepoList.return_value = []

        assert KojiClient().has_external_repos("f44-build") is False


class TestDependencyNode:
    def test_default_values(self):
        node = DependencyNode(name="test-pkg")

        assert node.name == "test-pkg"
        assert node.srpm_path is None
        assert node.package_info is None
        assert node.dependencies == []
        assert node.is_available is False
        assert node.build_order == -1

    def test_with_all_values(self):
        node = DependencyNode(
            name="test-pkg",
            srpm_path="/path/to/test.src.rpm",
            dependencies=["dep1", "dep2"],
            is_available=True,
            build_order=5,
        )

        assert node.name == "test-pkg"
        assert node.srpm_path == "/path/to/test.src.rpm"
        assert node.dependencies == ["dep1", "dep2"]
        assert node.is_available is True
        assert node.build_order == 5


class TestDependencyResolver:
    def test_default_initialization(self):
        resolver = DependencyResolver()

        assert resolver.koji is not None
        assert resolver.koji_tag == "fedora-build"
        assert resolver._available_packages is None
        assert resolver._dependency_graph == {}

    def test_custom_initialization(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="custom-tag")

        assert resolver.koji == mock_koji_client
        assert resolver.koji_tag == "custom-tag"

    def test_available_packages_lazy_load(self, mock_koji_client):
        mock_koji_client.list_packages.return_value = ["python3", "gcc", "make"]
        resolver = DependencyResolver(koji_client=mock_koji_client)

        result = resolver.available_packages

        assert "python3" in result
        assert "gcc" in result
        mock_koji_client.list_packages.assert_called_once()

    def test_available_packages_cached(self, mock_koji_client):
        mock_koji_client.list_packages.return_value = ["python3", "gcc"]
        resolver = DependencyResolver(koji_client=mock_koji_client)
        _ = resolver.available_packages
        _ = resolver.available_packages

        mock_koji_client.list_packages.assert_called_once()

    def test_refresh_available_packages(self, mock_koji_client):
        mock_koji_client.list_packages.return_value = ["python3"]
        resolver = DependencyResolver(koji_client=mock_koji_client)
        _ = resolver.available_packages
        resolver.refresh_available_packages()
        mock_koji_client.list_packages.return_value = ["python3", "gcc"]
        result = resolver.available_packages

        assert mock_koji_client.list_packages.call_count == 2
        assert "gcc" in result

    def test_find_missing_deps_all_available(self, mock_koji_client):
        mock_koji_client.list_packages.return_value = ["python3", "gcc", "make"]
        mock_koji_client.package_exists.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client)

        result = resolver.find_missing_deps(["python3", "gcc"])

        assert result == []

    def test_find_missing_deps_some_missing(self, mock_koji_client):
        mock_koji_client.list_packages.return_value = ["python3", "gcc"]
        mock_koji_client.package_exists.side_effect = lambda p, t: p != "missing-pkg"
        resolver = DependencyResolver(koji_client=mock_koji_client)

        result = resolver.find_missing_deps(["python3", "missing-pkg"])

        assert "missing-pkg" in result
        assert "python3" not in result

    def test_find_missing_deps_whitelisted_but_no_build(self, mock_koji_client):
        """Package in whitelist (list_packages) but without a build in our tag -> missing.

        Without this check vibebuild would consider such a package available (based
        on the whitelist) and skip it, which led to a silent pull from
        external_repo and an empty tagged_deps in the build summary.
        """
        mock_koji_client.list_packages.return_value = ["python-trustme", "gcc"]
        mock_koji_client.package_exists.return_value = True
        mock_koji_client.latest_build.side_effect = lambda pkg, tag: (
            None if pkg == "python-trustme" else {"nvr": f"{pkg}-1.0-1.fc42"}
        )
        resolver = DependencyResolver(koji_client=mock_koji_client)

        result = resolver.find_missing_deps(["python-trustme", "gcc"])

        assert "python-trustme" in result
        assert "gcc" not in result

    def test_find_missing_deps_with_build_requirement_objects(self, mock_koji_client):
        mock_koji_client.list_packages.return_value = ["gcc"]
        mock_koji_client.package_exists.side_effect = lambda p, t: p == "gcc"
        resolver = DependencyResolver(koji_client=mock_koji_client)
        deps = [
            BuildRequirement(name="gcc"),
            BuildRequirement(name="missing-dep", version="1.0", operator=">="),
        ]

        result = resolver.find_missing_deps(deps)

        assert "missing-dep" in result
        assert "gcc" not in result

    def test_build_dependency_graph_root_in_tag_is_still_analyzed(self, mock_koji_client):
        mock_koji_client.package_exists.side_effect = lambda p, t: p in ("root", "dep1")
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_get_requires:
            mock_get_requires.return_value = ["dep1", "dep2"]
            result = resolver.build_dependency_graph("root", "/path/to/root.src.rpm")

        assert result["root"].is_available is False
        assert result["root"].dependencies == ["dep2"]
        assert resolver.get_build_chain() == [["dep2"]]

    def test_build_dependency_graph_available_dependency(self, mock_koji_client):
        mock_koji_client.package_exists.side_effect = lambda p, t: p == "dep"
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_get_requires:
            mock_get_requires.return_value = ["dep"]
            result = resolver.build_dependency_graph("root", "/path/to/root.src.rpm")

        assert "dep" not in result
        assert resolver.get_build_chain() == []

    def test_get_build_chain_excludes_root(self, mock_koji_client):
        mock_koji_client.package_exists.return_value = False
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_get_requires:
            mock_get_requires.side_effect = lambda srpm: ["dep"] if "root" in srpm else []
            resolver.build_dependency_graph(
                "root", "/path/to/root.src.rpm", srpm_resolver=lambda pkg: f"/path/{pkg}.src.rpm"
            )

        assert resolver.get_build_chain() == [["dep"]]

    def test_build_dependency_graph_missing_package(self, mock_koji_client):
        mock_koji_client.package_exists.return_value = False
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_get_requires:
            mock_get_requires.return_value = []
            result = resolver.build_dependency_graph("missing-pkg", "/path/to/srpm")

        assert "missing-pkg" in result
        assert result["missing-pkg"].is_available is False
        assert result["missing-pkg"].srpm_path == "/path/to/srpm"

    def test_build_dependency_graph_with_deps(self, mock_koji_client):
        def package_exists_side_effect(pkg, tag):
            return pkg in ["dep1", "dep2"]

        mock_koji_client.package_exists.side_effect = package_exists_side_effect
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_get_requires:
            mock_get_requires.return_value = ["dep1", "dep2", "dep3"]
            result = resolver.build_dependency_graph(
                "main-pkg",
                "/path/to/main.src.rpm",
                srpm_resolver=lambda pkg: f"/path/to/{pkg}.src.rpm" if pkg == "dep3" else None,
            )

        assert "main-pkg" in result
        assert "dep3" in result
        assert result["dep3"].srpm_path == "/path/to/dep3.src.rpm"

    def test_topological_sort_empty_graph(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)

        result = resolver.topological_sort()

        assert result == []

    def test_topological_sort_single_package(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {"pkg-a": DependencyNode(name="pkg-a", is_available=False)}

        result = resolver.topological_sort()

        assert result == ["pkg-a"]

    def test_topological_sort_linear_deps(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(name="pkg-a", dependencies=["pkg-b"], is_available=False),
            "pkg-b": DependencyNode(name="pkg-b", dependencies=["pkg-c"], is_available=False),
            "pkg-c": DependencyNode(name="pkg-c", dependencies=[], is_available=False),
        }

        result = resolver.topological_sort()

        assert result.index("pkg-c") < result.index("pkg-b")
        assert result.index("pkg-b") < result.index("pkg-a")

    def test_topological_sort_skips_available(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(name="pkg-a", dependencies=["pkg-b"], is_available=False),
            "pkg-b": DependencyNode(name="pkg-b", is_available=True),
        }

        result = resolver.topological_sort()

        assert "pkg-a" in result
        assert "pkg-b" not in result

    def test_topological_sort_circular_dependency_raises(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(name="pkg-a", dependencies=["pkg-b"], is_available=False),
            "pkg-b": DependencyNode(name="pkg-b", dependencies=["pkg-a"], is_available=False),
        }

        with pytest.raises(CircularDependencyError, match="Circular dependency"):
            resolver.topological_sort()

    def test_get_build_chain_empty(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)

        result = resolver.get_build_chain()

        assert result == []

    def test_get_build_chain_groups_by_level(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "main": DependencyNode(name="main", dependencies=["dep1", "dep2"], is_available=False),
            "dep1": DependencyNode(name="dep1", dependencies=["base"], is_available=False),
            "dep2": DependencyNode(name="dep2", dependencies=["base"], is_available=False),
            "base": DependencyNode(name="base", dependencies=[], is_available=False),
        }

        result = resolver.get_build_chain()

        assert len(result) >= 2
        base_level = next(i for i, level in enumerate(result) if "base" in level)
        dep_level = next(i for i, level in enumerate(result) if "dep1" in level or "dep2" in level)
        main_level = next(i for i, level in enumerate(result) if "main" in level)
        assert base_level < dep_level < main_level

    def test_get_missing_packages(self, mock_koji_client):
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "with-srpm": DependencyNode(
                name="with-srpm", srpm_path="/path/to.src.rpm", is_available=False
            ),
            "no-srpm": DependencyNode(name="no-srpm", srpm_path=None, is_available=False),
            "available": DependencyNode(name="available", is_available=True),
        }

        result = resolver.get_missing_packages()

        assert "no-srpm" in result
        assert "with-srpm" not in result
        assert "available" not in result


class TestDependencyResolverWithNameResolver:
    def test_find_missing_deps_with_name_resolver(self, mock_koji_client):
        """find_missing_deps should use name_resolver to resolve names."""
        mock_nr = Mock()
        mock_nr.resolve.side_effect = lambda n: (
            "python3-requests" if n == "python3dist(requests)" else n
        )
        mock_koji_client.list_packages.return_value = ["python3-requests", "gcc"]
        mock_koji_client.package_exists.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, name_resolver=mock_nr)

        result = resolver.find_missing_deps(["python3dist(requests)"])

        assert result == []

    def test_find_missing_deps_resolved_different_original_available(self, mock_koji_client):
        """When resolved != name and original name is available in packages."""
        mock_nr = Mock()
        mock_nr.resolve.return_value = "resolved-name"
        mock_koji_client.list_packages.return_value = ["original-name"]
        mock_koji_client.package_exists.side_effect = lambda p, t: p == "original-name"
        resolver = DependencyResolver(koji_client=mock_koji_client, name_resolver=mock_nr)

        result = resolver.find_missing_deps(["original-name"])

        assert result == []

    def test_build_dependency_graph_with_name_resolver(self, mock_koji_client):
        """build_dependency_graph should use name_resolver."""
        mock_nr = Mock()
        mock_nr.resolve.side_effect = lambda n: n
        mock_nr.resolve_srpm_name.return_value = []
        mock_koji_client.package_exists.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, name_resolver=mock_nr)

        with patch("vibebuild.resolver.get_build_requires", return_value=["dep"]):
            result = resolver.build_dependency_graph("pkg", "/path/to/pkg.src.rpm")

        assert "pkg" in result
        mock_nr.resolve.assert_called_with("dep")


class TestResolveDeepsException:
    def test_resolve_deps_exception_sets_empty_deps(self, mock_koji_client):
        """resolve_deps exception should set dependencies=[]."""
        mock_koji_client.package_exists.return_value = False
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires", side_effect=Exception("parse error")):
            result = resolver.build_dependency_graph("pkg", "/path/to/pkg.src.rpm")

        assert "pkg" in result
        assert result["pkg"].dependencies == []


class TestGetBuildChainNodeNotFound:
    def test_get_build_chain_node_not_found(self, mock_koji_client):
        """get_build_chain should skip packages not in dependency_graph."""
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(name="pkg-a", dependencies=[], is_available=False),
        }

        result = resolver.get_build_chain()

        assert len(result) >= 1
        assert "pkg-a" in result[0]

    def test_get_build_chain_ghost_node_in_sort(self, mock_koji_client):
        """get_build_chain should skip nodes returned by topological_sort but missing from graph."""
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(name="pkg-a", dependencies=[], is_available=False),
        }
        with patch.object(resolver, "topological_sort", return_value=["pkg-a", "ghost-pkg"]):
            result = resolver.get_build_chain()

        assert len(result) >= 1
        assert "pkg-a" in result[0]


class TestFindMissingDepsOriginalNameFallback:
    def test_original_name_exists_in_koji(self, mock_koji_client):
        """When resolved != name, original name found via package_exists should not be missing."""
        mock_nr = Mock()
        mock_nr.resolve.return_value = "resolved-different"
        mock_koji_client.list_packages.return_value = []
        mock_koji_client.package_exists.side_effect = lambda p, t: p == "original-name"
        resolver = DependencyResolver(koji_client=mock_koji_client, name_resolver=mock_nr)

        result = resolver.find_missing_deps(["original-name"])

        assert result == []


class TestFindMissingDepsOriginalNameNotFound:
    def test_original_name_not_in_koji(self, mock_koji_client):
        """When resolved != name and original name NOT found, append to missing."""
        mock_nr = Mock()
        mock_nr.resolve.return_value = "resolved-different"
        mock_koji_client.list_packages.return_value = []
        mock_koji_client.package_exists.return_value = False
        resolver = DependencyResolver(koji_client=mock_koji_client, name_resolver=mock_nr)

        result = resolver.find_missing_deps(["original-name"])

        assert "resolved-different" in result


class TestBuildDependencyGraphEdgeCases:
    def test_dep_without_srpm(self, mock_koji_client):
        """Dependency with no SRPM should have empty deps."""
        mock_koji_client.package_exists.side_effect = lambda p, t: p not in ["root", "dep-no-srpm"]
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_reqs:
            mock_reqs.return_value = ["dep-no-srpm"]
            result = resolver.build_dependency_graph(
                "root", "/path/to/root.src.rpm", srpm_resolver=lambda pkg: None
            )

        assert "dep-no-srpm" in result
        assert result["dep-no-srpm"].srpm_path is None

    def test_no_srpm_resolver(self, mock_koji_client):
        """Without srpm_resolver, deps should get no SRPM path."""
        mock_koji_client.package_exists.side_effect = lambda p, t: p not in ["root", "dep"]
        resolver = DependencyResolver(koji_client=mock_koji_client)

        with patch("vibebuild.resolver.get_build_requires") as mock_reqs:
            mock_reqs.side_effect = lambda srpm: ["dep"] if "root" in srpm else []
            result = resolver.build_dependency_graph("root", "/path/to/root.src.rpm")

        assert "dep" in result
        assert result["dep"].srpm_path is None


class TestTopologicalSortEdgeCases:
    def test_dep_not_in_graph(self, mock_koji_client):
        """Dep reference not in graph should be handled."""
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(
                name="pkg-a", dependencies=["external-dep"], is_available=False
            ),
        }

        result = resolver.topological_sort()
        assert "pkg-a" in result


class TestGetBuildChainWithAvailableDep:
    def test_dep_in_levels_false(self, mock_koji_client):
        """get_build_chain: dep not in levels (available dep) should be skipped."""
        resolver = DependencyResolver(koji_client=mock_koji_client)
        resolver._dependency_graph = {
            "pkg-a": DependencyNode(name="pkg-a", dependencies=["avail-dep"], is_available=False),
            "avail-dep": DependencyNode(name="avail-dep", is_available=True),
        }

        result = resolver.get_build_chain()

        assert len(result) >= 1
        assert "pkg-a" in result[0]


class TestDependencyResolverExternalRepos:
    def test_check_external_repos_cached(self, mock_koji_client):
        """_check_external_repos should cache the result."""
        mock_koji_client.has_external_repos.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        assert resolver._check_external_repos() is True
        assert resolver._check_external_repos() is True
        mock_koji_client.has_external_repos.assert_called_once()

    def test_check_external_repos_exception(self, mock_koji_client):
        """_check_external_repos should return False on exception."""
        mock_koji_client.has_external_repos.side_effect = Exception("fail")
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        assert resolver._check_external_repos() is False

    def test_is_our_package_in_available(self, mock_koji_client):
        """_is_our_package should return True for packages in available_packages."""
        mock_koji_client.list_packages.return_value = ["my-pkg", "other-pkg"]
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        assert resolver._is_our_package("my-pkg") is True
        assert resolver._is_our_package("unknown-pkg") is False

    def test_is_our_package_with_name_resolver(self, mock_koji_client):
        """_is_our_package should try name_resolver for resolution."""
        mock_koji_client.list_packages.return_value = ["python3-requests"]
        mock_nr = Mock()
        mock_nr.resolve.return_value = "python3-requests"
        resolver = DependencyResolver(
            koji_client=mock_koji_client, koji_tag="f42-build", name_resolver=mock_nr
        )

        assert resolver._is_our_package("python3dist(requests)") is True

    def test_find_missing_deps_with_external_repos_skips_non_local(self, mock_koji_client):
        """With external repos, deps NOT in local Koji are assumed available."""
        # my-custom-pkg is registered but not built (package_exists returns False)
        mock_koji_client.list_packages.return_value = ["my-custom-pkg"]
        mock_koji_client.package_exists.return_value = False
        mock_koji_client.has_external_repos.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        result = resolver.find_missing_deps(["python3-devel", "gcc"])

        # python3-devel and gcc are NOT in our local Koji -> available via external repos
        assert "python3-devel" not in result
        assert "gcc" not in result

    def test_find_missing_deps_external_repos_registered_but_not_built(self, mock_koji_client):
        """Registered package not yet built is treated as available."""
        mock_koji_client.list_packages.return_value = ["my-custom-pkg"]
        mock_koji_client.package_exists.return_value = False
        mock_koji_client.has_external_repos.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        # my-custom-pkg is in available_packages (registered) so it's skipped early
        result = resolver.find_missing_deps(["my-custom-pkg"])
        assert "my-custom-pkg" not in result

    def test_find_missing_deps_without_external_repos(self, mock_koji_client):
        """Without external repos, all unbuilt deps are missing."""
        mock_koji_client.list_packages.return_value = []
        mock_koji_client.package_exists.return_value = False
        mock_koji_client.has_external_repos.return_value = False
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        result = resolver.find_missing_deps(["python3-devel"])

        assert "python3-devel" in result

    def test_find_missing_deps_external_repos_all_external(self, mock_koji_client):
        """With external repos and no local packages, nothing is missing."""
        mock_koji_client.list_packages.return_value = []
        mock_koji_client.package_exists.return_value = False
        mock_koji_client.has_external_repos.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        result = resolver.find_missing_deps(
            ["python3-devel", "python3-setuptools", "python3-pip", "gcc"]
        )

        assert result == []


class TestVersionConstraints:
    """Tests for the version check in find_missing_deps."""

    @pytest.mark.parametrize(
        "actual,required_op,required_ver,expected_missing",
        [
            ("1.17.0", ">=", "1.12", False),
            ("1.10.0", ">=", "1.12", True),
            ("1.12.0", ">=", "1.12", False),
            ("2.0", ">", "1.99", False),
            ("1.99", ">", "1.99", True),
            ("1.12", "=", "1.12", False),
            ("1.13", "=", "1.12", True),
            ("1.0", "<", "1.5", False),
            ("2.0", "<", "1.5", True),
            ("3.0", "<=", "3.0", False),
            ("3.1", "<=", "3.0", True),
        ],
    )
    def test_find_missing_deps_version_check(
        self, mock_koji_client, actual, required_op, required_ver, expected_missing
    ):
        mock_koji_client.list_packages.return_value = ["python3-cffi"]
        mock_koji_client.package_exists.return_value = True
        mock_koji_client.latest_build.return_value = {
            "nvr": f"python3-cffi-{actual}-1.fc42",
            "version": actual,
            "release": "1.fc42",
        }
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")
        dep = BuildRequirement(name="python3-cffi", version=required_ver, operator=required_op)

        result = resolver.find_missing_deps([dep])

        if expected_missing:
            assert "python3-cffi" in result
        else:
            assert "python3-cffi" not in result

    def test_no_constraint_with_existing_build_skips(self, mock_koji_client):
        """Without operator/version: whitelisted + build exists -> available, not missing."""
        mock_koji_client.list_packages.return_value = ["gcc"]
        mock_koji_client.package_exists.return_value = True
        mock_koji_client.latest_build.return_value = {"nvr": "gcc-13.0-1.fc42"}
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")

        result = resolver.find_missing_deps([BuildRequirement(name="gcc")])

        assert result == []
        mock_koji_client.latest_build.assert_called_once_with("gcc", "f42-build")

    def test_latest_build_none_forces_local_build(self, mock_koji_client):
        """Whitelisted + latest_build=None -> must be marked as missing.

        Contract: a package in the whitelist without a build is ours, waiting to be built, not
        available externally. The version constraint does not matter here,
        the absence of a build takes precedence.
        """
        mock_koji_client.list_packages.return_value = ["python3-cffi"]
        mock_koji_client.package_exists.return_value = True
        mock_koji_client.latest_build.return_value = None
        resolver = DependencyResolver(koji_client=mock_koji_client, koji_tag="f42-build")
        dep = BuildRequirement(name="python3-cffi", version="1.12", operator=">=")

        assert "python3-cffi" in resolver.find_missing_deps([dep])


class TestFindMissingDepsPrefetch:
    def test_prefetches_all_candidates_once(self, mock_koji_client):
        mock_nr = Mock()
        mock_nr.resolve.side_effect = lambda n: "python3-foo" if n == "python3dist(foo)" else n
        mock_nr.resolve_srpm_name.side_effect = lambda n: [f"src-{n}"]
        mock_koji_client.list_packages.return_value = []
        mock_koji_client.has_external_repos.return_value = True
        resolver = DependencyResolver(koji_client=mock_koji_client, name_resolver=mock_nr)

        resolver.find_missing_deps([BuildRequirement(name="python3dist(foo)"), "gcc"])

        names, tag = mock_koji_client.prefetch_latest_builds.call_args.args
        assert tag == "fedora-build"
        assert set(names) == {
            "python3dist(foo)",
            "python3-foo",
            "src-python3-foo",
            "gcc",
            "src-gcc",
        }
