"""
Rule-based package name resolver with optional ML fallback.

Resolves virtual RPM dependency names (like python3dist(requests), pkgconfig(glib-2.0),
perl(File::Path)) to real RPM package names, and maps RPM names to possible SRPM names.
"""

import logging
import re
from typing import Optional

from vibebuild.exceptions import NameResolutionError

logger = logging.getLogger(__name__)

# Try to import ML resolver; it may not be installed
try:
    from vibebuild.ml_resolver import MLPackageResolver

    HAS_ML = True
except ImportError:  # pragma: no cover
    HAS_ML = False

# Known RPM system macros for expanding %{...} in dependency names
SYSTEM_MACROS: dict[str, str] = {
    "python3_pkgversion": "3",
    "python3_version": "3.12",
    "python3_version_nodots": "312",
    "__python3": "/usr/bin/python3",
    "python3_sitelib": "/usr/lib/python3.12/site-packages",
    "python3_sitearch": "/usr/lib64/python3.12/site-packages",
    "lua_version": "5.4",
    "ruby_version": "3.2",
    "_prefix": "/usr",
    "_bindir": "/usr/bin",
    "_libdir": "/usr/lib64",
    "_includedir": "/usr/include",
    "_datadir": "/usr/share",
    "_sysconfdir": "/etc",
    "_mandir": "/usr/share/man",
    "_infodir": "/usr/share/info",
    "_localstatedir": "/var",
    "_sharedstatedir": "/var/lib",
}

# Regex patterns for resolving virtual RPM provides to real package names.
# Each entry is (compiled_regex, replacement_function).
PROVIDE_PATTERNS: list[tuple[re.Pattern, callable]] = [
    (
        re.compile(r"^python(\d*)dist\((.+)\)$"),
        lambda m: f"python{m.group(1) or '3'}-{m.group(2)}",
    ),
    (
        re.compile(r"^pkgconfig\((.+)\)$"),
        lambda m: f"{m.group(1)}-devel",
    ),
    (
        re.compile(r"^perl\((.+)\)$"),
        lambda m: f"perl-{m.group(1).replace('::', '-')}",
    ),
    (
        re.compile(r"^rubygem\((.+)\)$"),
        lambda m: f"rubygem-{m.group(1)}",
    ),
    (
        re.compile(r"^npm\((.+)\)$"),
        lambda m: f"nodejs-{m.group(1)}",
    ),
    (
        re.compile(r"^cmake\((.+)\)$"),
        lambda m: f"cmake-{m.group(1).lower()}",
    ),
    (
        re.compile(r"^tex\((.+)\)$"),
        lambda m: f"texlive-{m.group(1)}",
    ),
    (
        re.compile(r"^golang\((.+)\)$"),
        lambda m: f"golang-{m.group(1).replace('/', '-')}",
    ),
    (
        re.compile(r"^mvn\(([^:]+):([^:]+)\)$"),
        lambda m: m.group(2),
    ),
]

