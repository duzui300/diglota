"""Built-in English -> French (``en-fr``) language pair profile.

Demonstrates how a second same-script language pair plugs into Diglota with
zero changes to the core parsing, glossary, reading-exposure, or storage logic.
"""

from __future__ import annotations

import re

from .en_es import EN_FEEDS, EN_HMM_WORDS, EN_PROFILE
from .types import (
    LanguagePair,
    LanguageProfile,
    SameScriptHMMSegmenter,
    default_content_word,
    fold_text,
)

# --------------------------------------------------------------------------- #
# French morphology
# --------------------------------------------------------------------------- #

_FR_ENDING = re.compile(
    r"(?:aient|erait|irait|ions|iez|ons|ez|ent|ant|ais|ait|er|ir|re|es|s|e)$"
)
_FR_MIN_STEM = 3


def french_stem(word: str) -> str:
    """Lightweight French stemmer for vocabulary coverage and glossary matching."""
    text = fold_text(default_content_word(word))
    # Strip elided leading articles/pronouns like ``l'art`` or ``d'une`` if attached.
    if "'" in text or "’" in text:
        text = re.split(r"['’]", text)[-1]
    trimmed = _FR_ENDING.sub("", text)
    return (trimmed if len(trimmed) >= _FR_MIN_STEM else text)[:6]


# --------------------------------------------------------------------------- #
# Stopwords & function words (auto-folded inside LanguageProfile)
# --------------------------------------------------------------------------- #

_FR_CORE_STOP = frozenset(
    """de du des le la les un une au aux et ou mais donc or ni car en dans sur
    sous par pour avec sans entre chez vers à ce cet cette ces mon ton son ma ta
    sa mes tes ses notre votre leur nos vos leurs je tu il elle on nous vous ils
    elles me te se lui y que qui quoi dont où ne pas plus moins très bien mal
    est sont était étaient fut furent être avoir a ont avait avaient fait font
    faire tout tous toute toutes""".split()
)

_FR_FUNCTION_WORDS = _FR_CORE_STOP | frozenset(
    """comme quand si aussi même autre autres chaque quelque quelques plusieurs
    aucun aucune rien personne toujours jamais encore déjà ici là alors puis
    après avant pendant depuis jusqu'à parce pourquoi comment combien quel quelle
    quels quelles celui celle ceux celles ceci cela ça peut peuvent pouvait
    doit doivent veut veulent savoir sait savent dire dit disent aller va vont
    venir vient viennent voir voit voient prendre prend prennent""".split()
)

_FR_DERIVED_SUFFIXES = (
    "ment",
    "tion", "sion", "ation",
    "ite", "ete",
    "isme", "iste",
    "eur", "euse", "teur", "trice",
    "able", "ible",
    "ance", "ence",
    "ique", "eux", "euse",
    "age", "ure", "ette",
)

# --------------------------------------------------------------------------- #
# HMM lexicons for EN / FR diglot segmentation
# --------------------------------------------------------------------------- #

FR_HMM_WORDS = _FR_FUNCTION_WORDS | frozenset(
    """
    art artiste artistes oeuvre œuvre oeuvres œuvres monde humain humains
    humanité vie temps année années an ans siècle siècles histoire culture
    langage langue langues mot mots texte textes idée idées esprit cerveau
    pensée connaissance question questions réponse réponses forme formes manière
    façon partie parties chose choses cas exemple exemples changement changements
    processus système systèmes modèle modèles données information technologie
    machine machines outil outils travail travaux développement croissance
    avenir futur passé présent réel réalité vérité sens signification valeur
    valeurs pouvoir force raison raisons effet effets cause causes résultat
    résultats problème problèmes solution solutions sujet thèmes point points
    lieu lieux endroit groupe groupes société social personne personnes gens
    nom noms nombre numéro nouveau nouvelle nouveaux nouvelles vieux vieille
    grand grande grands grandes petit petite meilleur pire premier dernier propre
    possible impossible important différente différent différents général commun
    simple complexe difficile facile clair sombre comprendre apprendre enseigner
    montrer parler écrire écrit lire lit penser pense croire croit créer crée
    produire générer changer suivre suit réussir permettre sembler semble rester
    reste devenir devient falloir faut seulement pourtant cependant toutefois
    peut-être parfois souvent bientôt maintenant désormais ainsi grâce afin
    """.split()
)

EN_FR_AMBIGUOUS = frozenset(
    "a an on or in to as at by me he son par pour art air fin plan point "
    "simple general real possible impossible important culture history".split()
)

FR_ACCENTS = frozenset("àâäæçéèêëîïôœùûüÿÀÂÄÆÇÉÈÊËÎÏÔŒÙÛÜŸ«»")
FR_SUFFIXES = ("tion", "tions", "ment", "ments", "ique", "iques", "eur", "euse", "aient", "ité", "ités")
EN_SUFFIXES_FOR_FR = ("ing", "ough", "ness", "ship", "ight", "ould")

FR_WORD_PATTERN = r"[A-Za-zÀÂÄÆÇÉÈÊËÎÏÔŒÙÛÜŸàâäæçéèêëîïôœùûüÿ]+"

# --------------------------------------------------------------------------- #
# Profile & Pair
# --------------------------------------------------------------------------- #

FR_PROFILE = LanguageProfile(
    code="fr",
    name="French",
    native_name="Français",
    script="latin",
    word_spaced=True,
    cased=True,
    proficiency_scale="CEFR",
    word_pattern=FR_WORD_PATTERN,
    accent_chars=FR_ACCENTS,
    stopwords=_FR_FUNCTION_WORDS,
    function_words=_FR_FUNCTION_WORDS,
    derived_suffixes=_FR_DERIVED_SUFFIXES,
    sentence_punct=".!?…«»\"'“‘-–—(",
    abbreviations=("M", "Mme", "Mlle", "Dr", "Pr", "St", "Ste", "etc", "env", "ex", "cf"),
    tts_locales=("fr-FR", "fr-CA", "fr-BE", "fr-CH", "fr"),
    tts_rate=0.94,
    stemmer=french_stem,
)

EN_FR_SEGMENTER = SameScriptHMMSegmenter(
    base_code="en",
    target_code="fr",
    base_words=EN_HMM_WORDS,
    target_words=FR_HMM_WORDS,
    ambiguous_words=EN_FR_AMBIGUOUS,
    target_accents=FR_ACCENTS,
    target_suffixes=FR_SUFFIXES,
    base_suffixes=EN_SUFFIXES_FOR_FR,
    word_re=re.compile(FR_WORD_PATTERN),
)

EN_FR_SHELVES = (
    ("gentle", 0.00, 0.45, "Mostly English, French in short phrases"),
    ("steady", 0.45, 0.62, "A real weave, roughly half French"),
    ("dense", 0.62, 0.78, "French carries whole sentences"),
    ("immersion", 0.78, 1.01, "French-dominant"),
)

EN_FR_PAIR = LanguagePair(
    id="en-fr",
    base=EN_PROFILE,
    target=FR_PROFILE,
    segmenter=EN_FR_SEGMENTER,
    shelves=EN_FR_SHELVES,
    accept_language="en-US,en;q=0.9,fr;q=0.8",
    wikipedia_host="en.wikipedia.org",
    default_feeds=EN_FEEDS,
    min_probe_length=5,
    write_placeholder="Écrivez ici. Ne cherchez pas les mots, utilisez ceux que vous avez.",
)
