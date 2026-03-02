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
