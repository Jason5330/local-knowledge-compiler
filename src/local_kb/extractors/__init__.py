"""Installed local extractors and their shared registry."""

from . import html, office, pdf, text  # noqa: F401
from .base import registry

__all__ = ["registry"]
