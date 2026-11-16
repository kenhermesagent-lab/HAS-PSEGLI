"""Exceptions for the PSEG Long Island integration."""

from __future__ import annotations


class PsegliError(Exception):
    """Base error for the integration."""


class PsegliAuthError(PsegliError):
    """The session cookie was rejected or has expired."""


class PsegliConnectionError(PsegliError):
    """The PSEG portal could not be reached or answered unexpectedly."""
