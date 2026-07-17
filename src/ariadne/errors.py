from __future__ import annotations

from .types import AppError


class AriadneError(Exception):
    def __init__(self, error: AppError) -> None:
        super().__init__(error.message)
        self.error = error


def app_error(code: str, message: str, **details: object) -> AppError:
    return AppError(code=code, message=message, details=dict(details))
