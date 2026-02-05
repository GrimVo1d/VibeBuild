"""
Custom exceptions for VibeBuild.
"""


class VibeBuildError(Exception):
    """Base exception for VibeBuild."""

    pass


class InvalidSRPMError(VibeBuildError):
    """Raised when SRPM file is invalid or corrupted."""

    pass


class SpecParseError(VibeBuildError):
    """Raised when spec file cannot be parsed."""

    pass


class DependencyResolutionError(VibeBuildError):
    """Raised when dependencies cannot be resolved."""

    pass


class CircularDependencyError(DependencyResolutionError):
    """Raised when circular dependency is detected."""

    pass


class SRPMNotFoundError(VibeBuildError):
    """Raised when SRPM cannot be found in any source."""

    pass


class KojiBuildError(VibeBuildError):
    """Raised when Koji build fails."""

    pass


class KojiConnectionError(VibeBuildError):
    """Raised when connection to Koji hub fails."""

    pass


class NameResolutionError(VibeBuildError):
    """Raised when package name cannot be resolved."""

    pass
