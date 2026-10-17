"""
base.py
=======
Abstract base class and data models for application adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AdapterResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class AppAdapter(ABC):
    """
    Abstract interface for application adapters.
    Each adapter exposes a fixed list of actions with validated parameters.
    Unknown actions must be rejected.
    """

    name: str = ""
    aliases: list[str] = []
    actions: list[str] = []

    @abstractmethod
    def detect(self) -> bool:
        """Check if the target application is installed and available."""
        ...

    @abstractmethod
    def launch(self) -> bool:
        """Launch the application."""
        ...

    @abstractmethod
    def execute(self, action: str, **params: Any) -> AdapterResult:
        """Execute a validated adapter action."""
        ...

    def verify(self, action: str, result: AdapterResult) -> bool:
        """Verify the action completed successfully."""
        return result.success

