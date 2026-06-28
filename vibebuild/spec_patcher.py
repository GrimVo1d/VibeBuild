"""Patching spec files inside an SRPM before sending to koji.

Applied to all SRPMs (the target and dependencies) before submit when enabled.
Two transformations are applied:

1. The `%check` section is cut out entirely: upstream testsuites often require
   network access, specific pid/process limits and other things that our
   sandbox mock does not have; instead we rely on a separate smoke install after
   tagging. Always applied.
2. Optionally added at the start of the spec:
   `%define _unpackaged_files_terminate_build 0`, so that unpackaged files left in BUILDROOT
   do not fail the build. Applied conditionally, only on
   retry after a build that failed with Installed (but unpackaged) file(s) found
   (see the retry logic in `vibebuild/builder.py`).

Both mechanisms are standard RPM/Fedora: the macro is documented in upstream
`/usr/lib/rpm/macros`, `%check` is an ordinary optional spec section.

Patching is opt-in (`--patch-specs`): rebuilding an SRPM runs `rpmbuild -bs`,
which executes shell and Lua macros of the spec. It runs under bubblewrap when
available. Without the option SRPMs go to Koji untouched.
"""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from vibebuild.exceptions import VibeBuildError

logger = logging.getLogger(__name__)

UNPACKAGED_MACRO = "%define _unpackaged_files_terminate_build 0"

# Names of RPM sections. After `%check` parsing resumes when any
# of them appears at the start of a line.
_SECTION_NAMES = frozenset(
    {
        "package",
        "description",
        "prep",
        "generate_buildrequires",
        "conf",
        "build",
        "install",
        "check",
        "clean",
        "files",
        "changelog",
        "pre",
        "post",
        "preun",
        "postun",
        "pretrans",
        "posttrans",
        "trigger",
        "triggerin",
        "triggerun",
        "triggerpostun",
        "triggerprein",
        "verifyscript",
        "sepolicy",
        "filetriggerin",
        "filetriggerun",
        "filetriggerpostun",
        "transfiletriggerin",
        "transfiletriggerun",
        "transfiletriggerpostun",
    }
)


def _section_token(line: str) -> str | None:
    """Return the section name if the line starts one, otherwise None."""
    stripped = line.lstrip()
    if not stripped.startswith("%"):
        return None
    head = stripped.split(None, 1)[0]
    name = head[1:].split("(", 1)[0]
    return name if name in _SECTION_NAMES else None


def patch_spec_text(spec: str, add_unpackaged_macro: bool = False) -> str:
    """Apply the edits to the spec text. Idempotent.

    `%check` is always cut out. `UNPACKAGED_MACRO` is added only if
    `add_unpackaged_macro=True`, used on retry after an error
    "Installed (but unpackaged) file(s) found".
    """
    out: list[str] = []
    in_check = False
    has_macro = UNPACKAGED_MACRO in spec
    for line in spec.splitlines(keepends=True):
        section = _section_token(line)
        if section == "check":
            in_check = True
            continue
        if in_check and section is not None:
            in_check = False
        if not in_check:
            out.append(line)
    body = "".join(out)
    if add_unpackaged_macro and not has_macro:
        body = UNPACKAGED_MACRO + "\n" + body
    return body


