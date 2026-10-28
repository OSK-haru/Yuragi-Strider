"""Shared, provenance-aware analysis contracts for security experiments."""

from core.security_analysis.binary_contrast import (
    BINARY_CONTRAST_SCHEMA_VERSION,
    BinaryContrastReport,
    BinaryContrastRequest,
    ObservationSource,
    analyze_binary_contrast,
)

__all__ = [
    "BINARY_CONTRAST_SCHEMA_VERSION",
    "BinaryContrastReport",
    "BinaryContrastRequest",
    "ObservationSource",
    "analyze_binary_contrast",
]
