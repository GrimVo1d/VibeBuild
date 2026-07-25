"""Tests for vibebuild.spec_patcher.

patch_spec_text edits:
1. The `%check` section is cut out entirely, up to the next RPM section, always.
2. `%define _unpackaged_files_terminate_build 0` is added only when
   `add_unpackaged_macro=True` (builder raises the flag on retry after an error
   "Installed (but unpackaged) file(s) found").
"""

import io
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from vibebuild import spec_patcher
from vibebuild.exceptions import VibeBuildError
from vibebuild.spec_patcher import (
    UNPACKAGED_MACRO,
    _check_tarfile_filter,
    _extract_srpm,
    _rpm_path,
    _sandboxed,
    check_can_patch,
    cleanup_patched,
    patch_spec_text,
    patch_srpm,
)

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


def test_unpackaged_macro_not_added_by_default():
    patched = patch_spec_text(FULL_SPEC)
    assert UNPACKAGED_MACRO not in patched


def test_unpackaged_macro_added_with_flag():
    patched = patch_spec_text(FULL_SPEC, add_unpackaged_macro=True)
    assert patched.startswith(UNPACKAGED_MACRO)
    assert patched.count(UNPACKAGED_MACRO) == 1


def test_macro_not_duplicated_if_present():
    spec = f"{UNPACKAGED_MACRO}\nName: foo\n"
    patched = patch_spec_text(spec, add_unpackaged_macro=True)
    assert patched.count(UNPACKAGED_MACRO) == 1


def test_idempotent_without_macro():
    once = patch_spec_text(FULL_SPEC)
    twice = patch_spec_text(once)
    assert once == twice


def test_idempotent_with_macro():
    once = patch_spec_text(FULL_SPEC, add_unpackaged_macro=True)
    twice = patch_spec_text(once, add_unpackaged_macro=True)
    assert once == twice


def test_no_check_section_body_unchanged_by_default():
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
    assert patch_spec_text(spec) == spec


def test_no_check_section_body_unchanged_except_macro_when_flag_set():
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
    patched = patch_spec_text(spec, add_unpackaged_macro=True)
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
    spec = "Name: foo\n%checkpoint=true\nstill here\n%files\n%{_bindir}/foo\n"
    patched = patch_spec_text(spec)
    assert "%checkpoint=true" in patched
    assert "still here" in patched


def test_files_subpackage_resumes_after_check():
    spec = "%check\nmake test\n%files -n libfoo\n%{_libdir}/libfoo.so.*\n"
    patched = patch_spec_text(spec)
    assert "make test" not in patched
    assert "%files -n libfoo" in patched
    assert "%{_libdir}/libfoo.so.*" in patched


def test_macro_reference_in_post_not_stripped():
    # Between %check and %files comes %post, which is also a section and must resume parsing.
    spec = "%check\nmake test\n%post\n/sbin/ldconfig\n%files\n%{_bindir}/foo\n"
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


def _tgz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_rpm2archive(monkeypatch):
    """rpm2archive replaced by `cat`: the "SRPM" file already holds the tar.gz."""
    real_popen = subprocess.Popen

    def popen(cmd, **kwargs):
        assert cmd == ["rpm2archive", "-"]
        return real_popen(["cat"], **kwargs)

    monkeypatch.setattr(spec_patcher.subprocess, "Popen", popen)