def _extract_srpm(srpm: Path, dest: Path, timeout: int) -> None:
    """Unpack an SRPM into dest via rpm2archive and tarfile's "data" filter.

    The filter refuses absolute paths, `..`, links pointing outside dest and
    device files, so a crafted SRPM cannot write outside dest.
    """
    with srpm.open("rb") as src:
        proc = subprocess.Popen(
            ["rpm2archive", "-"], stdin=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            assert proc.stdout is not None
            with tarfile.open(fileobj=proc.stdout, mode="r|gz") as archive:
                archive.extractall(dest, filter="data")
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise VibeBuildError(f"rpm2archive timed out extracting {srpm.name}")
        except (tarfile.TarError, OSError) as exc:
            proc.kill()
            raise VibeBuildError(f"Cannot extract {srpm.name}: {exc}") from exc
    if proc.returncode != 0:
        raise VibeBuildError(
            f"rpm2archive failed on {srpm.name} (rc={proc.returncode}): "
            f"{err.decode(errors='replace').strip()}"
        )


def _rpm_path(path: Path) -> str:
    """A path as an rpm macro value: `%` would start a macro."""
    return str(path).replace("%", "%%")


@functools.cache
def _bwrap() -> str | None:
    """Path to a working bwrap, or None (not installed, or no user namespaces)."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        logger.warning(
            "bwrap not found: rpmbuild parses the spec without a sandbox, as the current user"
        )
        return None
    try:
        probe = subprocess.run(
            [bwrap, "--unshare-all", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        probe = subprocess.CompletedProcess([], 1, "", str(e))
    if probe.returncode != 0:
        logger.warning(
            "bwrap cannot create a sandbox here (%s): rpmbuild parses the spec without one",
            probe.stderr.strip().splitlines()[0] if probe.stderr.strip() else probe.returncode,
        )
        return None
    return bwrap


def _sandboxed(cmd: list[str], work_dir: Path) -> list[str]:
    """Wrap cmd in bubblewrap: no network, no $HOME, only work_dir writable.

    rpmbuild expands `%(...)` and `%{lua:...}` from the spec while parsing it, so
    a spec runs code. Where bwrap is missing or cannot create namespaces (e.g. an
    unprivileged container), the command runs as is, with a warning.
    """
    bwrap = _bwrap()
    if not bwrap:
        return cmd
    sandbox = [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--ro-bind-try", "/etc/rpm", "/etc/rpm",
        "--ro-bind-try", "/etc/passwd", "/etc/passwd",
        "--ro-bind-try", "/etc/group", "/etc/group",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", str(work_dir), str(work_dir),
        "--setenv", "HOME", str(work_dir),
        "--chdir", str(work_dir),
    ]  # fmt: skip
    return sandbox + cmd


def patch_srpm(
    srpm_path: str,
    timeout: int = 600,
    add_unpackaged_macro: bool = False,
) -> str:
    """Unpack the SRPM, patch the spec, rebuild a new SRPM in a fresh temp directory.

    Returns the absolute path to the new `.src.rpm`; the caller removes its
    directory when done (`cleanup_patched`). Name and Version do not change;
    `%{?dist}` in Release follows the local rpm macros, just as when Koji
    rebuilds the SRPM in its buildroot. On any error raises `VibeBuildError`,
    and nothing is left behind.

    When `add_unpackaged_macro=True`, the start of the spec gets
    `%define _unpackaged_files_terminate_build 0`.
    """
    srpm = Path(srpm_path).resolve()
    if not srpm.is_file():
        raise VibeBuildError(f"SRPM not found: {srpm}")

    target = Path(tempfile.mkdtemp(prefix="vibebuild-patch-"))
    try:
        return _patch_into(srpm, target, timeout, add_unpackaged_macro)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def cleanup_patched(patched_srpm: str) -> None:
    """Remove the temp directory a patch_srpm result lives in."""
    parent = Path(patched_srpm).parent
    if parent.name.startswith("vibebuild-patch-"):
        shutil.rmtree(parent, ignore_errors=True)


def _patch_into(srpm: Path, target: Path, timeout: int, add_unpackaged_macro: bool) -> str:
    sources_dir = target / "SOURCES"
    specs_dir = target / "SPECS"
    sources_dir.mkdir()
    specs_dir.mkdir()

    _extract_srpm(srpm, sources_dir, timeout)

    spec_files = sorted(p for p in sources_dir.glob("*.spec") if p.is_file() and not p.is_symlink())
    if not spec_files:
        raise VibeBuildError(f"No spec file found inside {srpm.name}")
    if len(spec_files) > 1:
        logger.warning("multiple specs in %s, picking %s", srpm.name, spec_files[0].name)

    spec_dst = specs_dir / spec_files[0].name
    spec_files[0].rename(spec_dst)
    spec_dst.write_text(
        patch_spec_text(spec_dst.read_text(), add_unpackaged_macro=add_unpackaged_macro)
    )

    cmd = [
        "rpmbuild",
        "-bs",
        "--nodeps",
        "--define",
        f"_topdir {_rpm_path(target)}",
        "--define",
        f"_sourcedir {_rpm_path(sources_dir)}",
        "--define",
        f"_specdir {_rpm_path(specs_dir)}",
        "--define",
        f"_srcrpmdir {_rpm_path(target)}",
        "--define",
        f"_builddir {_rpm_path(target / 'BUILD')}",
        str(spec_dst),
    ]
    try:
        result = subprocess.run(
            _sandboxed(cmd, target), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise VibeBuildError(f"rpmbuild -bs timed out for {srpm.name}")
    if result.returncode != 0:
        raise VibeBuildError(
            f"rpmbuild -bs failed for {srpm.name}: {result.stderr.strip() or result.stdout.strip()}"
        )

    new_srpms = sorted(target.glob("*.src.rpm"))
    if not new_srpms:
        raise VibeBuildError(f"rpmbuild produced no SRPM for {srpm.name}")

    logger.info("patched spec inside %s -> %s", srpm.name, new_srpms[0].name)
    return str(new_srpms[0])
