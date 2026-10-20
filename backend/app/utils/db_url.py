"""Database URL helpers."""

from __future__ import annotations

from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError


def redacted_database_url(database_url: str) -> str:
    """Return the URL with the password masked, suitable for logs/reports.

    Uses SQLAlchemy's built-in ``render_as_string(hide_password=True)`` so
    the masking format is consistent with how SQLAlchemy reports URLs in
    its own error messages.
    """
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except (TypeError, ValueError, AttributeError, SQLAlchemyError):
        return "<invalid>"
