"""Classification of BuildRequires by the toolchains/languages used.

Purely rule-based, no external dependencies. Used in the CLI to
build the "Languages / Toolchains detected" section in the build summary.
"""

from __future__ import annotations

# name -> (exact_names, prefix_markers)
# One BR can match several toolchains (a Python C extension
# pulls in both python3-devel and gcc).
_TOOLCHAIN_MARKERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "C/C++": (("gcc", "gcc-c++", "clang", "glibc-devel", "libstdc++-devel"), ()),
    "Go": (("golang", "golang-bin", "go-rpm-macros"), ("golang(",)),
    "Rust": (("rust", "cargo", "rust-packaging"), ("crate(",)),
    "Python": (
        ("python3-devel", "python3-setuptools", "python3-pip"),
        ("python3dist(", "python("),
    ),
    "Perl": (("perl", "perl-devel", "perl-generators"), ("perl(",)),
    "Ruby": (("ruby", "ruby-devel", "rubygems"), ("rubygem(",)),
    "Node.js": (("nodejs", "npm"), ("npm(", "nodejs-")),
    "Java": (("java-devel", "maven-local"), ("mvn(",)),
    "CMake": (("cmake",), ("cmake(",)),
    "Meson": (("meson", "ninja-build"), ()),
    "Autotools": (("autoconf", "automake", "libtool"), ()),
}


def detect_toolchains(build_requires: list[str]) -> list[str]:
    """Return a sorted list of toolchain names found in the BR.

    BR names are checked for an exact match and for a prefix match
    for virtual provides (`python3dist(...)`, `golang(...)`, etc.).
    Returns a list without duplicates in a fixed order (as in
    `_TOOLCHAIN_MARKERS`).
    """
    found: set[str] = set()
    for req in build_requires:
        for tc_name, (exact, prefixes) in _TOOLCHAIN_MARKERS.items():
            if req in exact or any(req.startswith(p) for p in prefixes):
                found.add(tc_name)
    return [t for t in _TOOLCHAIN_MARKERS if t in found]
