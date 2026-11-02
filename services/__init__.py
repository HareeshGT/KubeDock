"""Shared in-process application services for KubeDock."""

from .connection import ConnectionService, ConnectionSnapshot
from .runtime import AppServices

__all__ = ["AppServices", "ConnectionService", "ConnectionSnapshot"]
