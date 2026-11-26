"""Built-in English -> Spanish (``en-es``) language pair profile.

Consolidates the Spanish/English lexicons, stopwords, stemming rules, shelf
bands, feeds, and segmentation parameters that previously lived across half a
dozen modules. Every stopword set is auto-folded by :class:`LanguageProfile`,
so accented forms like ``está``, ``están``, ``había``, ``más``, ``aquí`` and
``así`` always match folded tokens cleanly.
"""

from __future__ import annotations

import re

from .types import (
    LanguagePair,
    LanguageProfile,
    SameScriptHMMSegmenter,
    default_content_word,
    fold_text,
)

# --------------------------------------------------------------------------- #
# Spanish morphology
# --------------------------------------------------------------------------- #

_ES_REFLEXIVE = re.compile(r"(?:se|me|te|nos|os|lo|la|le|los|las|les)$")
_ES_ENDING = re.compile(r"(?:ando|iendo|ado|ido|ar|er|ir|es|os|as|o|a|e|s)$")
_ES_MIN_STEM = 3


def spanish_stem(word: str) -> str:
    """Crude Spanish stemmer matching inflections, clitics, and o->ue / e->ie stems."""
    text = fold_text(default_content_word(word))
    if len(text) > 6:
        text = _ES_REFLEXIVE.sub("", text)
    text = text.replace("ue", "o").replace("ie", "e")
    trimmed = _ES_ENDING.sub("", text)
    return (trimmed if len(trimmed) >= _ES_MIN_STEM else text)[:6]


# --------------------------------------------------------------------------- #
# Stopwords & function words (auto-folded inside LanguageProfile)
# --------------------------------------------------------------------------- #

_ES_CORE_STOP = frozenset(
    """de la el los las un una unos unas que y e o u en por para con sin sobre
    entre al del a se le les lo su sus mi tu es son era eran fue fueron ser
    estar está están ni no sí si te os nos me más mas menos muy ya""".split()
)

_ES_FUNCTION_WORDS = _ES_CORE_STOP | frozenset(
    """estaba han ha he hay había habia tan todo toda todos todas esta este esto
    estos estas ese esa eso esos esas como será sera puede pueden podía podia
    tener tiene tienen hace hacen hacer pero aunque sino también tambien siempre
    nunca ahora antes después despues luego aquí aqui allí alli así asi cada
    desde hasta cuando donde quien quienes cual porque pues solo sólo mismo misma
    otro otra otros otras nada algo alguien nadie bien mal sea sean fuera fuese
    siendo sido cuál aún aun todavía todavia casi tanto tanta tantos tantas""".split()
)

_ES_STOPWORDS = _ES_FUNCTION_WORDS | frozenset(
    """aquel aquella ello ellos ellas nosotras vosotros vosotras cuales algun
    alguna ningun ninguna tenía tenia debe deben eres somos sois estábamos
    estaban haber qué cómo dónde cuándo cuánto""".split()
)

_EN_STOPWORDS = frozenset(
    """the and for that with this from have been were are was will would could should
    they them their there here what when where which while whose about into over
    than then they're more most some such only also just very much many other
    these those your you're our ours its it's does did doing done make makes made
    take takes taken give gives given come comes came goes went going said says
    say see sees seen look looks looked know knows known think thinks thought
    a an of in on at by to as is am it or but not no nor so if be been being do
    has had can may might must my me we us he she his her him one two all any
    out up down off own same too why how who whom both each few per via within
    without across along around near upon toward towards against between among
    during before after above below under shall i your something someone thing
    things way ways able like get gets got put puts use used using again once
    because""".split()
)

_ES_DERIVED_SUFFIXES = (
    "mente",
    "idad", "edad", "dad",
    "cion", "sion", "zon",
    "miento", "mientos",
    "oso", "osa", "osos", "osas",
    "ivo", "iva", "ivos", "ivas",
    "able", "ables", "ible", "ibles",
    "ancia", "encia",
    "eza", "ura",
    "ismo", "ista",
    "isimo", "isima",
    "ito", "ita", "illo", "illa",
    "ador", "adora", "edor", "ente", "ante",
)

