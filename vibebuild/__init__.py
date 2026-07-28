"""
VibeBuild - Koji extension for automatic dependency resolution and building.
"""

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"
__author__ = "VibeBuild Team"

if TYPE_CHECKING:
    from vibebuild.analyzer import get_build_requires
    from vibebuild.builder import KojiBuilder
    from vibebuild.fetcher import SRPMFetcher
    from vibebuild.name_resolver import PackageNameResolver
    from vibebuild.resolver import DependencyResolver

# Imported on first use: importing the package must not need koji, so the CLI can
# report a missing koji module instead of failing with a traceback
_EXPORTS = {
    "get_build_requires": "vibebuild.analyzer",
    "DependencyResolver": "vibebuild.resolver",
    "SRPMFetcher": "vibebuild.fetcher",
    "KojiBuilder": "vibebuild.builder",
    "PackageNameResolver": "vibebuild.name_resolver",
}

__all__ = [
    "get_build_requires",
    "DependencyResolver",
    "SRPMFetcher",
    "KojiBuilder",
    "PackageNameResolver",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'vibebuild' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
