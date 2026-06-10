"""Tests for vibebuild.toolchain.detect_toolchains."""

import pytest

from vibebuild.toolchain import detect_toolchains


class TestDetectToolchainsSingleLanguage:
    def test_gcc_cpp_detects_c_cpp(self):
        assert detect_toolchains(["gcc-c++"]) == ["C/C++"]

    def test_golang_virtual_provide_detects_go(self):
        assert detect_toolchains(["golang(github.com/spf13/cobra)"]) == ["Go"]

    def test_crate_virtual_provide_detects_rust(self):
        assert detect_toolchains(["crate(serde/default)"]) == ["Rust"]

    def test_python3dist_detects_python(self):
        assert detect_toolchains(["python3dist(flask)"]) == ["Python"]

    def test_perl_virtual_provide_detects_perl(self):
        assert detect_toolchains(["perl(File::Path)"]) == ["Perl"]

    def test_rubygem_virtual_provide_detects_ruby(self):
        assert detect_toolchains(["rubygem(bundler)"]) == ["Ruby"]

    def test_mvn_virtual_provide_detects_java(self):
        assert detect_toolchains(["mvn(org.apache:commons-lang)"]) == ["Java"]

    def test_cargo_detects_rust(self):
        assert detect_toolchains(["cargo"]) == ["Rust"]

    def test_cmake_detects_cmake(self):
        assert detect_toolchains(["cmake"]) == ["CMake"]

    def test_meson_detects_meson(self):
        assert detect_toolchains(["meson"]) == ["Meson"]


class TestDetectToolchainsMultiple:
    def test_mixed_returns_fixed_order(self):
        # _TOOLCHAIN_MARKERS order: C/C++, Go, Rust, ..., CMake, Meson, Autotools
        result = detect_toolchains(["gcc-c++", "cmake", "golang"])
        assert result == ["C/C++", "Go", "CMake"]

    def test_python_c_extension_detects_both(self):
        result = detect_toolchains(["python3-devel", "gcc"])
        assert "Python" in result
        assert "C/C++" in result
        # order: C/C++ comes before Python in the markers
        assert result.index("C/C++") < result.index("Python")

    def test_no_duplicates_when_same_toolchain_matches_twice(self):
        result = detect_toolchains(["gcc", "gcc-c++", "clang"])
        assert result == ["C/C++"]


class TestDetectToolchainsEdgeCases:
    def test_empty_list_returns_empty(self):
        assert detect_toolchains([]) == []

    def test_unknown_packages_return_empty(self):
        assert detect_toolchains(["totally-unrelated-pkg", "another-one"]) == []

    def test_python_short_provide_detects_python(self):
        # "python(abi)" is a frequent BR in Fedora
        assert detect_toolchains(["python(abi)"]) == ["Python"]

    def test_autotools_trio(self):
        result = detect_toolchains(["autoconf", "automake", "libtool"])
        assert result == ["Autotools"]


@pytest.mark.parametrize(
    "br,expected",
    [
        ("ninja-build", "Meson"),
        ("nodejs", "Node.js"),
        ("npm(typescript)", "Node.js"),
        ("nodejs(modules)", "Node.js"),
        ("rust-packaging", "Rust"),
        ("go-rpm-macros", "Go"),
        ("pyproject-rpm-macros", "Python"),
        ("python3-cryptography", "Python"),
        ("php(language)", "PHP"),
        ("php-composer(symfony/console)", "PHP"),
        ("R", "R"),
        ("R-base", "R"),
        ("R-devel", "R"),
        ("lua(io)", "Lua"),
        ("lua-event", "Lua"),
        ("ghc-rpm-macros", "Haskell"),
        ("ghc-base", "Haskell"),
        ("haskell-platform", "Haskell"),
        ("ocaml-findlib", "OCaml"),
        ("ocaml(Stdlib)", "OCaml"),
        ("erlang-rebar", "Erlang"),
        ("swift-lang", "Swift"),
        ("dotnet-sdk", ".NET"),
        ("dotnet-runtime-8.0", ".NET"),
        ("java-21-openjdk-devel", "Java"),
        ("ant", "Java"),
    ],
)
def test_parametrized_markers(br, expected):
    assert expected in detect_toolchains([br])