# Known RPM subpackages that come from a different SRPM than their name suggests.
# Maps binary RPM name -> SRPM name.
# This avoids futile download attempts for packages that don't have their own SRPM.
SUBPACKAGE_TO_SRPM: dict[str, str] = {
    # ---- perl core modules (subpackages of the 'perl' SRPM) ----
    # ---- perl-List-Util -> perl-Scalar-List-Utils ----
    # ---- python3 subpackages -> python3.NN ----
    # ---- gcc subpackages ----
    # ---- glibc subpackages ----
    # ---- systemtap subpackages ----
    # ---- zlib (replaced by zlib-ng in modern Fedora) ----
    # ---- groff subpackages ----
    # ---- procps (renamed to procps-ng) ----
    # ---- coreutils subpackages ----
    # ---- util-linux subpackages ----
    # ---- openssl subpackages ----
    # ---- krb5 subpackages ----
    # ---- binutils subpackages ----
    # ---- xz subpackages ----
    # ---- bzip2 subpackages ----
    # ---- zstd subpackages ----
    # ---- attr subpackages ----
    # ---- acl subpackages ----
    # ---- gtest subpackages ----
    # ---- atk -> at-spi2-core (renamed in modern Fedora) ----
    # ---- wget -> wget2 (renamed in modern Fedora) ----
    # ---- rust subpackages ----
    # ---- rust-bindgen ----
    # ---- python3 -> python3.NN ----
    # ---- emacs subpackages ----
    # ---- curl subpackages ----
    # ---- pcre2 subpackages ----
    # ---- libffi subpackages ----
    # ---- readline subpackages ----
    # ---- sqlite subpackages ----
    # ---- expat subpackages ----
    # ---- libxml2 subpackages ----
    # ---- libxslt subpackages ----
    # ---- mesa subpackages ----
    # ---- nodejs -> versioned ----
    # ---- libpng subpackages ----
    # ---- freetype subpackages ----
    # ---- fontconfig subpackages ----
    # ---- pango subpackages ----
    # ---- cairo subpackages ----
    # ---- gdk-pixbuf2 subpackages ----
    # ---- gtk3 subpackages ----
    # ---- glib2 subpackages ----
    # ---- dbus subpackages ----
    # ---- systemd subpackages ----
    # ---- libcap subpackages ----
}

# Macro pattern for matching %{macro_name} or %{?macro_name}
_MACRO_PATTERN = re.compile(r"%\{([^}]+)\}")


