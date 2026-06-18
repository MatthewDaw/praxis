"""Data access layer — UI-agnostic integration boundary for Matthew's API."""

from services.data_provider import DataProvider, get_data_provider
from services.mock_provider import MockDataProvider

__all__ = ["DataProvider", "MockDataProvider", "get_data_provider"]