# --------------------------------------------------------------------------- #
# HMM lexicons for EN / ES diglot segmentation
# --------------------------------------------------------------------------- #

ES_HMM_WORDS = frozenset(
    """
    de la el los las un una unos unas que y e o u en por para con sin sobre entre
    hasta desde segun según durante mediante se le les lo su sus mi tu es son era
    eran fue fueron ser estar está están estaba estaban estan han ha he hay había
    habia puede pueden podía podia tiene tienen tenía tenia hacer hace hacen más
    mas menos muy también tambien pero sino aunque porque pues como cuando donde
    quien quienes cual cuales este esta esto estos estas ese esa eso esos esas
    aquel aquella al del ni ya aún aun todavía todavia siempre nunca ahora antes
    después despues luego aquí aqui allí alli así asi tan tanto todo toda todos
    todas otro otra otros otras mismo misma cada algún algun alguna ningún ninguna
    nada nadie algo alguien tal tales pues sea sean fuera fuese siendo sido esta
    éste ésta aquello cuyo cuya cuyos cuyas ambos ambas cualquiera quienes quiera
    debía debia podría podria debería deberia había habia siendo hemos habéis
    nuestros nuestras vuestro vuestra ellos ellas nosotros vosotros usted ustedes
    te os nos me les consiste resultan resulta existen existe hace hacía hacia
    arte artista artistas obra obras mundo humano humanos humanidad vida tiempo
    años año siglo siglos historia cultura lenguaje lengua lenguas idioma palabra
    palabras texto textos ideas idea mente cerebro pensamiento conocimiento
    pregunta respuestas forma formas manera modos modo parte partes cosa cosas
    caso casos ejemplo ejemplos cambio cambios proceso procesos sistema sistemas
    modelo modelos datos información tecnologia tecnología maquina máquina
    máquinas herramientas herramienta trabajo trabajos desarrollo crecimiento
    futuro pasado presente real realidad verdad sentido significado valor valores
    poder fuerza razón razon razones efecto efectos causa causas resultado
    resultados problema problemas solución solucion pregunta tema temas punto
    puntos lugar lugares grupo grupos sociedad social persona personas gente
    nombre nombres número numero parte nuevo nueva viejos viejo grandes grande
    mejor peor mayor menor primero primer último ultimo mismo propio propia
    posible imposible importante diferentes diferente distintos distinto general
    común comun simple complejo difícil dificil fácil facil claro oscuro
    entender comprender aprender aprender enseñar mostrar decir dice dicen habla
    hablar escribir escribe lee leer pensar piensa creer cree crear crea crear
    producir generar cambiar cambiar seguir sigue lograr conseguir permitir
    parecer parece queda quedan tener tiene deben debe puede debemos podemos
    quieren quiere hacer hacen ver vemos da dan van vamos solo sólo además ademas
    incluso entonces mientras tanto sin embargo aunque quizá quiza tal vez
    """.split()
)

EN_HMM_WORDS = frozenset(
    """
    the a an and or but if of to in on at by for with from as is are was were be
    been being am has have had do does did will would can could shall should may
    might must this that these those it its he she they them their his her we us
    our you your i me my not no so than then there here when where which who whom
    whose what how why all any some more most other such only also very just into
    over after before between during about up down out off again once because
    while both each few many much own same too s t don now
    art artist artists work works world human humans life time year years century
    history culture language languages word words text texts idea ideas mind brain
    thought knowledge question questions form forms way ways part parts thing
    things case cases example examples change changes process processes system
    systems model models data information technology machine machines tool tools
    future past present real reality truth meaning value values power reason
    reasons effect effects cause causes result results problem problems solution
    theme point points place places group groups society social person people
    name names number new old great large big better worse greater less first last
    own possible impossible important different general common simple complex
    difficult easy clear dark understand learn teach show say says said speak talk
    write writes read think thinks believe believes create creates produce
    generate change follow continue achieve get allow seem seems remain stay
    become becomes need needs want wants see seen make makes made give gives
    take takes come comes go goes know knows find finds look looks feel feels
    still even though however perhaps maybe almost often always never sometimes
    """.split()
)

