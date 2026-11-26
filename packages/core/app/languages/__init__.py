"""Pluggable language-pair support for Diglota."""

from .en_es import EN_ES_PAIR, EN_PROFILE, ES_PROFILE, spanish_stem
from .en_fr import EN_FR_PAIR, FR_PROFILE, french_stem
from .registry import (
    DEFAULT_PAIR,
    DEFAULT_PAIR_ID,
    LanguageRegistry,
    get_pair,
    get_pair_or_default,
    get_registry,
    list_pairs,
    register_pair,
    resolve_language_name,
)
from .types import (
    CrossScriptSegmenter,
    DiglotSegmenter,
    LanguagePair,
    LanguageProfile,
    SameScriptHMMSegmenter,
    default_content_word,
    default_head_word,
    fold_text,
    slugify_text,
)

__all__ = [
    "CrossScriptSegmenter",
    "DEFAULT_PAIR",
    "DEFAULT_PAIR_ID",
    "DiglotSegmenter",
    "EN_ES_PAIR",
    "EN_FR_PAIR",
    "EN_PROFILE",
    "ES_PROFILE",
    "FR_PROFILE",
    "LanguagePair",
    "LanguageProfile",
    "LanguageRegistry",
    "SameScriptHMMSegmenter",
    "default_content_word",
    "default_head_word",
    "fold_text",
    "french_stem",
    "get_pair",
    "get_pair_or_default",
    "get_registry",
    "list_pairs",
    "register_pair",
    "resolve_language_name",
    "spanish_stem",
    "slugify_text",
]
