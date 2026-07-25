"""Patching spec files inside an SRPM before sending it to Koji.

When enabled, applied to every SRPM (the target and its dependencies) before
submit. Two transformations:

1. The `%check` section is cut out entirely: upstream testsuites often need
   network access, process limits and other things a mock buildroot does not
   have. Always applied.
2. `%define _unpackaged_files_terminate_build 0` is added at the start of the
   spec, so that files left unpackaged in the buildroot do not fail the build.
   Only on the retry after a build failed with "Installed (but unpackaged)
   file(s) found" (see the retry logic in `vibebuild/builder.py`).

Both mechanisms are standard RPM/Fedora: the macro is documented in upstream
`/usr/lib/rpm/macros`, `%check` is an ordinary optional spec section.

Patching is opt-in (`--patch-specs`): rebuilding an SRPM runs `rpmbuild -bs`,
which executes shell and Lua macros of the spec. It runs under bubblewrap; where
bwrap cannot sandbox it, patching refuses unless `--allow-unsandboxed` is given.
Without the option SRPMs go to Koji untouched.
"""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError

logger = logging.getLogger(__name__)

UNPACKAGED_MACRO = "%define _unpackaged_files_terminate_build 0"

# Sources in an SRPM are mostly compressed already: the payload is about the SRPM size
MAX_PAYLOAD_BYTES = 8 * 1024**3

# tarfile's "data" filter can be bypassed before these releases (CVE-2025-4517 and others)
_SAFE_TARFILE: dict[tuple[int, ...], tuple[int, ...]] = {(3, 12): (3, 12, 11), (3, 13): (3, 13, 4)}

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


def _size_limited(limit: int) -> Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None]:
    """tarfile's "data" filter that also stops once members add up to more than limit bytes."""
    total = 0

    def data_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal total
        checked = tarfile.data_filter(member, path)
        total += member.size
        if total > limit:
            raise VibeBuildError(f"SRPM payload is larger than {limit} bytes")
        return checked

    return data_filter


