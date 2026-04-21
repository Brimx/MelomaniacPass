"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║              Motor de Matching Fuzzy y Validación                    ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: engine/match.py
Descripción: Implementa el sistema Hunter Recovery de matching fuzzy para
            validación de resultados de búsqueda entre plataformas.
            Utiliza algoritmos de similitud de cadenas (RapidFuzz) con
            umbrales adaptativos y estrategias de salvamento.

Estrategia de Diseño - Sistema Hunter Recovery:
    El motor de matching implementa un sistema de validación multi-nivel
    que balancea precisión y recall:
    
    1. NIVEL IDEAL (≥85%): Match de alta confianza, aceptado automáticamente
       - Score combinado ≥85%, o
       - Artista perfecto (100%) + título ≥60%
    
    2. NIVEL SALVAMENTO: Rescata matches con artista exacto pero título variable
       - Artista ≥99% + título ≥60%
       - Común en canciones con múltiples versiones/ediciones
    
    3. NIVEL REVISIÓN (<40%): Requiere intervención manual
       - Score combinado <40% sin salvamento posible
       - Previene falsos positivos con covers/remixes
    
    4. NIVEL LOG (70-84%): Válido pero registrado para análisis
       - Matches aceptables que merecen monitoreo
    
    El sistema usa RapidFuzz (token_sort_ratio) que es robusto ante:
    - Reordenamiento de palabras
    - Variaciones de capitalización
    - Espacios y puntuación inconsistentes

Dependencias Opcionales:
    - rapidfuzz: Librería de matching fuzzy de alto rendimiento
      Si no está disponible, retorna scores perfectos (100) como fallback

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from engine.normalizer import (
    _ASIAN_RE, _LETHAL_WORDS, _normalize_title, _strip_noise,
    clean_metadata,
    FUZZY_IDEAL, FUZZY_LOG_BAND_LOW, FUZZY_REVISION_THRESHOLD,
    FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT, ARTIST_EXACT_MIN, ARTIST_PERFECT,
)

# Intento de importación de RapidFuzz (dependencia opcional)
try:
    from rapidfuzz import fuzz as _fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False


def _fuzzy_score_pair(
    orig_title: str,
    orig_artist: str,
    found_title: str,
    found_artist: str,
) -> int:
    """
    Calcula score fuzzy combinado entre metadatos originales y encontrados.
    
    Utiliza RapidFuzz token_sort_ratio sobre núcleos limpios de título y artista.
    Este algoritmo es robusto ante reordenamiento de palabras y variaciones
    de formato, ideal para comparar metadatos entre plataformas.
    
    Args:
        orig_title: Título original de la canción.
        orig_artist: Artista original.
        found_title: Título encontrado en la búsqueda.
        found_artist: Artista encontrado en la búsqueda.
    
    Returns:
        Score de similitud 0-100. 100 = match perfecto, 0 = completamente diferente.
        Retorna 100 si RapidFuzz no está disponible (fallback optimista).
    
    Note:
        token_sort_ratio ordena alfabéticamente los tokens antes de comparar,
        lo que lo hace robusto ante variaciones como:
        - "The Beatles" vs "Beatles The"
        - "Bohemian Rhapsody Queen" vs "Queen Bohemian Rhapsody"
    """
    if not HAS_RAPIDFUZZ:
        return 100
    ct, ca = clean_metadata(orig_title, orig_artist)
    found_t, fa = clean_metadata(found_title, found_artist)
    return int(
        _fuzz.token_sort_ratio(
            f"{ct} {ca}".lower(),
            f"{found_t} {fa}".lower(),
        )
    )


def _fuzzy_scores_triple(
    orig_title: str,
    orig_artist: str,
    found_title: str,
    found_artist: str,
) -> tuple[int, int, int]:
    """
    Calcula scores fuzzy desglosados: combinado, solo título, solo artista.
    
    Proporciona granularidad para estrategias de salvamento y elasticidad.
    Permite detectar casos donde el artista es exacto pero el título varía
    (común en canciones con múltiples versiones/ediciones).
    
    Args:
        orig_title: Título original de la canción.
        orig_artist: Artista original.
        found_title: Título encontrado en la búsqueda.
        found_artist: Artista encontrado en la búsqueda.
    
    Returns:
        Tupla (combined, title_only, artist_only) con scores 0-100.
        - combined: Score del string concatenado "título artista"
        - title_only: Score comparando solo títulos
        - artist_only: Score comparando solo artistas
    
    Example:
        >>> _fuzzy_scores_triple(
        ...     "Bohemian Rhapsody", "Queen",
        ...     "Bohemian Rhapsody (Remastered)", "Queen"
        ... )
        (95, 88, 100)  # Artista perfecto, título muy similar
    
    Note:
        Esta función es el corazón del sistema Hunter Recovery. El desglose
        permite implementar lógica de salvamento para casos edge como:
        - Artista exacto + título con sufijo de versión
        - Título exacto + artista con colaboradores adicionales
    """
    if not HAS_RAPIDFUZZ:
        return 100, 100, 100
    ct, ca = clean_metadata(orig_title, orig_artist)
    found_t, fa = clean_metadata(found_title, found_artist)
    comb = int(_fuzz.token_sort_ratio(f"{ct} {ca}".lower(), f"{found_t} {fa}".lower()))
    tit  = int(_fuzz.token_sort_ratio(ct.lower(), found_t.lower()))
    art  = int(_fuzz.token_sort_ratio(ca.lower(), fa.lower()))
    return comb, tit, art


