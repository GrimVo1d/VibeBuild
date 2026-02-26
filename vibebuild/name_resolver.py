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
    "perl-Attribute-Handlers": "perl",
    "perl-AutoLoader": "perl",
    "perl-AutoSplit": "perl",
    "perl-B": "perl",
    "perl-base": "perl",
    "perl-Benchmark": "perl",
    "perl-blib": "perl",
    "perl-Class-Struct": "perl",
    "perl-Config": "perl",
    "perl-Config-Extensions": "perl",
    "perl-Cwd": "perl",
    "perl-DBM_Filter": "perl",
    "perl-debugger": "perl",
    "perl-deprecate": "perl",
    "perl-Devel-Peek": "perl",
    "perl-Devel-SelfStubber": "perl",
    "perl-diagnostics": "perl",
    "perl-DirHandle": "perl",
    "perl-Dumpvalue": "perl",
    "perl-DynaLoader": "perl",
    "perl-English": "perl",
    "perl-Errno": "perl",
    "perl-ExtUtils-Constant": "perl",
    "perl-ExtUtils-Embed": "perl",
    "perl-ExtUtils-Miniperl": "perl",
    "perl-Fcntl": "perl",
    "perl-fields": "perl",
    "perl-File-Basename": "perl",
    "perl-File-Compare": "perl",
    "perl-File-Copy": "perl",
    "perl-File-DosGlob": "perl",
    "perl-File-Find": "perl",
    "perl-File-stat": "perl",
    "perl-File-Spec": "perl",
    "perl-File-Spec-Functions": "perl",
    "perl-FileCache": "perl",
    "perl-FileHandle": "perl",
    "perl-filetest": "perl",
    "perl-FindBin": "perl",
    "perl-GDBM_File": "perl",
    "perl-Getopt-Std": "perl",
    "perl-Hash-Util": "perl",
    "perl-Hash-Util-FieldHash": "perl",
    "perl-I18N-Collate": "perl",
    "perl-I18N-LangTags": "perl",
    "perl-I18N-Langinfo": "perl",
    "perl-if": "perl",
    "perl-interpreter": "perl",
    "perl-IO": "perl",
    "perl-IO-Handle": "perl",
    "perl-IPC-Open3": "perl",
    "perl-less": "perl",
    "perl-lib": "perl",
    "perl-libs": "perl",
    "perl-locale": "perl",
    "perl-Locale-Maketext-Simple": "perl",
    "perl-macros": "perl",
    "perl-Math-Complex": "perl",
    "perl-Memoize": "perl",
    "perl-meta-notation": "perl",
    "perl-Module-Loaded": "perl",
    "perl-mro": "perl",
    "perl-NDBM_File": "perl",
    "perl-Net": "perl",
    "perl-NEXT": "perl",
    "perl-ODBM_File": "perl",
    "perl-Opcode": "perl",
    "perl-open": "perl",
    "perl-overload": "perl",
    "perl-overloading": "perl",
    "perl-Pod-Functions": "perl",
    "perl-Pod-Html": "perl",
    "perl-POSIX": "perl",
    "perl-Safe": "perl",
    "perl-Search-Dict": "perl",
    "perl-SelectSaver": "perl",
    "perl-SelfLoader": "perl",
    "perl-sigtrap": "perl",
    "perl-sort": "perl",
    "perl-strict": "perl",
    "perl-subs": "perl",
    "perl-Symbol": "perl",
    "perl-Sys-Hostname": "perl",
    "perl-Term-Complete": "perl",
    "perl-Term-ReadLine": "perl",
    "perl-Test": "perl",
    "perl-Test-More": "perl",
    "perl-Text-Abbrev": "perl",
    "perl-Thread": "perl",
    "perl-Thread-Semaphore": "perl",
    "perl-Tie": "perl",
    "perl-Tie-File": "perl",
    "perl-Tie-Memoize": "perl",
    "perl-Time": "perl",
    "perl-Time-Piece": "perl",
    "perl-Unicode-UCD": "perl",
    "perl-User-pwent": "perl",
    "perl-autouse": "perl",
    "perl-vars": "perl",
    "perl-vmsish": "perl",
    "perl-warnings": "perl",
    # ---- perl-List-Util -> perl-Scalar-List-Utils ----
    "perl-List-Util": "perl-Scalar-List-Utils",
    # ---- python3 subpackages -> python3.NN ----
    "python3-libs": "python3.13",
    "python3-devel": "python3.13",
    "python3-idle": "python3.13",
    "python3-tkinter": "python3.13",
    "python3-test": "python3.13",
    # ---- gcc subpackages ----
    "gcc-c++": "gcc",
    "gcc-gfortran": "gcc",
    "gcc-objc": "gcc",
    "gcc-objc++": "gcc",
    "gcc-gdb-plugin": "gcc",
    "gcc-plugin-devel": "gcc",
    "libgcc": "gcc",
    "libstdc++": "gcc",
    "libstdc++-devel": "gcc",
    "libstdc++-static": "gcc",
    "libgomp": "gcc",
    "libatomic": "gcc",
    "libitm": "gcc",
    "libasan": "gcc",
    "libtsan": "gcc",
    "libubsan": "gcc",
    "liblsan": "gcc",
    # ---- glibc subpackages ----
    "glibc-common": "glibc",
    "glibc-devel": "glibc",
    "glibc-headers": "glibc",
    "glibc-static": "glibc",
    "glibc-utils": "glibc",
    "glibc-langpack-en": "glibc",
    "glibc-langpack-tr": "glibc",
    "glibc-locale-source": "glibc",
    "glibc-minimal-langpack": "glibc",
    "glibc-all-langpacks": "glibc",
    # ---- systemtap subpackages ----
    "systemtap-sdt-devel": "systemtap",
    "systemtap-sdt-dtrace": "systemtap",
    "systemtap-devel": "systemtap",
    "systemtap-client": "systemtap",
    "systemtap-server": "systemtap",
    "systemtap-runtime": "systemtap",
    # ---- zlib (replaced by zlib-ng in modern Fedora) ----
    "zlib": "zlib-ng",
    "zlib-devel": "zlib-ng",
    "zlib-static": "zlib-ng",
    # ---- groff subpackages ----
    "groff-base": "groff",
    "groff-perl": "groff",
    # ---- procps (renamed to procps-ng) ----
    "procps": "procps-ng",
    # ---- coreutils subpackages ----
    "coreutils-common": "coreutils",
    "coreutils-single": "coreutils",
    # ---- util-linux subpackages ----
    "util-linux-core": "util-linux",
    "libblkid": "util-linux",
    "libblkid-devel": "util-linux",
    "libmount": "util-linux",
    "libmount-devel": "util-linux",
    "libuuid": "util-linux",
    "libuuid-devel": "util-linux",
    "libfdisk": "util-linux",
    "libfdisk-devel": "util-linux",
    "libsmartcols": "util-linux",
    "libsmartcols-devel": "util-linux",
    # ---- openssl subpackages ----
    "openssl-libs": "openssl",
    "openssl-devel": "openssl",
    # ---- krb5 subpackages ----
    "krb5-libs": "krb5",
    "krb5-devel": "krb5",
    "krb5-server": "krb5",
    "krb5-workstation": "krb5",
    # ---- binutils subpackages ----
    "binutils-devel": "binutils",
    "binutils-gold": "binutils",
    # ---- xz subpackages ----
    "xz-libs": "xz",
    "xz-devel": "xz",
    # ---- bzip2 subpackages ----
    "bzip2-libs": "bzip2",
    "bzip2-devel": "bzip2",
    # ---- zstd subpackages ----
    "libzstd": "zstd",
    "libzstd-devel": "zstd",
    "libzstd-static": "zstd",
    # ---- attr subpackages ----
    "libattr": "attr",
    "libattr-devel": "attr",
    # ---- acl subpackages ----
    "libacl": "acl",
    "libacl-devel": "acl",
    # ---- gtest subpackages ----
    "gtest-devel": "gtest",
    "gmock": "gtest",
    "gmock-devel": "gtest",
    "cmake-gtest": "gtest",
    # ---- atk -> at-spi2-core (renamed in modern Fedora) ----
    "atk": "at-spi2-core",
    "atk-devel": "at-spi2-core",
    # ---- wget -> wget2 (renamed in modern Fedora) ----
    "wget": "wget2",
    # ---- rust subpackages ----
    "cargo": "rust",
    "clippy": "rust",
    "rustfmt": "rust",
    "rust-std-static": "rust",
    "rust-src": "rust",
    "rust-doc": "rust",
    "rust-analyzer": "rust",
    # ---- rust-bindgen ----
    "bindgen": "rust-bindgen-cli",
    "bindgen-cli": "rust-bindgen-cli",
    # ---- python3 -> python3.NN ----
    "python3": "python3.13",
    # ---- emacs subpackages ----
    "emacs-common": "emacs",
    "emacs-nox": "emacs",
    "emacs-lucid": "emacs",
    "emacs-terminal": "emacs",
    # ---- curl subpackages ----
    "libcurl": "curl",
    "libcurl-devel": "curl",
    # ---- pcre2 subpackages ----
    "pcre2-devel": "pcre2",
    "pcre2-utf16": "pcre2",
    "pcre2-utf32": "pcre2",
    # ---- libffi subpackages ----
    "libffi-devel": "libffi",
    # ---- readline subpackages ----
    "readline-devel": "readline",
    # ---- sqlite subpackages ----
    "sqlite-devel": "sqlite",
    "sqlite-libs": "sqlite",
    # ---- expat subpackages ----
    "expat-devel": "expat",
    # ---- libxml2 subpackages ----
    "libxml2-devel": "libxml2",
    # ---- libxslt subpackages ----
    "libxslt-devel": "libxslt",
    # ---- mesa subpackages ----
    "mesa-libGL": "mesa",
    "mesa-libGL-devel": "mesa",
    "mesa-libEGL": "mesa",
    "mesa-libEGL-devel": "mesa",
    "mesa-libGLU": "mesa",
    "mesa-libGLU-devel": "mesa",
    "mesa-libgbm": "mesa",
    "mesa-libgbm-devel": "mesa",
    "mesa-vulkan-drivers": "mesa",
    "mesa-dri-drivers": "mesa",
    # ---- nodejs -> versioned ----
    "nodejs": "nodejs22",
    "nodejs-devel": "nodejs22",
    "npm": "nodejs22",
    # ---- libpng subpackages ----
    "libpng": "libpng",
    "libpng-devel": "libpng",
    # ---- freetype subpackages ----
    "freetype-devel": "freetype",
    # ---- fontconfig subpackages ----
    "fontconfig-devel": "fontconfig",
    # ---- pango subpackages ----
    "pango-devel": "pango",
    # ---- cairo subpackages ----
    "cairo-devel": "cairo",
    "cairo-gobject-devel": "cairo",
    # ---- gdk-pixbuf2 subpackages ----
    "gdk-pixbuf2-devel": "gdk-pixbuf2",
    # ---- gtk3 subpackages ----
    "gtk3-devel": "gtk3",
    # ---- glib2 subpackages ----
    "glib2-devel": "glib2",
    # ---- dbus subpackages ----
    "dbus-devel": "dbus",
    "dbus-libs": "dbus",
    "dbus-common": "dbus",
    # ---- systemd subpackages ----
    "systemd-devel": "systemd",
    "systemd-libs": "systemd",
    "systemd-udev": "systemd",
    # ---- libcap subpackages ----
    "libcap-devel": "libcap",
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
            candidates.append(srpm)
            # Also keep original name as fallback
            if rpm_name != srpm:
                candidates.append(rpm_name)

        # Rule: python3-X -> try python-X first, then python3-X
        elif rpm_name.startswith("python3-"):
            base = rpm_name[len("python3-"):]
            candidates.append(f"python-{base}")
            candidates.append(rpm_name)

        # Rule: python2-X -> try python-X first
        elif rpm_name.startswith("python2-"):
            base = rpm_name[len("python2-"):]
            candidates.append(f"python-{base}")
            candidates.append(rpm_name)

        # Rule: *-devel -> try without -devel suffix
        elif rpm_name.endswith("-devel"):
            base = rpm_name[: -len("-devel")]
            candidates.append(base)
            candidates.append(rpm_name)

        # Rule: *-libs -> try without -libs suffix
        elif rpm_name.endswith("-libs"):
            base = rpm_name[: -len("-libs")]
            candidates.append(base)
            candidates.append(rpm_name)

        # Rule: *-common -> try without -common suffix
        elif rpm_name.endswith("-common"):
            base = rpm_name[: -len("-common")]
            candidates.append(base)
            candidates.append(rpm_name)

        # Rule: *-base -> try without -base suffix
        elif rpm_name.endswith("-base"):
            base = rpm_name[: -len("-base")]
            candidates.append(base)
            candidates.append(rpm_name)

        # Rule: *-static -> try without -static suffix
        elif rpm_name.endswith("-static"):
            base = rpm_name[: -len("-static")]
            candidates.append(base)
            candidates.append(rpm_name)

        # Rule: *-langpack-XX -> try base package
        elif "-langpack-" in rpm_name:
            base = rpm_name.split("-langpack-")[0]
            candidates.append(base)
            candidates.append(rpm_name)

        # Rule: perl-X -> same name (SRPM usually matches)
        elif rpm_name.startswith("perl-"):
            candidates.append(rpm_name)

        # Rule: rubygem-X -> rubygem-X (SRPM usually matches)
        elif rpm_name.startswith("rubygem-"):
            candidates.append(rpm_name)

        # Rule: nodejs-X -> nodejs-X
        elif rpm_name.startswith("nodejs-"):
            candidates.append(rpm_name)

        # Rule: golang-X -> golang-X
        elif rpm_name.startswith("golang-"):
            candidates.append(rpm_name)

        # Default: use the name as-is
        else:
            candidates.append(rpm_name)

        # Deduplicate while preserving order (insurance for future patterns)
        seen: set[str] = set()
        result: list[str] = []
        for name in candidates:
            if name not in seen:  # pragma: no branch
                seen.add(name)
                result.append(name)

        return result

    def get_download_candidates(self, name: str) -> list[str]:
        """
        Return a list of package names to try when downloading an SRPM.

        Used for both aliases (e.g. python3 -> python3.12) and virtual provides.
        Order: ML prediction (srpm_name, rpm_name), rule-based resolve, resolve_srpm_name variants, then name.

        Args:
            name: Package name or alias (e.g. "python3", "python3dist(requests)")

        Returns:
            List of names to try, deduplicated and ordered by likelihood.
        """
        seen: set[str] = set()
        candidates: list[str] = []

        # Strip rich dependency syntax before processing
        name = self._strip_rich_dep(name)

        # 0. Known subpackage mapping — highest priority (avoid futile lookups)
        if name in SUBPACKAGE_TO_SRPM:
            srpm = SUBPACKAGE_TO_SRPM[name]
            if srpm not in seen:
                seen.add(srpm)
                candidates.append(srpm)

        # 1. ML prediction (for aliases like python3 and virtual provides)
        if self.ml_resolver:
            try:
                ml_result = self.ml_resolver.predict(name)
                if ml_result and isinstance(ml_result, dict):
                    srpm = ml_result.get("srpm_name")
                    rpm = ml_result.get("rpm_name")
                    if srpm and srpm not in seen:
                        seen.add(srpm)
                        candidates.append(srpm)
                    if rpm and rpm not in seen:
                        seen.add(rpm)
                        candidates.append(rpm)
            except Exception:
                pass

        # 2. Rule-based resolve (virtual provides)
        resolved = self.resolve(name)
        if resolved and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

        # 3. SRPM name variants for the given name
        for n in self.resolve_srpm_name(name):
            if n not in seen:
                seen.add(n)
                candidates.append(n)

        # 4. SRPM name variants for the rule-resolved name (if different)
        if resolved and resolved != name:
            for n in self.resolve_srpm_name(resolved):
                if n not in seen:
                    seen.add(n)
                    candidates.append(n)

        # 5. Original name (safety net; resolve() always adds name to candidates)
        if name not in seen:  # pragma: no cover
            candidates.append(name)

        return candidates
