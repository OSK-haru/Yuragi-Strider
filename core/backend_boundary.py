"""Backend selection boundary for Yuragi-Strider simulation runners."""

from __future__ import annotations

from importlib import import_module
from typing import Any


PYTHON_DENSE_BACKEND = "python_dense"
RUST_DENSE_PREVIEW_BACKEND = "rust_dense_preview"
RUST_RESET_SEQUENCE_SCHEMA_VERSION = "local_reset_sequence_v1"
SUPPORTED_SIMULATION_BACKENDS = {
    PYTHON_DENSE_BACKEND,
    RUST_DENSE_PREVIEW_BACKEND,
}
PYTHON_DENSE_BACKEND_NAME = "python_dense_streaming_v1"
BACKEND_VERSION = "7.10"
RUST_PREVIEW_FALLBACK_REASON = (
    "rust_dense_preview did not execute; Python dense fallback or an earlier "
    "validation/runtime guard handled the result"
)


def rust_backend_status() -> dict[str, Any]:
    """Return import/smoke metadata for the optional Rust preview module."""

    try:
        module = import_module("yuragi_strider_rust")
    except Exception as exc:  # pragma: no cover - exact import errors vary locally.
        return {
            "available": False,
            "name": "",
            "reason": str(exc),
        }

    backend_name = getattr(module, "backend_name", None)
    if backend_name is None:
        return {
            "available": False,
            "name": "",
            "reason": "yuragi_strider_rust.backend_name() is missing",
        }
    try:
        name = str(backend_name())
    except Exception as exc:  # pragma: no cover - defensive optional backend guard.
        return {
            "available": False,
            "name": "",
            "reason": f"yuragi_strider_rust.backend_name() failed: {exc}",
        }
    return {
        "available": True,
        "name": name,
        "reason": "",
    }


def is_rust_backend_available() -> bool:
    """Return whether the optional Rust preview module is importable."""

    return bool(rust_backend_status()["available"])


def get_rust_backend_name() -> str:
    """Return the Rust module backend name, or an empty string when unavailable."""

    return str(rust_backend_status()["name"])


def backend_metadata(
    requested_backend: str,
    rust_kernel_used: bool = False,
    rust_kernel_fallback_used: bool = False,
    rust_kernel_fallback_reason: str = "",
) -> dict[str, Any]:
    """Return JSON-safe diagnostics for the requested simulation backend."""

    requested_backend = str(requested_backend or PYTHON_DENSE_BACKEND)
    if requested_backend == PYTHON_DENSE_BACKEND:
        return {
            "backend_requested": PYTHON_DENSE_BACKEND,
            "backend_name": PYTHON_DENSE_BACKEND_NAME,
            "backend_version": BACKEND_VERSION,
            "backend_available": True,
            "backend_fallback_used": False,
            "backend_fallback_reason": "",
        }

    if requested_backend == RUST_DENSE_PREVIEW_BACKEND:
        rust_status = rust_backend_status()
        backend_fallback_used = bool(rust_kernel_fallback_used or not rust_kernel_used)
        fallback_reason = rust_kernel_fallback_reason
        if backend_fallback_used and not fallback_reason:
            fallback_reason = (
                str(rust_status["reason"])
                if not rust_status["available"]
                else RUST_PREVIEW_FALLBACK_REASON
            )
        return {
            "backend_requested": RUST_DENSE_PREVIEW_BACKEND,
            "backend_name": (
                str(rust_status["name"]) if rust_kernel_used and not backend_fallback_used
                else PYTHON_DENSE_BACKEND_NAME
            ),
            "backend_version": BACKEND_VERSION,
            "backend_available": bool(rust_status["available"]),
            "backend_fallback_used": backend_fallback_used,
            "backend_fallback_reason": fallback_reason,
            "rust_module_available": bool(rust_status["available"]),
            "rust_module_name": str(rust_status["name"]),
            "rust_module_error": str(rust_status["reason"]),
        }

    return {
        "backend_requested": requested_backend,
        "backend_name": "",
        "backend_version": BACKEND_VERSION,
        "backend_available": False,
        "backend_fallback_used": False,
        "backend_fallback_reason": f"unsupported simulation backend: {requested_backend}",
    }