def _ideal_pass_hunter(comb: int, tit: int, art: int) -> bool:
    """
    Determina si un match cumple criterios de paso ideal del sistema Hunter.
    
    Implementa la lógica de aceptación automática con dos estrategias:
    1. Score combinado ≥85% (FUZZY_IDEAL)
    2. Artista perfecto/casi perfecto + título ≥60% (salvamento)
    
    Args:
        comb: Score combinado 0-100.
        tit: Score de título 0-100.
        art: Score de artista 0-100.
    
    Returns:
        True si el match es aceptable automáticamente, False en caso contrario.
    
    Note:
        La estrategia de salvamento (artista exacto + título ≥60%) es crítica
        para manejar casos donde el título varía por versiones/ediciones pero
        el artista es inequívoco. Ejemplos:
        - "Imagine" vs "Imagine (Remastered 2010)" - mismo artista
        - "Let It Be" vs "Let It Be - Remastered 2009" - mismo artista
    """
    if comb >= FUZZY_IDEAL:
        return True
    if art == ARTIST_PERFECT and tit >= FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT:
        return True
    if art >= ARTIST_EXACT_MIN and tit >= FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT:
        return True
    return False


def _fuzzy_flags_elastic(comb: int, tit: int, art: int) -> tuple[bool, bool]:
    """
    Calcula flags de confianza para un match: needs_review y low_confidence.
    
    Implementa la lógica de clasificación de matches en tres categorías:
    1. Alta confianza: needs_review=False, low_confidence=False
    2. Confianza media: needs_review=False, low_confidence=True (70-84%)
    3. Requiere revisión: needs_review=True (score <40% sin salvamento)
    
    Args:
        comb: Score combinado 0-100.
        tit: Score de título 0-100.
        art: Score de artista 0-100.
    
    Returns:
        Tupla (needs_review, low_confidence).
        - needs_review: True si requiere revisión manual
        - low_confidence: True si está en banda de confianza media (70-84%)
    
    Note:
        La lógica de salvamento previene que matches con artista exacto
        sean marcados para revisión incluso si el score combinado es bajo.
        Esto es intencional: un artista exacto es señal fuerte de que
        estamos en la canción correcta, incluso si el título varía.
    """
    salvaged = (art >= ARTIST_EXACT_MIN and tit >= FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT)
    needs_review = comb < FUZZY_REVISION_THRESHOLD and not salvaged
    ideal = _ideal_pass_hunter(comb, tit, art)
    low_conf = (
        ideal
        and not needs_review
        and (
            (FUZZY_LOG_BAND_LOW <= comb < FUZZY_IDEAL)
            or (art >= ARTIST_EXACT_MIN and FUZZY_LOG_BAND_LOW <= tit < FUZZY_IDEAL)
        )
    )
    return needs_review, low_conf


def _fuzzy_flags(score: int) -> tuple[bool, bool]:
    """Fallback monocanal (sin triple)."""
    return _fuzzy_flags_elastic(score, score, score)


def score_spotify_match(
    local_title: str,
    local_artist: str,
    local_duration_ms: int,
    local_is_explicit: bool,
    sp_title: str,
    sp_artist: str,
    sp_duration_ms: int,
    sp_is_explicit: bool,
) -> int:
    """
    Sistema de puntuación base 100 para evaluar un resultado de Spotify
    contra un track local. Reemplaza el desempate por popularity (eliminado
    por Spotify en marzo 2026 para apps en Development Mode).

    Distribución de pesos:
        60 pts — Fuzzy matching (40 título + 20 artista)
        30 pts — Delta de duración
        10 pts — Bonus de metadata (explicit flag)

    Args:
        local_title / local_artist: metadatos del track local (ya limpios).
        local_duration_ms: duración local en milisegundos.
        local_is_explicit: flag explicit del track local.
        sp_title / sp_artist: metadatos del resultado de Spotify.
        sp_duration_ms: duración del resultado en milisegundos.
        sp_is_explicit: flag explicit del resultado de Spotify.

    Returns:
        Score entero 0-100.  Valores negativos se clampean a 0.
    """
    # ── 60 pts: Fuzzy (40 título + 20 artista) ──────────────────────
    if HAS_RAPIDFUZZ:
        ct, ca = clean_metadata(local_title, local_artist)
        ft, fa = clean_metadata(sp_title, sp_artist)
        title_ratio  = _fuzz.token_sort_ratio(ct.lower(), ft.lower())
        artist_ratio = _fuzz.token_sort_ratio(ca.lower(), fa.lower())
    else:
        title_ratio  = SequenceMatcher(None, local_title.lower(), sp_title.lower()).ratio() * 100
        artist_ratio = SequenceMatcher(None, local_artist.lower(), sp_artist.lower()).ratio() * 100

    fuzzy_pts = (title_ratio / 100.0) * 40 + (artist_ratio / 100.0) * 20

    # ── 30 pts: Delta de duración ────────────────────────────────────
    delta_ms = abs(local_duration_ms - sp_duration_ms)
    if delta_ms <= 2000:
        duration_pts = 30
    elif delta_ms <= 5000:
        duration_pts = 15
    else:
        duration_pts = -20

    # ── 10 pts: Bonus de metadata (explicit) ─────────────────────────
    metadata_pts = 10 if (local_is_explicit == sp_is_explicit) else 0

    return max(0, int(fuzzy_pts + duration_pts + metadata_pts))


