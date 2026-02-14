"""
SRPM and spec file analyzer for extracting BuildRequires.
"""

import re
import subprocess
import tempfile
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


def get_build_requires(srpm_path: str) -> list[str]:
    """
    Extract BuildRequires from SRPM file.

    This is a convenience function that extracts the spec file from SRPM
    and returns list of package names required for building.

    Args:
        srpm_path: Path to .src.rpm file

    Returns:
        List of package names (without versions)

    Raises:
        FileNotFoundError: If SRPM doesn't exist
        InvalidSRPMError: If SRPM is invalid
    """
    srpm_path = Path(srpm_path)
    if not srpm_path.exists():
        raise FileNotFoundError(f"SRPM not found: {srpm_path}")

    if not srpm_path.suffix == ".rpm" or ".src." not in srpm_path.name:
        raise InvalidSRPMError(f"Not a valid SRPM file: {srpm_path}")

    with tempfile.TemporaryDirectory():
        try:
            subprocess.run(
                ["rpm2cpio", str(srpm_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise InvalidSRPMError(f"Failed to extract SRPM: {e.stderr.decode()}")
        except FileNotFoundError:
            raise InvalidSRPMError("rpm2cpio not found. Install rpm package.")

        result = subprocess.run(
            ["rpm", "-qp", "--requires", str(srpm_path)], capture_output=True, text=True
        )

        if result.returncode != 0:
            raise InvalidSRPMError(f"Failed to query SRPM: {result.stderr}")

        requires = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            for op in SpecAnalyzer.VERSION_OPERATORS:
                if op in line:
                    line = line.split(op)[0].strip()
                    break

            if line.startswith("rpmlib(") or line.startswith("/"):
                continue

            if line and line not in requires:
                requires.append(line)

        return requires


def get_package_info_from_srpm(srpm_path: str) -> PackageInfo:
    """
    Extract full package information from SRPM.

    Args:
        srpm_path: Path to .src.rpm file

    Returns:
        PackageInfo with all extracted data
    """
    srpm_path = Path(srpm_path)
    if not srpm_path.exists():
        raise FileNotFoundError(f"SRPM not found: {srpm_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            f"rpm2cpio {srpm_path} | cpio -idmv", shell=True, cwd=tmpdir, capture_output=True
        )

        spec_files = list(Path(tmpdir).glob("*.spec"))
        if not spec_files:
            raise InvalidSRPMError(f"No spec file found in SRPM: {srpm_path}")

        analyzer = SpecAnalyzer()
        return analyzer.analyze_spec(str(spec_files[0]))