def _extract_srpm(srpm: Path, dest: Path, timeout: int) -> None:
    """Unpack an SRPM into dest via rpm2archive and tarfile's "data" filter.

    The filter refuses absolute paths, `..`, links pointing outside dest and
    device files, so a crafted SRPM cannot write outside dest; the payload may
    unpack to at most MAX_PAYLOAD_BYTES.
    """
    _check_tarfile_filter()
    with srpm.open("rb") as src:
        # List argv without a shell; rpm2archive is looked up in PATH
        proc = subprocess.Popen(  # nosec B603 B607
            ["rpm2archive", "-"], stdin=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # The timeout covers the extraction too: killing rpm2archive ends the stream
        # tarfile is blocked on
        timed_out = threading.Event()

        def kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|gz") as archive:
                # _size_limited wraps tarfile.data_filter, which bandit does not see through
                size_limited = _size_limited(MAX_PAYLOAD_BYTES)
                archive.extractall(dest, filter=size_limited)  # nosec B202
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise VibeBuildError(f"rpm2archive timed out extracting {srpm.name}") from exc
        except (tarfile.TarError, OSError) as exc:
            if timed_out.is_set():
                raise VibeBuildError(f"rpm2archive timed out extracting {srpm.name}") from exc
            raise VibeBuildError(f"Cannot extract {srpm.name}: {exc}") from exc
        finally:
            timer.cancel()
            # On any error rpm2archive may still run: stop and reap it
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if timed_out.is_set():
        raise VibeBuildError(f"rpm2archive timed out extracting {srpm.name}")
    if proc.returncode != 0:
        raise VibeBuildError(
            f"rpm2archive failed on {srpm.name} (rc={proc.returncode}): "
            f"{err.decode(errors='replace').strip()}"
        )


def _check_tarfile_filter(version: tuple[int, ...] = sys.version_info[:3]) -> None:
    """Refuse to extract on a Python whose tarfile "data" filter has known bypasses."""
    fixed = _SAFE_TARFILE.get(version[:2])
    if fixed and version < fixed:
        raise VibeBuildError(
            f"Python {'.'.join(map(str, version))} has tarfile filter bypasses "
            f"(CVE-2025-4517); --patch-specs needs Python {'.'.join(map(str, fixed))} or later"
        )


def _rpm_path(path: Path) -> str:
    """A path as an rpm macro value: `%` would start a macro."""
    return str(path).replace("%", "%%")


@functools.cache
def _bwrap() -> tuple[str | None, str]:
    """Path to a working bwrap and "", or None and why there is none.

    The probe runs `true` in the very sandbox rpmbuild gets: a container may allow
    namespaces but refuse a fresh /proc, which only shows with the full set of mounts.
    """
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None, "bwrap not found"
    try:
        with tempfile.TemporaryDirectory(prefix="vibebuild-probe-") as work_dir:
            # List argv without a shell; bwrap is the absolute path from shutil.which
            probe = subprocess.run(  # nosec B603
                _sandbox_argv(bwrap, Path(work_dir)) + ["true"],
                capture_output=True,
                text=True,
                timeout=30,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        probe = subprocess.CompletedProcess([], 1, "", str(e))
    if probe.returncode != 0:
        reason = probe.stderr.strip().splitlines()[0] if probe.stderr.strip() else probe.returncode
        return None, f"bwrap cannot create a sandbox here ({reason})"
    return bwrap, ""


def _require_sandbox() -> None:
    """Raise VibeBuildError unless bwrap can sandbox rpmbuild here."""
    bwrap, problem = _bwrap()
    if not bwrap:
        raise VibeBuildError(
            f"{problem}: --patch-specs runs the spec's macros and needs a sandbox "
            "(--allow-unsandboxed runs them without one)"
        )


def check_can_patch(allow_unsandboxed: bool = False) -> None:
    """Raise VibeBuildError if patch_srpm would refuse to run here; checked before a build."""
    _check_tarfile_filter()
    if not allow_unsandboxed:
        _require_sandbox()


@functools.cache
def _warn_unsandboxed(problem: str) -> None:
    logger.warning("%s: rpmbuild parses the spec without a sandbox, as the current user", problem)


def _sandboxed(cmd: list[str], work_dir: Path, allow_unsandboxed: bool = False) -> list[str]:
    """Wrap cmd in bubblewrap: no network, no $HOME, no capabilities, only work_dir writable.

    rpmbuild expands `%(...)` and `%{lua:...}` from the spec while parsing it, so
    a spec runs code. Where bwrap is missing or cannot create namespaces (e.g. an
    unprivileged container), raises VibeBuildError, or with allow_unsandboxed
    runs the command as is, with a warning.
    """
    bwrap, problem = _bwrap()
    if not bwrap:
        if not allow_unsandboxed:
            _require_sandbox()
        _warn_unsandboxed(problem)
        return cmd
    return _sandbox_argv(bwrap, work_dir) + cmd


def _sandbox_argv(bwrap: str, work_dir: Path) -> list[str]:
    """bwrap's argv up to the command it runs."""
    # A fresh tmpfs inside the sandbox, not the host /tmp
    sandbox_tmp = "/tmp"  # nosec B108
    return [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop", "ALL",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/usr/sbin",
        "--setenv", "LANG", "C.UTF-8",
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
        "--tmpfs", sandbox_tmp,
        "--bind", str(work_dir), str(work_dir),
        "--setenv", "HOME", str(work_dir),
        "--chdir", str(work_dir),
    ]  # fmt: skip


def patch_srpm(
    srpm_path: str,
    timeout: int = 600,
    add_unpackaged_macro: bool = False,
    allow_unsandboxed: bool = False,
) -> str:
    """Unpack the SRPM, patch the spec, rebuild a new SRPM in a fresh temp directory.

    Returns the absolute path to the new `.src.rpm`; the caller removes its
    directory when done (`cleanup_patched`). Name and Version do not change;
    `%{?dist}` in Release follows the local rpm macros, just as when Koji
    rebuilds the SRPM in its buildroot. On any error raises `VibeBuildError`,
    and nothing is left behind.

    When `add_unpackaged_macro=True`, the start of the spec gets
    `%define _unpackaged_files_terminate_build 0`. `allow_unsandboxed=True`
    lets rpmbuild run without bwrap where it cannot sandbox.
    """
    srpm = Path(srpm_path).resolve()
    if not srpm.is_file():
        raise SRPMNotFoundError(f"SRPM not found: {srpm}")

    target = Path(tempfile.mkdtemp(prefix="vibebuild-patch-"))
    try:
        return _patch_into(srpm, target, timeout, add_unpackaged_macro, allow_unsandboxed)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def cleanup_patched(patched_srpm: str) -> None:
    """Remove the temp directory a patch_srpm result lives in."""
    parent = Path(patched_srpm).parent
    if parent.name.startswith("vibebuild-patch-"):
        shutil.rmtree(parent, ignore_errors=True)


def _patch_into(
    srpm: Path, target: Path, timeout: int, add_unpackaged_macro: bool, allow_unsandboxed: bool
) -> str:
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
        # List argv without a shell; the spec is parsed inside bwrap
        result = subprocess.run(  # nosec B603
            _sandboxed(cmd, target, allow_unsandboxed),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VibeBuildError(f"rpmbuild -bs timed out for {srpm.name}") from exc
    if result.returncode != 0:
        raise VibeBuildError(
            f"rpmbuild -bs failed for {srpm.name}: {result.stderr.strip() or result.stdout.strip()}"
        )

    new_srpms = sorted(target.glob("*.src.rpm"))
    if not new_srpms:
        raise VibeBuildError(f"rpmbuild produced no SRPM for {srpm.name}")

    logger.info("patched spec inside %s -> %s", srpm.name, new_srpms[0].name)
    return str(new_srpms[0])