class TestExtractSrpm:
    def test_extracts_into_dest(self, tmp_path, fake_rpm2archive):
        srpm = tmp_path / "foo.src.rpm"
        srpm.write_bytes(_tgz({"foo.spec": b"Name: foo", "foo.tar.gz": b"src"}))
        dest = tmp_path / "out"
        dest.mkdir()

        _extract_srpm(srpm, dest, timeout=30)

        assert (dest / "foo.spec").read_bytes() == b"Name: foo"

    def test_payload_size_limit(self, tmp_path, fake_rpm2archive, mocker):
        mocker.patch("vibebuild.spec_patcher.MAX_PAYLOAD_BYTES", 10)
        srpm = tmp_path / "foo.src.rpm"
        srpm.write_bytes(_tgz({"foo.spec": b"Name: foo", "big.tar.gz": b"x" * 64}))
        dest = tmp_path / "out"
        dest.mkdir()

        with pytest.raises(VibeBuildError, match="larger than 10 bytes"):
            _extract_srpm(srpm, dest, timeout=30)

        assert not (dest / "big.tar.gz").exists()

    def test_refuses_parent_dir_paths(self, tmp_path, fake_rpm2archive):
        srpm = tmp_path / "foo.src.rpm"
        srpm.write_bytes(_tgz({"../evil": b"pwned"}))
        dest = tmp_path / "out"
        dest.mkdir()

        with pytest.raises(VibeBuildError, match="Cannot extract"):
            _extract_srpm(srpm, dest, timeout=30)

        assert not (tmp_path / "evil").exists()

    def test_absolute_paths_land_inside_dest(self, tmp_path, fake_rpm2archive):
        srpm = tmp_path / "foo.src.rpm"
        srpm.write_bytes(_tgz({str(tmp_path / "evil"): b"pwned"}))
        dest = tmp_path / "out"
        dest.mkdir()

        _extract_srpm(srpm, dest, timeout=30)

        assert not (tmp_path / "evil").exists()
        assert (dest / str(tmp_path / "evil").lstrip("/")).read_bytes() == b"pwned"

    def test_not_an_archive(self, tmp_path, fake_rpm2archive):
        srpm = tmp_path / "foo.src.rpm"
        srpm.write_bytes(b"not gzip")
        dest = tmp_path / "out"
        dest.mkdir()

        with pytest.raises(VibeBuildError, match="Cannot extract"):
            _extract_srpm(srpm, dest, timeout=30)

    def test_hanging_rpm2archive_times_out(self, tmp_path, monkeypatch):
        real_popen = subprocess.Popen
        monkeypatch.setattr(
            spec_patcher.subprocess, "Popen", lambda cmd, **kw: real_popen(["sleep", "30"], **kw)
        )
        srpm = tmp_path / "foo.src.rpm"
        srpm.write_bytes(b"")
        dest = tmp_path / "out"
        dest.mkdir()

        with pytest.raises(VibeBuildError, match="timed out"):
            _extract_srpm(srpm, dest, timeout=1)


def test_rpm_path_escapes_percent():
    assert _rpm_path(Path("/tmp/a%{lua:x}")) == "/tmp/a%%{lua:x}"


class TestSandboxed:
    @pytest.fixture(autouse=True)
    def fresh_probe(self):
        spec_patcher._bwrap.cache_clear()
        yield
        spec_patcher._bwrap.cache_clear()

    @pytest.fixture
    def probe(self):
        with (
            patch("vibebuild.spec_patcher.shutil.which", return_value="/usr/bin/bwrap"),
            patch("vibebuild.spec_patcher.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            yield run

    def test_wraps_in_bwrap(self, tmp_path, probe):
        cmd = _sandboxed(["rpmbuild", "-bs", "x.spec"], tmp_path)

        assert cmd[0] == "/usr/bin/bwrap"
        assert "--unshare-all" in cmd
        assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
        assert cmd.index("--clearenv") < cmd.index("--setenv")
        i = cmd.index("PATH")
        assert cmd[i - 1 : i + 2] == ["--setenv", "PATH", "/usr/bin:/usr/sbin"]
        assert cmd[-3:] == ["rpmbuild", "-bs", "x.spec"]
        i = cmd.index("--bind")
        assert cmd[i + 1 : i + 3] == [str(tmp_path), str(tmp_path)]

    def test_probe_uses_the_full_sandbox(self, tmp_path, probe):
        _sandboxed(["rpmbuild"], tmp_path)

        argv = probe.call_args.args[0]
        assert argv[-1] == "true"
        assert "--proc" in argv and "--cap-drop" in argv

    def test_bwrap_is_probed_once(self, tmp_path, probe):
        _sandboxed(["a"], tmp_path)
        _sandboxed(["b"], tmp_path)

        assert probe.call_count == 1

    def test_without_bwrap_refuses(self, tmp_path):
        with patch("vibebuild.spec_patcher.shutil.which", return_value=None):
            with pytest.raises(VibeBuildError, match="bwrap not found.*--allow-unsandboxed"):
                _sandboxed(["rpmbuild"], tmp_path)

    def test_without_bwrap_allowed_runs_as_is_with_warning(self, tmp_path, caplog):
        with patch("vibebuild.spec_patcher.shutil.which", return_value=None):
            cmd = _sandboxed(["rpmbuild"], tmp_path, allow_unsandboxed=True)

        assert cmd == ["rpmbuild"]
        assert "bwrap not found" in caplog.text

    def test_bwrap_without_namespaces_refuses(self, tmp_path, probe):
        probe.return_value = subprocess.CompletedProcess(
            [], 1, "", "bwrap: No permissions to create a new namespace\n"
        )

        with pytest.raises(VibeBuildError, match="No permissions to create a new namespace"):
            _sandboxed(["rpmbuild"], tmp_path)

    def test_check_can_patch(self, probe):
        check_can_patch()

        probe.return_value = subprocess.CompletedProcess([], 1, "", "no userns\n")
        spec_patcher._bwrap.cache_clear()
        with pytest.raises(VibeBuildError, match="no userns"):
            check_can_patch()
        check_can_patch(allow_unsandboxed=True)


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ((3, 12, 10), True),
        ((3, 12, 11), False),
        ((3, 13, 3), True),
        ((3, 13, 4), False),
        ((3, 14, 0), False),
    ],
)
def test_tarfile_filter_version_guard(version, refused):
    if refused:
        with pytest.raises(VibeBuildError, match="CVE-2025-4517"):
            _check_tarfile_filter(version)
    else:
        _check_tarfile_filter(version)


