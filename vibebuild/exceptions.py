"""
Custom exceptions for VibeBuild.
"""


class VibeBuildError(Exception):
    """Base exception for VibeBuild."""


class InvalidSRPMError(VibeBuildError):
    """Raised when SRPM file is invalid or corrupted."""


class DependencyResolutionError(VibeBuildError):
    """Raised when dependencies cannot be resolved."""


class CircularDependencyError(DependencyResolutionError):
    """Raised when circular dependency is detected."""


class SRPMNotFoundError(VibeBuildError):
    """Raised when an SRPM file is missing or cannot be found in any source."""


class KojiBuildError(VibeBuildError):
    """Raised when Koji build fails."""


class KojiConnectionError(VibeBuildError):
    """Raised when connection to Koji hub fails."""
