"""Patching spec files inside an SRPM before sending to koji.

Applied automatically to all SRPMs (the target and dependencies) before submit.
Two transformations are applied:

1. `%define _unpackaged_files_terminate_build 0` is added at the start of the spec,
   so that unpackaged files left in BUILDROOT do not fail the build.
2. The `%check` section is cut out entirely: upstream testsuites often require
   network access, specific pid/process limits and other things that our
   sandbox mock does not have; instead we rely on a separate smoke install after
   tagging.

Both mechanisms are standard RPM/Fedora: the macro is documented in upstream
`/usr/lib/rpm/macros`, `%check` is an ordinary optional spec section.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

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


def _section_token(line: str) -> Optional[str]:
    """Return the section name if the line starts one, otherwise None."""
    stripped = line.lstrip()
    if not stripped.startswith("%"):
        return None
    head = stripped.split(None, 1)[0]
    name = head[1:].split("(", 1)[0]
    return name if name in _SECTION_NAMES else None


def patch_spec_text(spec: str) -> str:
    """Apply both edits to the spec text. Idempotent."""
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
    if not has_macro:
        body = UNPACKAGED_MACRO + "\n" + body
    return body


def _extract_srpm(srpm: Path, dest: Path, timeout: int) -> None:
    """Unpack an SRPM via rpm2cpio | cpio into dest. Without shell=True."""
    rpm2cpio = subprocess.Popen(
        ["rpm2cpio", str(srpm)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        cpio = subprocess.Popen(
            ["cpio", "-idm", "--quiet"],
            stdin=rpm2cpio.stdout,
            cwd=str(dest),
            stderr=subprocess.PIPE,
        )
        if rpm2cpio.stdout is not None:
            rpm2cpio.stdout.close()
        try:
            _, cpio_err = cpio.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            cpio.kill()
            raise VibeBuildError(f"cpio timed out extracting {srpm.name}")
    finally:
        if rpm2cpio.poll() is None:
            rpm2cpio.kill()
    if cpio.returncode != 0:
        raise VibeBuildError(
            f"cpio failed extracting {srpm.name} (rc={cpio.returncode}): "
            f"{cpio_err.decode(errors='replace') if cpio_err else ''}"
        )


def patch_srpm(srpm_path: str, work_dir: Path, timeout: int = 600) -> str:
    """Unpack the SRPM, patch the spec, rebuild a new SRPM alongside.

    Returns the absolute path to the new `.src.rpm`. The NVR is preserved because
    Version/Release in the spec do not change. On any error raises
    `VibeBuildError`, so the calling code can catch it and fall back to the original
    SRPM.
    """
    srpm = Path(srpm_path).resolve()
    if not srpm.exists():
        raise VibeBuildError(f"SRPM not found: {srpm}")

    target = Path(work_dir) / f"{srpm.stem}-patched"
    sources_dir = target / "SOURCES"
    specs_dir = target / "SPECS"
    target.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(exist_ok=True)
    specs_dir.mkdir(exist_ok=True)

    _extract_srpm(srpm, sources_dir, timeout)

    spec_files = sorted(sources_dir.glob("*.spec"))
    if not spec_files:
        raise VibeBuildError(f"No spec file found inside {srpm.name}")
    if len(spec_files) > 1:
        logger.warning("multiple specs in %s, picking %s", srpm.name, spec_files[0].name)

    spec_dst = specs_dir / spec_files[0].name
    spec_files[0].rename(spec_dst)
    spec_dst.write_text(patch_spec_text(spec_dst.read_text()))

    result = subprocess.run(
        [
            "rpmbuild",
            "-bs",
            "--nodeps",
            "--define",
            f"_topdir {target}",
            "--define",
            f"_sourcedir {sources_dir}",
            "--define",
            f"_specdir {specs_dir}",
            "--define",
            f"_srcrpmdir {target}",
            "--define",
            f"_builddir {target}/BUILD",
            str(spec_dst),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise VibeBuildError(
            f"rpmbuild -bs failed for {srpm.name}: {result.stderr.strip() or result.stdout.strip()}"
        )

    new_srpms = sorted(target.glob("*.src.rpm"))
    if not new_srpms:
        raise VibeBuildError(f"rpmbuild produced no SRPM for {srpm.name}")

    logger.info("patched spec inside %s -> %s", srpm.name, new_srpms[0].name)
    return str(new_srpms[0])
