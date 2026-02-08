"""
SRPM and spec file analyzer for extracting package metadata.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from vibebuild.exceptions import SpecParseError


@dataclass
class PackageInfo:
    """Container for parsed package metadata."""

    name: str
    version: str
    release: str
    source_urls: list[str] = field(default_factory=list)

    @property
    def nvr(self) -> str:
        return f"{self.name}-{self.version}-{self.release}"


class SpecAnalyzer:
    """Parse RPM .spec files and extract Name/Version/Release."""

    def __init__(self) -> None:
        self._macros: dict[str, str] = {}

    def analyze_spec(self, spec_path: str) -> PackageInfo:
        spec_file = Path(spec_path)
        if not spec_file.exists():
            raise SpecParseError(f"Spec file not found: {spec_path}")

        content = spec_file.read_text(encoding="utf-8", errors="replace")
        name: Optional[str] = None
        version: Optional[str] = None
        release: Optional[str] = None
        source_urls: list[str] = []

        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("name:"):
                name = self._extract_value(line, "Name")
                self._macros["name"] = name or ""
            elif line.lower().startswith("version:"):
                version = self._extract_value(line, "Version")
                self._macros["version"] = version or ""
            elif line.lower().startswith("release:"):
                release = self._extract_value(line, "Release")
                if release:
                    release = release.split("%")[0]
            elif re.match(r"^source\d*\s*:", line, re.IGNORECASE):
                value = line.split(":", 1)[1].strip()
                if value:
                    source_urls.append(self._expand_macros(value))

        if not name:
            raise SpecParseError(f"Spec file missing Name: {spec_path}")
        if not version:
            raise SpecParseError(f"Spec file missing Version: {spec_path}")

        return PackageInfo(
            name=name,
            version=version,
            release=release or "1",
            source_urls=source_urls,
        )

    def _extract_value(self, line: str, key: str) -> str:
        if ":" not in line:
            return ""
        return self._expand_macros(line.split(":", 1)[1].strip())

    def _expand_macros(self, value: str) -> str:
        for _ in range(8):
            match = re.search(r"%\{([^}]+)\}", value)
            if not match:
                break
            macro_name = match.group(1)
            replacement = self._macros.get(macro_name, "")
            value = value.replace(match.group(0), replacement)
        return value