def _joji_trikeyword_query(title: str, artist: str) -> str:
    """Primeras 3 palabras clave del título + artista."""
    from engine.normalizer import clean_metadata as _cm
    ct, ca = _cm(title, artist)
    words = [w for w in ct.split() if w][:3]
    if not words:
        return ""
    return f"{' '.join(words)} {ca}".strip()


def _duration_to_seconds(dur: str) -> Optional[int]:
    """'4:19' → 259 | '1:04:19' → 3859 | '' → None"""
    try:
        parts = [int(p) for p in str(dur).split(':')]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, AttributeError):
        pass
    return None


def validar_match(
    local_title: str,
    local_artist: str,
    remote_result: dict,
    _local_duration_s: Optional[int] = None,
) -> bool:
    """
    Motor de validación multi-capa para resultados de ytmusicapi 1.11.5.

    L0 — Bypass asiático  : scripts CJK/Hangul → match inmediato
    L1 — Prueba de ácido  : substring + solapamiento de artista → MATCH
    L2 — Filtro letal     : cover / karaoke / tribute → REJECT
    L3 — Fuzzy safety net : SequenceMatcher ≥ 0.65 → MATCH / REJECT
    """
    raw_artists: list = remote_result.get('artists') or []
    r_artists: list[str] = [
        unicodedata.normalize("NFKC", str(a.get('name', ''))).lower()
        for a in raw_artists
        if isinstance(a, dict) and a.get('name')
    ]
    r_artist_str: str = ' '.join(r_artists)

    r_title_raw: str = remote_result.get('title', '')
    r_title: str = _strip_noise(unicodedata.normalize("NFKC", str(r_title_raw))).lower()

    l_title:  str = _normalize_title(unicodedata.normalize("NFKC", str(local_title)))
    l_artist: str = _normalize_title(unicodedata.normalize("NFKC", str(local_artist)))

    # L0: Bypass asiático
    if _ASIAN_RE.search(l_title) or _ASIAN_RE.search(r_title):
        return True

    # L1: Prueba de ácido
    title_match: bool = (l_title in r_title) or (r_title in l_title)
    artist_match: bool = (
        l_artist in r_artist_str
        or r_artist_str in l_artist
        or any(word in r_artist_str for word in l_artist.split() if len(word) > 2)
    )
    if title_match and artist_match:
        return True

    # L2: Filtro letal
    if any(word in r_title for word in _LETHAL_WORDS):
        return False

    # L3: Fuzzy safety net
    l_full = unicodedata.normalize("NFKC", f"{l_title} {l_artist}")
    r_full = unicodedata.normalize("NFKC", f"{r_title} {r_artist_str}")
    return SequenceMatcher(None, l_full, r_full).ratio() >= 0.65


def _yt_select_best(
    name: str,
    artist: str,
    results: list[dict],
    local_duration_s: Optional[int],
) -> Optional[str]:
    """
    Evalúa los primeros 3 resultados de ytmusicapi.search() y elige el mejor.
    Tie-breaker A: preferir resultType == 'song'.
    Tie-breaker B: duración más cercana al original (±5s).
    """
    DURATION_MARGIN_S = 5

    candidates: list[dict] = []
    for result in results[:3]:
        if not result.get('videoId'):
            continue
        if validar_match(name, artist, result, local_duration_s):
            candidates.append(result)

    if not candidates:
        return None

    songs = [c for c in candidates if c.get('resultType') == 'song']
    pool  = songs if songs else candidates

    if local_duration_s is not None and len(pool) > 1:
        def _delta(c: dict) -> float:
            remote_s = (
                c.get('duration_seconds')
                or _duration_to_seconds(c.get('duration', ''))
            )
            return abs(remote_s - local_duration_s) if remote_s is not None else float('inf')

        within_margin = [c for c in pool if _delta(c) <= DURATION_MARGIN_S]
        if within_margin:
            pool = within_margin

    return pool[0].get('videoId')