class PackageNameResolver:
    """
    Resolves RPM dependency names to real package names using rule-based
    pattern matching with optional ML fallback.

    Pipeline: cache -> expand macros -> apply virtual provide patterns -> ML fallback -> original
    """

    def __init__(self, ml_resolver=None):
        """
        Initialize the resolver.

        Args:
            ml_resolver: Optional ML-based resolver instance. If provided and available,
                         it will be used as a fallback when rule-based resolution fails.
        """
        self._cache: dict[str, str] = {}
        self.ml_resolver = ml_resolver

    @staticmethod
    def _strip_rich_dep(name: str) -> str:
        """
        Extract the primary package name from an RPM rich/boolean dependency.

        Handles patterns like:
          (python3dist(tomli) if python3-devel < 3.11) -> python3dist(tomli)
          (pkg1 or pkg2)  -> pkg1
          (pkg1 and pkg2) -> pkg1
        """
        s = name.strip()
        # Strip outer parentheses of boolean dep expressions
        if s.startswith("(") and (" if " in s or " or " in s or " and " in s
                                   or " unless " in s or " with " in s
                                   or " without " in s):
            s = s.lstrip("(").rstrip(")")
            # Take the first token before the boolean operator
            for keyword in (" if ", " unless ", " or ", " and ", " with ", " without "):
                if keyword in s:
                    s = s.split(keyword)[0].strip()
                    break
            # Strip any trailing version comparison from the extracted name
            s = re.split(r"\s*[><=!]+\s*", s)[0].strip()
        return s

    def resolve(self, dep_name: str) -> str:
        """
        Resolve a dependency name to a real RPM package name.

        Pipeline order:
        1. Check cache
        2. Strip rich/boolean dependency syntax
        3. Expand RPM macros
        4. Try virtual provide pattern matching
        5. ML fallback (if available)
        6. Return expanded name as-is

        Args:
            dep_name: The dependency name from a spec file (e.g. "python3dist(requests)")

        Returns:
            Resolved RPM package name (e.g. "python3-requests")
        """
        if not dep_name:
            return dep_name

        # Check cache first
        if dep_name in self._cache:
            return self._cache[dep_name]

        # Step 0: Strip rich/boolean dependency syntax
        dep_name_clean = self._strip_rich_dep(dep_name)

        # Step 1: Expand macros
        expanded = self.expand_macros(dep_name_clean)

        # Step 2: Try virtual provide patterns
        resolved = self.resolve_virtual_provide(expanded)
        if resolved is not None:
            self._cache[dep_name] = resolved
            return resolved

        # Step 3: ML fallback if available and name contains parentheses
        # (indicating an unresolved virtual provide)
        if self.ml_resolver and "(" in expanded:
            try:
                ml_result = self.ml_resolver.predict(expanded)
                if ml_result:
                    rpm_name = (
                        ml_result.get("rpm_name", expanded)
                        if isinstance(ml_result, dict)
                        else ml_result
                    )
                    if rpm_name != expanded:
                        logger.debug("ML resolved '%s' -> '%s'", expanded, rpm_name)
                        self._cache[dep_name] = rpm_name
                        return rpm_name
            except Exception as e:
                logger.debug("ML resolver failed for '%s': %s", expanded, e)

        # Step 4: Return expanded name as-is
        self._cache[dep_name] = expanded
        return expanded

    def expand_macros(self, name: str) -> str:
        """
        Expand RPM macros in a dependency name using SYSTEM_MACROS.

        Handles %{macro}, %{?macro} (conditional), and bare %macro patterns.

        Args:
            name: Name potentially containing RPM macros

        Returns:
            Name with known macros expanded
        """
        if "%" not in name:
            return name

        def replace_macro(match: re.Match) -> str:
            macro_expr = match.group(1)
            # Handle conditional macros like %{?python3_pkgversion}
            if macro_expr.startswith("?"):
                macro_name = macro_expr[1:]
                # Handle macros with default values like %{?macro:default}
                if ":" in macro_name:
                    parts = macro_name.split(":", 1)
                    macro_name = parts[0]
                    default = parts[1]
                    return SYSTEM_MACROS.get(macro_name, default)
                # Conditional: if defined, expand; otherwise empty string
                return SYSTEM_MACROS.get(macro_name, "")
            return SYSTEM_MACROS.get(macro_expr, match.group(0))

        return _MACRO_PATTERN.sub(replace_macro, name)

    def resolve_virtual_provide(self, name: str) -> Optional[str]:
        """
        Try to resolve a virtual provide name using PROVIDE_PATTERNS.

        Args:
            name: Dependency name that may be a virtual provide
                  (e.g. "python3dist(requests)", "pkgconfig(glib-2.0)")

        Returns:
            Resolved package name, or None if no pattern matched
        """
        for pattern, resolver_fn in PROVIDE_PATTERNS:
            match = pattern.match(name)
            if match:
                return resolver_fn(match)
        return None

    def resolve_srpm_name(self, rpm_name: str) -> list[str]:
        """
        Map an RPM binary package name to possible SRPM (source package) names.

        Many RPM binary packages have different SRPM names. For example:
        - python3-requests (RPM) -> python-requests (SRPM)
        - glib2-devel (RPM) -> glib2 (SRPM)

        Args:
            rpm_name: RPM binary package name

        Returns:
            List of possible SRPM names, ordered by likelihood
        """
        candidates = []

        # Check known subpackage-to-SRPM mapping first
        if rpm_name in SUBPACKAGE_TO_SRPM:
            srpm = SUBPACKAGE_TO_SRPM[rpm_name]
            # Also keep original name as fallback

        # Rule: python3-X -> try python-X first, then python3-X

        # Rule: python2-X -> try python-X first

        # Rule: *-devel -> try without -devel suffix

        # Rule: *-libs -> try without -libs suffix

        # Rule: *-common -> try without -common suffix

        # Rule: *-base -> try without -base suffix

        # Rule: *-static -> try without -static suffix

        # Rule: *-langpack-XX -> try base package

        # Rule: perl-X -> same name (SRPM usually matches)

        # Rule: rubygem-X -> rubygem-X (SRPM usually matches)

        # Rule: nodejs-X -> nodejs-X

        # Rule: golang-X -> golang-X

        # Default: use the name as-is

        # Deduplicate while preserving order (insurance for future patterns)






        # Strip rich dependency syntax before processing

        # 0. Known subpackage mapping — highest priority (avoid futile lookups)
            srpm = SUBPACKAGE_TO_SRPM[name]

        # 1. ML prediction (for aliases like python3 and virtual provides)

        # 2. Rule-based resolve (virtual provides)

        # 3. SRPM name variants for the given name

        # 4. SRPM name variants for the rule-resolved name (if different)

        # 5. Original name (safety net; resolve() always adds name to candidates)
