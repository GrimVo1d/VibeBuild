"""Tests for vibebuild.name_resolver module."""

import pytest
from unittest.mock import Mock, patch

from vibebuild.name_resolver import (
    PROVIDE_PATTERNS,
    SYSTEM_MACROS,
    PackageNameResolver,
)


class TestPackageNameResolverVirtualProvides:
    """Test resolution of virtual RPM provides to real package names."""

    def test_resolve_python3dist(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("python3dist(requests)")

        assert result == "python3-requests"

    def test_resolve_python2dist(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("python2dist(six)")

        assert result == "python2-six"

    def test_resolve_pythondist_no_version(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("pythondist(setuptools)")

        assert result == "python3-setuptools"

    def test_resolve_pkgconfig(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("pkgconfig(glib-2.0)")

        assert result == "glib-2.0-devel"

    def test_resolve_perl(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("perl(File::Path)")

        assert result == "perl-File-Path"

    def test_resolve_rubygem(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("rubygem(bundler)")

        assert result == "rubygem-bundler"

    def test_resolve_npm(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("npm(typescript)")

        assert result == "nodejs-typescript"

    def test_resolve_golang(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("golang(github.com/foo/bar)")

        assert result == "golang-github.com-foo-bar"

    def test_resolve_tex(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("tex(latex)")

        assert result == "texlive-latex"

    def test_resolve_mvn(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("mvn(org.apache:commons-lang)")

        assert result == "commons-lang"

    def test_resolve_cmake(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("cmake(KF5CoreAddons)")

        assert result == "cmake-kf5coreaddons"


class TestPackageNameResolverMacros:
    """Test RPM macro expansion."""

    def test_expand_macros_python3_pkgversion(self):
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{python3_pkgversion}-devel")

        assert result == "3-devel"

    def test_expand_macros_conditional(self):
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{?python3_pkgversion}")

        assert result == "3"

    def test_expand_macros_unknown_conditional(self):
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{?unknown_macro}")

        assert result == ""

    def test_expand_macros_unknown_regular(self):
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{unknown_macro}")

        assert result == "%{unknown_macro}"

    def test_expand_macros_no_macros(self):
        resolver = PackageNameResolver()

        result = resolver.expand_macros("gcc")

        assert result == "gcc"

    def test_expand_macros_multiple(self):
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{_bindir}/python%{python3_pkgversion}")

        assert result == "/usr/bin/python3"


class TestPackageNameResolverPlainNames:
    """Test that plain package names pass through unchanged."""

    def test_resolve_plain_name(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("gcc")

        assert result == "gcc"

    def test_resolve_plain_name_with_version_suffix(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("python3-devel")

        assert result == "python3-devel"

    def test_resolve_empty_string(self):
        resolver = PackageNameResolver()

        result = resolver.resolve("")

        assert result == ""


class TestPackageNameResolverSRPMNames:
    """Test mapping RPM names to SRPM names."""

    def test_resolve_srpm_name_python3(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("python3-requests")

        assert result == ["python-requests", "python3-requests"]

    def test_resolve_srpm_name_python2(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("python2-six")

        assert result == ["python-six", "python2-six"]

    def test_resolve_srpm_name_devel(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("glib2-devel")

        assert result == ["glib2", "glib2-devel"]

    def test_resolve_srpm_name_libs(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("glib2-libs")

        assert result == ["glib2", "glib2-libs"]

    def test_resolve_srpm_name_perl(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("perl-File-Path")

        assert result == ["perl-File-Path"]

    def test_resolve_srpm_name_plain(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("gcc")

        assert result == ["gcc"]

    def test_resolve_srpm_name_rubygem(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("rubygem-bundler")

        assert result == ["rubygem-bundler"]

    def test_resolve_srpm_name_nodejs(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("nodejs-typescript")

        assert result == ["nodejs-typescript"]

    def test_resolve_srpm_name_golang(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_srpm_name("golang-github.com-foo-bar")

        assert result == ["golang-github.com-foo-bar"]


class TestPackageNameResolverCaching:
    """Test that caching works correctly."""

    def test_caching_returns_same_result(self):
        resolver = PackageNameResolver()

        result1 = resolver.resolve("python3dist(requests)")
        result2 = resolver.resolve("python3dist(requests)")

        assert result1 == result2
        assert result1 == "python3-requests"

    def test_caching_populates_cache(self):
        resolver = PackageNameResolver()

        resolver.resolve("python3dist(requests)")

        assert "python3dist(requests)" in resolver._cache
        assert resolver._cache["python3dist(requests)"] == "python3-requests"

    def test_cache_hit_avoids_recomputation(self):
        resolver = PackageNameResolver()
        # Pre-populate cache with a custom value
        resolver._cache["custom-dep"] = "custom-resolved"

        result = resolver.resolve("custom-dep")

        assert result == "custom-resolved"


class TestPackageNameResolverMLFallback:
    """Test ML fallback behavior."""

    def test_no_ml_fallback_returns_original(self):
        resolver = PackageNameResolver(ml_resolver=None)

        # An unrecognized virtual provide should return as-is
        result = resolver.resolve("unknown_provider(something)")

        assert result == "unknown_provider(something)"

    def test_ml_fallback_called_for_unresolved_virtual_provide(self):
        mock_ml = Mock()
        mock_ml.predict.return_value = "resolved-package"
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("custom_provider(something)")

        assert result == "resolved-package"
        mock_ml.predict.assert_called_once_with("custom_provider(something)")

    def test_ml_fallback_not_called_when_rules_match(self):
        mock_ml = Mock()
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("python3dist(requests)")

        assert result == "python3-requests"
        mock_ml.predict.assert_not_called()

    def test_ml_fallback_not_called_for_plain_names(self):
        mock_ml = Mock()
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("gcc")

        assert result == "gcc"
        mock_ml.predict.assert_not_called()

    def test_ml_fallback_exception_handled_gracefully(self):
        mock_ml = Mock()
        mock_ml.predict.side_effect = RuntimeError("ML model error")
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("custom_provider(something)")

        # Should return the expanded name, not raise
        assert result == "custom_provider(something)"

    def test_ml_fallback_returns_none_falls_through(self):
        mock_ml = Mock()
        mock_ml.predict.return_value = None
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("custom_provider(something)")

        assert result == "custom_provider(something)"

    def test_ml_fallback_returns_same_name_falls_through(self):
        mock_ml = Mock()
        mock_ml.predict.return_value = "custom_provider(something)"
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("custom_provider(something)")

        assert result == "custom_provider(something)"


class TestSystemMacros:
    """Test that SYSTEM_MACROS contains expected entries."""

    def test_python3_pkgversion(self):
        assert SYSTEM_MACROS["python3_pkgversion"] == "3"

    def test_python3_version(self):
        assert SYSTEM_MACROS["python3_version"] == "3.12"

    def test_prefix(self):
        assert SYSTEM_MACROS["_prefix"] == "/usr"

    def test_bindir(self):
        assert SYSTEM_MACROS["_bindir"] == "/usr/bin"

    def test_libdir(self):
        assert SYSTEM_MACROS["_libdir"] == "/usr/lib64"


class TestProvidePatterns:
    """Test that PROVIDE_PATTERNS list is well-formed."""

    def test_patterns_are_compiled(self):
        for pattern, fn in PROVIDE_PATTERNS:
            assert hasattr(pattern, "match"), f"Pattern {pattern} is not compiled"

    def test_patterns_count(self):
        assert len(PROVIDE_PATTERNS) >= 9


class TestResolveVirtualProvide:
    """Test resolve_virtual_provide directly."""

    def test_returns_none_for_plain_name(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_virtual_provide("gcc")

        assert result is None

    def test_returns_none_for_unknown_provider(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_virtual_provide("custom(something)")

        assert result is None

    def test_returns_resolved_for_known_provider(self):
        resolver = PackageNameResolver()

        result = resolver.resolve_virtual_provide("python3dist(flask)")

        assert result == "python3-flask"


class TestExpandMacrosWithDefault:
    """Test conditional macros with default values like %{?macro:default}."""

    def test_expand_known_macro_with_default(self):
        """Known macro with default should use macro value, not default."""
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{?python3_pkgversion:2}")

        assert result == "3"

    def test_expand_unknown_macro_with_default(self):
        """Unknown macro with default should use default value."""
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{?nonexistent_macro:fallback_value}")

        assert result == "fallback_value"

    def test_expand_macro_default_empty(self):
        """Macro with colon but empty default."""
        resolver = PackageNameResolver()

        result = resolver.expand_macros("%{?nonexistent:}")

        assert result == ""


class TestGetDownloadCandidates:
    """Test get_download_candidates for full coverage."""

    def test_plain_name_no_resolver(self):
        """Plain name without ML resolver."""
        resolver = PackageNameResolver(ml_resolver=None)

        result = resolver.get_download_candidates("gcc")

        assert "gcc" in result

    def test_python3_name_generates_variants(self):
        """python3-X should generate python-X and python3-X variants."""
        resolver = PackageNameResolver(ml_resolver=None)

        result = resolver.get_download_candidates("python3-requests")

        assert "python-requests" in result
        assert "python3-requests" in result

    def test_with_ml_resolver_dict_result(self):
        """ML resolver returning dict with srpm_name and rpm_name."""
        mock_ml = Mock()
        mock_ml.predict.return_value = {"srpm_name": "python-requests", "rpm_name": "python3-requests"}
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.get_download_candidates("python3dist(requests)")

        assert "python-requests" in result
        assert "python3-requests" in result

    def test_with_ml_resolver_exception(self):
        """ML resolver exception should be caught silently."""
        mock_ml = Mock()
        mock_ml.predict.side_effect = RuntimeError("ML error")
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.get_download_candidates("gcc")

        assert "gcc" in result

    def test_with_ml_resolver_returns_none(self):
        """ML resolver returning None should be handled."""
        mock_ml = Mock()
        mock_ml.predict.return_value = None
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.get_download_candidates("gcc")

        assert "gcc" in result

    def test_deduplication(self):
        """Candidates should be deduplicated."""
        resolver = PackageNameResolver(ml_resolver=None)

        result = resolver.get_download_candidates("gcc")

        assert len(result) == len(set(result))

    def test_resolved_different_from_name_adds_srpm_variants(self):
        """When resolved != name, srpm variants of resolved should be added."""
        resolver = PackageNameResolver(ml_resolver=None)

        result = resolver.get_download_candidates("python3dist(requests)")

        # resolved will be python3-requests
        assert "python3-requests" in result
        # srpm variant: python-requests
        assert "python-requests" in result

    def test_ml_resolver_dict_with_no_srpm(self):
        """ML resolver dict with None srpm_name should skip it."""
        mock_ml = Mock()
        mock_ml.predict.return_value = {"srpm_name": None, "rpm_name": "python3-requests"}
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.get_download_candidates("python3dist(requests)")

        assert "python3-requests" in result

    def test_ml_resolver_dict_rpm_same_as_srpm(self):
        """ML resolver dict with same srpm_name and rpm_name should deduplicate."""
        mock_ml = Mock()
        mock_ml.predict.return_value = {"srpm_name": "same-pkg", "rpm_name": "same-pkg"}
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.get_download_candidates("some-pkg")

        assert result.count("same-pkg") == 1

    def test_ml_resolver_dict_with_no_rpm(self):
        """ML resolver dict with None rpm_name should skip it."""
        mock_ml = Mock()
        mock_ml.predict.return_value = {"srpm_name": "python-requests", "rpm_name": None}
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.get_download_candidates("python3dist(requests)")

        assert "python-requests" in result


class TestMLFallbackDictResult:
    """Test ML fallback returning dict with rpm_name."""

    def test_ml_returns_dict_with_rpm_name(self):
        """ML resolver returning dict should use rpm_name."""
        mock_ml = Mock()
        mock_ml.predict.return_value = {"rpm_name": "resolved-pkg", "srpm_name": "resolved-src"}
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("custom_provider(something)")

        assert result == "resolved-pkg"

    def test_ml_returns_dict_with_same_rpm_name_falls_through(self):
        """ML dict with rpm_name same as input should fall through."""
        mock_ml = Mock()
        mock_ml.predict.return_value = {"rpm_name": "custom_provider(something)", "srpm_name": "src"}
        resolver = PackageNameResolver(ml_resolver=mock_ml)

        result = resolver.resolve("custom_provider(something)")

        assert result == "custom_provider(something)"