EN_ES_AMBIGUOUS = frozenset("no a me he son as has sea da la di mi si an".split())

ES_ACCENTS = frozenset("áéíóúüñ¿¡ÁÉÍÓÚÜÑ")
ES_SUFFIXES = ("ción", "ciones", "dad", "dades", "mente", "ísimo", "ísima", "aban", "iesen")
EN_SUFFIXES = ("ing", "tion", "sion", "ough")

# Clean word regex: intentionally excludes inverted punctuation ``¿¡`` so tokens
# never carry leading punctuation into glossaries, exposure counts or stems.
ES_WORD_PATTERN = r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+"
EN_WORD_PATTERN = r"[A-Za-z]+"

EN_FEEDS = (
    ("Quanta", "https://api.quantamagazine.org/feed/", "science and maths"),
    ("Aeon", "https://aeon.co/feed.rss", "essays and philosophy"),
    ("Nautilus", "https://nautil.us/feed/", "science essays"),
    ("The Conversation", "https://theconversation.com/us/articles.atom", "researchers writing plainly"),
    ("MIT News", "https://news.mit.edu/rss/topic/artificial-intelligence2", "technology and AI"),
    ("Literary Hub", "https://lithub.com/feed/", "culture and books"),
    ("BBC Future", "https://www.bbc.com/future/feed.rss", "science and society"),
)

# --------------------------------------------------------------------------- #
# Profiles & Pair
# --------------------------------------------------------------------------- #

EN_PROFILE = LanguageProfile(
    code="en",
    name="English",
    native_name="English",
    script="latin",
    word_spaced=True,
    cased=True,
    proficiency_scale="CEFR",
    word_pattern=EN_WORD_PATTERN,
    stopwords=_EN_STOPWORDS,
    function_words=_EN_STOPWORDS,
    abbreviations=("Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "vs", "etc", "e.g", "i.e"),
    tts_locales=("en-US", "en-GB", "en"),
    tts_rate=1.0,
)

ES_PROFILE = LanguageProfile(
    code="es",
    name="Spanish",
    native_name="Español",
    script="latin",
    word_spaced=True,
    cased=True,
    proficiency_scale="CEFR",
    word_pattern=ES_WORD_PATTERN,
    accent_chars=ES_ACCENTS,
    stopwords=_ES_STOPWORDS,
    function_words=_ES_FUNCTION_WORDS,
    derived_suffixes=_ES_DERIVED_SUFFIXES,
    sentence_punct=".!?…¿¡\"'“‘-–—(",
    abbreviations=("Sr", "Sra", "Dr", "Dra", "EE", "UU", "etc", "p. ej", "aprox", "núm"),
    tts_locales=("es-ES", "es-MX", "es-US", "es-AR", "es-CO", "es"),
    tts_rate=0.92,
    stemmer=spanish_stem,
)

EN_ES_SEGMENTER = SameScriptHMMSegmenter(
    base_code="en",
    target_code="es",
    base_words=EN_HMM_WORDS,
    target_words=ES_HMM_WORDS,
    ambiguous_words=EN_ES_AMBIGUOUS,
    target_accents=ES_ACCENTS,
    target_suffixes=ES_SUFFIXES,
    base_suffixes=EN_SUFFIXES,
    word_re=re.compile(ES_WORD_PATTERN),
)

EN_ES_SHELVES = (
    ("gentle", 0.00, 0.45, "Mostly English, Spanish in short phrases"),
    ("steady", 0.45, 0.62, "A real weave, roughly half Spanish"),
    ("dense", 0.62, 0.78, "Spanish carries whole sentences"),
    ("immersion", 0.78, 1.01, "Spanish-dominant"),
)

EN_ES_PAIR = LanguagePair(
    id="en-es",
    base=EN_PROFILE,
    target=ES_PROFILE,
    segmenter=EN_ES_SEGMENTER,
    shelves=EN_ES_SHELVES,
    accept_language="en-US,en;q=0.9,es;q=0.8",
    wikipedia_host="en.wikipedia.org",
    default_feeds=EN_FEEDS,
    min_probe_length=5,
    write_placeholder="Escribe aquí. No busques las palabras, usa las que tienes.",
)
