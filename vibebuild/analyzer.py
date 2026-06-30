"""
SRPM header reader: name, version, release and BuildRequires.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from vibebuild.exceptions import InvalidSRPMError, SRPMNotFoundError


@dataclass
class BuildRequirement:
    """Represents a single build requirement."""

    name: str
    version: str | None = None
    operator: str | None = None

    def __str__(self) -> str:
        if self.version and self.operator:
            return f"{self.name} {self.operator} {self.version}"
        return self.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other) -> bool:
        if isinstance(other, BuildRequirement):
            return self.name == other.name
        return False


@dataclass
class PackageInfo:
    """Information extracted from spec/SRPM."""

    name: str
    version: str
    release: str
    build_requires: list[BuildRequirement]
    source_urls: list[str]

    @property
    def nvr(self) -> str:
        return f"{self.name}-{self.version}-{self.release}"


_HEADER_QUERY = (
    "%{NAME}\\n%{VERSION}\\n%{RELEASE}\\n"
    "[%{REQUIRENAME}\\t%{REQUIREFLAGS:depflags}\\t%{REQUIREVERSION}\\n]"
)


def _check_srpm_path(srpm_path: str) -> Path:
    path = Path(srpm_path)
    if not path.exists():
        raise SRPMNotFoundError(f"SRPM not found: {srpm_path}")
    if path.suffix != ".rpm" or ".src." not in path.name:
        raise InvalidSRPMError(f"Not a valid SRPM file: {srpm_path}")
    return path


def read_srpm_header(srpm_path: str) -> PackageInfo:
    """
    Read name, version, release and BuildRequires from the SRPM header.

    Raises:
        SRPMNotFoundError: If the SRPM does not exist
        InvalidSRPMError: If the file is not an SRPM or rpm cannot read it
    """
    path = _check_srpm_path(srpm_path)
    try:
        result = subprocess.run(
            ["rpm", "-qp", "--qf", _HEADER_QUERY, str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise InvalidSRPMError("rpm not found. Install the rpm package.") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidSRPMError(f"Reading the header of {path} timed out") from exc
    if result.returncode != 0:
        raise InvalidSRPMError(f"Failed to query SRPM {path}: {result.stderr.strip()}")

    lines = result.stdout.split("\n")
    if len(lines) < 3 or not all(lines[:3]):
        raise InvalidSRPMError(f"Unexpected rpm output for {path}")
    name, version, release = lines[:3]

    build_requires: list[BuildRequirement] = []
    for line in lines[3:]:
        if not line.strip():
            continue
        req_name, flags, req_version = (line.split("\t") + ["", ""])[:3]
        # rpmlib() entries are rpm features, file paths are not resolved by name
        if req_name.startswith("rpmlib(") or req_name.startswith("/"):
            continue
        req = BuildRequirement(
            name=req_name,
            version=req_version or None,
            operator=flags or None,
        )
        if req not in build_requires:
            build_requires.append(req)

    return PackageInfo(
        name=name,
        version=version,
        release=release,
        build_requires=build_requires,
        source_urls=[],
    )


def get_build_requires(srpm_path: str) -> list[BuildRequirement]:
    """
    Return the BuildRequires of an SRPM, with version constraints.

    Raises:
        SRPMNotFoundError: If the SRPM does not exist
        InvalidSRPMError: If the file is not an SRPM or rpm cannot read it
    """
    return read_srpm_header(srpm_path).build_requires


def get_package_info_from_srpm(srpm_path: str, use_cache: bool = True) -> PackageInfo:
    """
    Extract package information from an SRPM header.

    Args:
        srpm_path: Path to .src.rpm file
        use_cache: use the persistent parse cache (default True)

    Returns:
        PackageInfo with all extracted data
    """
    _check_srpm_path(srpm_path)

    if use_cache:
        try:
            from vibebuild import _spec_cache

            cached = _spec_cache.get(srpm_path)
            if cached is not None:
                return cached
        except Exception:
            # any cache problem must not break the main work
            pass

    info = read_srpm_header(srpm_path)

    if use_cache:
        try:
            from vibebuild import _spec_cache

            _spec_cache.put(srpm_path, info)
        except Exception:
            pass

    return info
