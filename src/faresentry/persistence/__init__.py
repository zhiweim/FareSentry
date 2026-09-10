"""Local persistence, isolated from flight providers and agent judgment."""

from faresentry.persistence.sqlite import SQLiteFareHistory

__all__ = ["SQLiteFareHistory"]