class TestPatchSrpm:
    @pytest.fixture
    def srpm(self, tmp_path):
        path = tmp_path / "foo-1.0-1.src.rpm"
        path.write_bytes(b"srpm")
        return path

    @pytest.fixture
    def extract(self):
        def fake(srpm, dest, timeout):
            (dest / "foo.spec").write_text(FULL_SPEC)

        with patch("vibebuild.spec_patcher._extract_srpm", side_effect=fake) as mock:
            yield mock

    @pytest.fixture
    def rpmbuild(self):
        def fake(cmd, **kwargs):
            topdir = Path(cmd[cmd.index("--define") + 1].removeprefix("_topdir "))
            (topdir / "foo-1.0-1.src.rpm").write_bytes(b"patched")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch("vibebuild.spec_patcher._bwrap", return_value=("/usr/bin/bwrap", "")),
            patch("vibebuild.spec_patcher.subprocess.run", side_effect=fake) as mock,
        ):
            yield mock

    def test_rebuilds_patched_srpm_in_temp_dir(self, srpm, extract, rpmbuild):
        patched = Path(patch_srpm(str(srpm), add_unpackaged_macro=True))
        try:
            assert patched.parent.name.startswith("vibebuild-patch-")
            assert patched.read_bytes() == b"patched"
            spec = (patched.parent / "SPECS" / "foo.spec").read_text()
            assert "%check" not in spec
            assert spec.startswith(UNPACKAGED_MACRO)
        finally:
            cleanup_patched(str(patched))
        assert not patched.parent.exists()

    def test_rpmbuild_failure_leaves_nothing(self, srpm, extract, rpmbuild, tmp_path):
        rpmbuild.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "bad")
        dirs = []
        real_mkdtemp = spec_patcher.tempfile.mkdtemp

        def mkdtemp(**kwargs):
            dirs.append(Path(real_mkdtemp(**kwargs)))
            return str(dirs[-1])

        with patch("vibebuild.spec_patcher.tempfile.mkdtemp", side_effect=mkdtemp):
            with pytest.raises(VibeBuildError, match="rpmbuild -bs failed"):
                patch_srpm(str(srpm))

        assert dirs and not dirs[0].exists()

    def test_rpmbuild_timeout(self, srpm, extract, rpmbuild):
        rpmbuild.side_effect = subprocess.TimeoutExpired("rpmbuild", 1)

        with pytest.raises(VibeBuildError, match="timed out"):
            patch_srpm(str(srpm))

    def test_symlinked_spec_is_ignored(self, srpm, extract, rpmbuild, tmp_path):
        target = tmp_path / "secret"
        target.write_text("x")
        extract.side_effect = lambda s, dest, t: (dest / "foo.spec").symlink_to(target)

        with pytest.raises(VibeBuildError, match="No spec file"):
            patch_srpm(str(srpm))

    def test_missing_srpm(self, tmp_path):
        with pytest.raises(VibeBuildError, match="SRPM not found"):
            patch_srpm(str(tmp_path / "nope.src.rpm"))


def test_cleanup_patched_only_removes_its_own_dirs(tmp_path):
    other = tmp_path / "keep"
    other.mkdir()
    (other / "x.src.rpm").write_text("x")

    cleanup_patched(str(other / "x.src.rpm"))

    assert other.exists()
