"""Provider interfaces and shared error/status types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd


class ProviderError(RuntimeError):
    """Base class for external market-data failures."""


class ProviderAuthenticationError(ProviderError):
    """Raised when a provider credential is missing or invalid."""


class ProviderDownloadError(ProviderError):
    """Raised after a provider request exhausts its retry policy."""


@dataclass(frozen=True, slots=True)
class DownloadIssue:
    """One symbol-level issue that must remain visible to callers."""

    symbol: str
    status: str
    message: str


class PriceDataProvider(ABC):
    """Abstract source of daily bars in the canonical Phase 2 schema.

    The interval is half open: ``start`` is inclusive and ``end`` is
    exclusive, regardless of the upstream provider's native convention.
    """

    name: str
    adjustment_mode: str

    @abstractmethod
    def get_daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Return canonical daily bars for ``start <= date < end``."""


class UniverseProvider(ABC):
    """Abstract source of point-in-time listed-security metadata."""

    @abstractmethod
    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        """Return the provider-native master snapshot for ``as_of_date``."""
