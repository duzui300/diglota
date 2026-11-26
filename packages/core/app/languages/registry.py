"""Thread-safe registry of supported language pairs."""

from __future__ import annotations

import threading

from .en_es import EN_ES_PAIR
from .en_fr import EN_FR_PAIR
from .types import LanguagePair, LanguageProfile

DEFAULT_PAIR_ID = "en-es"
DEFAULT_PAIR = EN_ES_PAIR


class LanguageRegistry:
    """Stores available :class:`LanguagePair` definitions keyed by pair id (e.g. ``en-es``)."""

    def __init__(self, initial: tuple[LanguagePair, ...] = (EN_ES_PAIR, EN_FR_PAIR)) -> None:
        self._lock = threading.RLock()
        self._pairs: dict[str, LanguagePair] = {}
        self._profiles: dict[str, LanguageProfile] = {}
        for pair in initial:
            self.register(pair)

    def register(self, pair: LanguagePair) -> None:
        with self._lock:
            key = pair.id.strip().lower()
            self._pairs[key] = pair
            self._profiles[pair.base.code.lower()] = pair.base
            self._profiles[pair.target.code.lower()] = pair.target

    def has(self, pair_id: str | None) -> bool:
        if not pair_id:
            return False
        with self._lock:
            return pair_id.strip().lower() in self._pairs

    def get(self, pair_id: str | None = None) -> LanguagePair:
        """Resolve ``pair_id``, falling back to :data:`DEFAULT_PAIR` when omitted."""
        key = (pair_id or DEFAULT_PAIR_ID).strip().lower()
        with self._lock:
            if key in self._pairs:
                return self._pairs[key]
            raise KeyError(f"unsupported language pair: {pair_id!r}")

    def get_or_default(self, pair_id: str | None = None) -> LanguagePair:
        """Resolve ``pair_id``, falling back safely to :data:`DEFAULT_PAIR` if unknown."""
        if not pair_id:
            return DEFAULT_PAIR
        with self._lock:
            return self._pairs.get(pair_id.strip().lower(), DEFAULT_PAIR)

    def list_pairs(self) -> list[LanguagePair]:
        with self._lock:
            return list(self._pairs.values())

    def resolve_language_name(self, target: str, *, default_pair: LanguagePair | None = None) -> str:
        """Resolve a language code or name (``en``, ``es``, ``French``, ``zh``) to its display name."""
        raw = (target or "").strip()
        pair = default_pair or DEFAULT_PAIR
        if not raw:
            return pair.target.name
        lowered = raw.lower()
        with self._lock:
            if lowered in self._profiles:
                return self._profiles[lowered].name
            for profile in self._profiles.values():
                if lowered == profile.name.lower() or lowered == profile.native_name.lower():
                    return profile.name
        # Preserve explicit language names like "French" or "Chinese" rather than
        # collapsing everything non-English into Spanish.
        if lowered.startswith(pair.base.code.lower()) and len(lowered) <= len(pair.base.name):
            if pair.base.name.lower().startswith(lowered):
                return pair.base.name
        return raw


_REGISTRY = LanguageRegistry()


def get_registry() -> LanguageRegistry:
    return _REGISTRY


def register_pair(pair: LanguagePair) -> None:
    _REGISTRY.register(pair)


def get_pair(pair_id: str | None = None) -> LanguagePair:
    return _REGISTRY.get(pair_id)


def get_pair_or_default(pair_id: str | None = None) -> LanguagePair:
    return _REGISTRY.get_or_default(pair_id)


def list_pairs() -> list[LanguagePair]:
    return _REGISTRY.list_pairs()


def resolve_language_name(target: str, *, default_pair: LanguagePair | None = None) -> str:
    return _REGISTRY.resolve_language_name(target, default_pair=default_pair)
