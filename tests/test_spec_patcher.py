"""Tests for vibebuild.spec_patcher.patch_spec_text.

Verify two edits:
1. `%define _unpackaged_files_terminate_build 0` is added once.
2. The `%check` section is cut out entirely, up to the next RPM section.
"""

import pytest

from vibebuild.spec_patcher import UNPACKAGED_MACRO, patch_spec_text

FULL_SPEC = """\
Name: foo
Version: 1.0
Release: 1%{?dist}

%description
A test package.

%prep
%setup -q

%build
make

%install
make install

%check
make test
echo "should not survive"

%files
%{_bindir}/foo

%changelog
* Mon Jan 01 2026 Test <test@example.internal> - 1.0-1
- initial
"""


def test_strip_check_section_completely():
    patched = patch_spec_text(FULL_SPEC)
    assert "%check" not in patched
    assert "make test" not in patched
    assert 'echo "should not survive"' not in patched


def test_strip_check_preserves_following_sections():
    patched = patch_spec_text(FULL_SPEC)
    assert "%files" in patched
    assert "%{_bindir}/foo" in patched
    assert "%changelog" in patched
    assert "initial" in patched


def test_unpackaged_macro_added():
    patched = patch_spec_text(FULL_SPEC)
    assert patched.startswith(UNPACKAGED_MACRO)
    assert patched.count(UNPACKAGED_MACRO) == 1


def test_macro_not_duplicated_if_present():
    spec = f"{UNPACKAGED_MACRO}\nName: foo\n"
    patched = patch_spec_text(spec)
    assert patched.count(UNPACKAGED_MACRO) == 1


def test_idempotent():
    once = patch_spec_text(FULL_SPEC)
    twice = patch_spec_text(once)
    assert once == twice


def test_no_check_section_body_unchanged_except_macro():
    spec = (
        "Name: foo\n"
        "%prep\n"
        "%setup -q\n"
        "%build\n"
        "make\n"
        "%install\n"
        "make install\n"
        "%files\n"
        "%{_bindir}/foo\n"
    )
    patched = patch_spec_text(spec)
    body = patched.replace(UNPACKAGED_MACRO + "\n", "", 1)
    assert body == spec


def test_check_at_end_of_file_strips_to_eof():
    spec = "Name: foo\n%check\nrun tests\nmore stuff\n"
    patched = patch_spec_text(spec)
    assert "run tests" not in patched
    assert "more stuff" not in patched
    assert "Name: foo" in patched


def test_partial_keyword_not_treated_as_check():
    # %checkpoint, %check_some_macro and the like must not trigger the strip
    spec = "Name: foo\n" "%checkpoint=true\n" "still here\n" "%files\n" "%{_bindir}/foo\n"
    patched = patch_spec_text(spec)
    assert "%checkpoint=true" in patched
    assert "still here" in patched


def test_files_subpackage_resumes_after_check():
    spec = "%check\n" "make test\n" "%files -n libfoo\n" "%{_libdir}/libfoo.so.*\n"
    patched = patch_spec_text(spec)
    assert "make test" not in patched
    assert "%files -n libfoo" in patched
    assert "%{_libdir}/libfoo.so.*" in patched


def test_macro_reference_in_post_not_stripped():
    # Between %check and %files comes %post, which is also a section and must resume parsing.
    spec = "%check\n" "make test\n" "%post\n" "/sbin/ldconfig\n" "%files\n" "%{_bindir}/foo\n"
    patched = patch_spec_text(spec)
    assert "make test" not in patched
    assert "%post" in patched
    assert "/sbin/ldconfig" in patched


@pytest.mark.parametrize(
    "section",
    [
        "%package -n libfoo",
        "%description -n libfoo",
        "%files -n libfoo-devel",
        "%post -p /sbin/ldconfig",
    ],
)
def test_section_with_arguments_resumes_parsing(section: str):
    spec = f"%check\nmake test\n{section}\nbody line\n"
    patched = patch_spec_text(spec)
    assert "make test" not in patched
    assert section in patched
    assert "body line" in patched
