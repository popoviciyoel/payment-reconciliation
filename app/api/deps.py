"""Shared FastAPI dependencies (overridable in tests)."""

from app.storage import JsonStorage, storage


def get_storage() -> JsonStorage:
    return storage
