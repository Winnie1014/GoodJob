"""Errors that are safe to return from the local command boundary."""

from __future__ import annotations


class GoodJobError(Exception):
    """Base error with a stable machine-readable code."""

    code = "goodjob_error"


class InvalidInputError(GoodJobError):
    code = "invalid_input"


class CapabilityError(GoodJobError):
    code = "authorization_session_mismatch"


class WriterBusyError(GoodJobError):
    code = "writer_busy"


class UnsupportedSchemaError(GoodJobError):
    code = "unsupported_schema"


class UnsupportedPlatformError(GoodJobError):
    code = "unsupported_platform"


class MissingDependencyError(GoodJobError):
    code = "missing_dependency"


class PermissionRequiredError(GoodJobError):
    code = "permission_required"


class UnsupportedCapabilityError(GoodJobError):
    code = "unsupported_capability"
