"""Market domain module exports.

Keep the package import light. Compatibility registries are loaded lazily so
callers that only need Market Catalog facts do not construct legacy facades.
"""

from importlib import import_module

from .catalog import (
    MARKET_CATALOG,
    MarketCapabilities,
    MarketCatalog,
    MarketCatalogEntry,
    MarketCatalogError,
    get_market_catalog,
)
from .calendar_policy import (
    CalendarProvider,
    CalendarSessionOverride,
)
from .market import (
    SUPPORTED_MARKET_CODE_ORDER,
    SUPPORTED_MARKET_CODES,
    Market,
    UnsupportedMarketError,
    normalize_market_codes,
)
from .mic import MicFacts

_LAZY_EXPORTS = {
    "BenchmarkFacts": (".registry", "BenchmarkFacts"),
    "MarketProfile": (".registry", "MarketProfile"),
    "MarketRegistry": (".registry", "MarketRegistry"),
    "market_registry": (".registry", "market_registry"),
    "MicAliasDefinition": (".mic_aliases", "MicAliasDefinition"),
    "MicAliasRegistry": (".mic_aliases", "MicAliasRegistry"),
    "MicAliasResolution": (".mic_aliases", "MicAliasResolution"),
    "mic_alias_registry": (".mic_aliases", "mic_alias_registry"),
    "MarketSymbolSuffixDefinition": (
        ".symbol_suffixes",
        "MarketSymbolSuffixDefinition",
    ),
    "MarketSymbolSuffixRegistry": (
        ".symbol_suffixes",
        "MarketSymbolSuffixRegistry",
    ),
    "market_symbol_suffix_registry": (
        ".symbol_suffixes",
        "market_symbol_suffix_registry",
    ),
}


def __getattr__(name: str) -> object:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "MARKET_CATALOG",
    "Market",
    "MarketCapabilities",
    "MarketCatalog",
    "MarketCatalogEntry",
    "MarketCatalogError",
    "MarketProfile",
    "MarketRegistry",
    "BenchmarkFacts",
    "CalendarProvider",
    "CalendarSessionOverride",
    "MicFacts",
    "MicAliasDefinition",
    "MicAliasRegistry",
    "MicAliasResolution",
    "MarketSymbolSuffixDefinition",
    "MarketSymbolSuffixRegistry",
    "SUPPORTED_MARKET_CODE_ORDER",
    "SUPPORTED_MARKET_CODES",
    "UnsupportedMarketError",
    "get_market_catalog",
    "market_registry",
    "mic_alias_registry",
    "market_symbol_suffix_registry",
    "normalize_market_codes",
]
