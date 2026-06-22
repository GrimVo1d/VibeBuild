"""
SRPM and spec file analyzer for extracting BuildRequires.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from vibebuild.exceptions import InvalidSRPMError, SpecParseError


@dataclass
class BuildRequirement:
    """Represents a single build requirement."""

    name: str
    version: Optional[str] = None
    operator: Optional[str] = None

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


class SpecAnalyzer:
    """Analyzes RPM spec files to extract build information."""

    VERSION_OPERATORS = [">=", "<=", ">", "<", "=", "=="]
    MACRO_PATTERN = re.compile(r"%\{([^}]+)\}")

    def __init__(self):
        from vibebuild.name_resolver import SYSTEM_MACROS

        self._macros: dict[str, str] = dict(SYSTEM_MACROS)

    def analyze_spec(self, spec_path: str) -> PackageInfo:
        """
        Parse a spec file and extract package information.

        Args:
            spec_path: Path to the .spec file

        Returns:
            PackageInfo with extracted data

        Raises:
            FileNotFoundError: If spec file doesn't exist
            SpecParseError: If spec file cannot be parsed
        """
        spec_path = Path(spec_path)
        if not spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")

        content = spec_path.read_text(encoding="utf-8", errors="replace")
        return self._parse_spec_content(content)

    def _parse_spec_content(self, content: str) -> PackageInfo:
        """Parse spec file content."""
        lines = content.split("\n")

        name = None
        version = None
        release = None
        build_requires: list[BuildRequirement] = []
        source_urls: list[str] = []

        # First pass: collect %global and %define macros
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("%global ") or stripped.startswith("%define "):
                parts = stripped.split(None, 2)
                if len(parts) >= 3:
                    macro_name = parts[1]
                    macro_value = self._expand_macros(parts[2].strip())
                    self._macros[macro_name] = macro_value

        for line in lines:
            line = line.strip()

            if line.startswith("#"):
                continue

            if line.lower().startswith("name:"):
                name = self._extract_value(line, "Name")
                self._macros["name"] = name

            elif line.lower().startswith("version:"):
                version = self._extract_value(line, "Version")
                self._macros["version"] = version

            elif line.lower().startswith("release:"):
                release = self._extract_value(line, "Release")
                release = release.split("%")[0]

            elif line.lower().startswith("buildrequires:"):
                reqs = self._parse_build_requires(line)
                build_requires.extend(reqs)

            elif line.lower().startswith("source") and ":" in line:
                url = self._extract_value(line, line.split(":")[0])
                if url:
                    source_urls.append(self._expand_macros(url))

        if not name:
            raise SpecParseError("Could not find Name in spec file")
        if not version:
            raise SpecParseError("Could not find Version in spec file")
        if not release:
            release = "1"

        return PackageInfo(
            name=name,
            version=version,
            release=release,
            build_requires=build_requires,
            source_urls=source_urls,
        )

    def _extract_value(self, line: str, field: str) -> str:
        """Extract value after field: prefix."""
        parts = line.split(":", 1)
        if len(parts) < 2:
            return ""
        value = parts[1].strip()
        return self._expand_macros(value)

    def _expand_macros(self, value: str) -> str:
        """Expand RPM macros in value."""

        def replace_macro(match):
            macro_name = match.group(1)
            if "?" in macro_name:
                macro_name = macro_name.split("?")[0]
            return self._macros.get(macro_name, match.group(0))

        return self.MACRO_PATTERN.sub(replace_macro, value)

    def _parse_build_requires(self, line: str) -> list[BuildRequirement]:
        """Parse BuildRequires line into list of requirements."""
        requirements = []

        value = line.split(":", 1)[1].strip()

        parts = re.split(r",\s*|\s+(?=[a-zA-Z])", value)

        i = 0
        while i < len(parts):
            part = parts[i].strip()
            if not part:
                i += 1
                continue

            name = part
            version = None
            operator = None

            for op in self.VERSION_OPERATORS:
                if op in part:
                    name, rest = part.split(op, 1)
                    name = name.strip()
                    operator = op
                    version = rest.strip()
                    break

            if not version and i + 2 < len(parts):
                next_part = parts[i + 1].strip()
                if next_part in self.VERSION_OPERATORS:
                    operator = next_part
                    version = parts[i + 2].strip()
                    i += 2

            if name:
                # Try to expand any macros
                name = self._expand_macros(name)
                # Don't strip parentheses - virtual provides like python3dist(requests)
                # are valid and will be resolved by PackageNameResolver later
                if name:  # pragma: no branch
                    requirements.append(
                        BuildRequirement(name=name, version=version, operator=operator)
                    )

            i += 1

        return requirements


# One header line each for name, version and release, then one line per requirement:
# name, comparison flags and version, tab-separated. rpm expanded every macro when it
# built the SRPM, so this is exact where reading the spec text is not (%autorelease,
# %{?dist}, conditionals, %generate_buildrequires on Fedora Koji SRPMs).
_HEADER_QUERY = (
    "%{NAME}\\n%{VERSION}\\n%{RELEASE}\\n"
    "[%{REQUIRENAME}\\t%{REQUIREFLAGS:depflags}\\t%{REQUIREVERSION}\\n]"
)


def _check_srpm_path(srpm_path: str) -> Path:
    path = Path(srpm_path)
    if not path.exists():
        raise FileNotFoundError(f"SRPM not found: {srpm_path}")
    if path.suffix != ".rpm" or ".src." not in path.name:
        raise InvalidSRPMError(f"Not a valid SRPM file: {srpm_path}")
    return path


def read_srpm_header(srpm_path: str) -> PackageInfo:
    """
    Read name, version, release and BuildRequires from the SRPM header.

    Raises:
        FileNotFoundError: If the SRPM does not exist
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
        FileNotFoundError: If the SRPM does not exist
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
