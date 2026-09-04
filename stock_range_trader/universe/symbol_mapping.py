"""Lossless J-Quants to Yahoo Finance symbol conversion."""

from __future__ import annotations

import re

JQUANTS_COMMON_CODE = re.compile(r"^[0-9]{3}[0-9A-Z]0$")
YFINANCE_TSE_TICKER = re.compile(r"^[0-9]{3}[0-9A-Z]\.T$")


class SymbolMappingError(ValueError):
    """Raised when a code cannot be converted without guessing."""


def jquants_to_yfinance(code: str) -> str:
    """Convert a verified five-character common-stock code to ``XXXX.T``.

    J-Quants represents the ordinary/common issue with a fifth-character
    suffix of ``0``. The prefix is retained as text so new alphanumeric issue
    codes such as ``130A0`` become ``130A.T`` without any integer coercion.
    """

    if not isinstance(code, str):
        raise SymbolMappingError("J-Quants code must remain a string")
    normalized = code.strip().upper()
    if not JQUANTS_COMMON_CODE.fullmatch(normalized):
        raise SymbolMappingError(
            "J-Quants common-stock code must match three digits, one "
            "alphanumeric character, and suffix 0"
        )
    return f"{normalized[:4]}.T"


def yfinance_to_jquants(ticker: str) -> str:
    """Reverse a verified Tokyo Yahoo ticker without numeric conversion."""

    if not isinstance(ticker, str):
        raise SymbolMappingError("Yahoo ticker must be a string")
    normalized = ticker.strip().upper()
    if not YFINANCE_TSE_TICKER.fullmatch(normalized):
        raise SymbolMappingError("Yahoo Tokyo ticker must match XXXX.T")
    return f"{normalized[:4]}0"
