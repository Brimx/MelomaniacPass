"""
╔══════════════════════════════════════════════════════════════════════╗
║       MelomaniacPass v4.5 — Recovery & Universal Auth             ║
║                                                                      ║
║  Architecture : BLoC-inspired (AppState ◄─ Service ◄─ UI)           ║
║  Design       : Solid dark surfaces · IBM Plex Sans · OLED          ║
║  Engine       : Hunter Recovery · Universal Auth · Post-mortem        ║
║  Lifecycle    : Hard exit · Session probes · Semáforo real            ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §1  IMPORTS & ENVIRONMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import asyncio
import csv
import io
import os
import random
import re
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote
import traceback
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum, auto
from typing import Callable, Optional

import flet as ft
from dotenv import load_dotenv
from auth_manager import AuthManager, BROWSER_JSON

# ── Optional heavy deps (graceful degradation) ────────────────────────
try:
    import spotipy
    from spotipy.exceptions import SpotifyException
    HAS_SPOTIFY = True
except ImportError:
    HAS_SPOTIFY = False
    SpotifyException = None  # type: ignore[misc,assignment]

try:
    from ytmusicapi import YTMusic
    HAS_YTMUSIC = True
except ImportError:
    HAS_YTMUSIC = False

try:
    from rapidfuzz import fuzz as _fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

import requests  # always needed for Apple Music + Spotify shadow auth

load_dotenv()


def _is_spotify_rate_limited(exc: BaseException) -> bool:
    if SpotifyException is None:
        return False
    return isinstance(exc, SpotifyException) and getattr(exc, "http_status", None) == 429


def _failure_reason_from_exc(exc: BaseException) -> str:
    """Post-mortem: motivo legible para fallos de red/API (caso Joji, etc.)."""
    if HAS_SPOTIFY and SpotifyException is not None and isinstance(
        exc, SpotifyException,
    ):
        hs = getattr(exc, "http_status", None)
        if hs is not None:
            return f"Spotify HTTP {hs}"
    msg = str(exc)
    return msg[:300] + ("…" if len(msg) > 300 else "")


def _is_ytm_unauthorized(exc: BaseException) -> bool:
    """401 / Unauthorized desde ytmusicapi o requests subyacente."""
    s = str(exc).lower()
    if "401" in str(exc) or "status code: 401" in s or "unauthorized" in s:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 401:
        return True
    return False


_YTM_401_MSG = (
    "[ERROR] YouTube Music: la sesión de browser.json ha expirado o es inválida (401). "
    "Renueva Cookie + Authorization (SAPISIDHASH) desde el navegador o el asistente de configuración."
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §2  DATA MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Track:
    id: str
    name: str
    artist: str
    album: str
    duration: str
    img_url: str
    platform: str
    selected: bool = True
    transfer_status: str = "pending"  # pending|searching|found|not_found|transferred|error|revision_necesaria
    failure_reason: str = ""  # post-mortem: Zero Results, HTTP …, excepción


@dataclass
class SearchResult:
    """Resultado de búsqueda universal V4.0 Hunter (id + revisión solo si <40%)."""
    track_id: Optional[str] = None
    needs_review: bool = False  # True → revisión (match <40% tras Hunter)
    low_confidence: bool = False  # True → 70–84%: válido; solo log interno


class LoadState(Enum):
    IDLE           = auto()
    LOADING_META   = auto()
    LOADING_TRACKS = auto()
    READY          = auto()
    ERROR          = auto()


class TransferState(Enum):
    IDLE    = auto()
    RUNNING = auto()
    DONE    = auto()
    ERROR   = auto()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §3  CIRCUIT BREAKER  (Rate-Limit Protection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RateLimitError(Exception):
    def __init__(self, platform: str, retry_after: int):
        self.platform    = platform
        self.retry_after = retry_after
        super().__init__(f"Rate-limited by {platform}. Retry in {retry_after}s.")


class CircuitBreaker:
    """
    If a 429 is detected, the breaker trips and notifies subscribers.
    The UI disables all network buttons and shows a live countdown.
    Auto-resets after `cooldown` seconds.
    """
    def __init__(self, platform: str, default_cooldown: int = 60):
        self.platform         = platform
        self.default_cooldown = default_cooldown
        self.is_open: bool    = False
        self._until: float    = 0.0
        self._callbacks: list[Callable[[bool, int], None]] = []

    def subscribe(self, cb: Callable[[bool, int], None]) -> None:
        self._callbacks.append(cb)

    def trip(self, retry_after: Optional[int] = None) -> None:
        wait         = retry_after or self.default_cooldown
        self.is_open = True
        self._until  = time.monotonic() + wait
        self._notify(True, wait)
        asyncio.create_task(self._auto_reset(wait))

    def check_or_raise(self) -> None:
        if self.is_open:
            raise RateLimitError(self.platform, int(self.remaining))

    @property
    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic())

    def _notify(self, is_open: bool, remaining: int) -> None:
        for cb in self._callbacks:
            try:
                cb(is_open, remaining)
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # Callbacks de UI pueden lanzar cualquier excepción

    async def _auto_reset(self, wait: float) -> None:
        await asyncio.sleep(wait)
        self.is_open = False
        self._notify(False, 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §4  SPOTIFY SHADOW AUTH MANAGER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _default_spotify_windows_ua() -> str:
    """User-Agent coherente con cabeceras de navegador Windows (Chrome/Edge)."""
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    )


def spotify_navigation_headers() -> dict[str, str]:
    """
    Cabeceras de navegación real desde .env (SPOTIFY_ACCEPT, SPOTIFY_ACCEPT_LANG,
    SPOTIFY_ORIGIN, SPOTIFY_USER_AGENT). Accept-Language admite SPOTIFY_ACCEPT_LANGUAGE
    como respaldo.
    """
    accept = os.getenv("SPOTIFY_ACCEPT", "").strip() or (
        "application/json, text/plain, */*"
    )
    accept_lang = (
        os.getenv("SPOTIFY_ACCEPT_LANG", "").strip()
        or os.getenv("SPOTIFY_ACCEPT_LANGUAGE", "").strip()
        or "es-419,es;q=0.9,en;q=0.8"
    )
    origin = os.getenv("SPOTIFY_ORIGIN", "").strip() or "https://open.spotify.com"
    referer = origin.rstrip("/") + "/" if origin else "https://open.spotify.com/"
    ua = os.getenv("SPOTIFY_USER_AGENT", "").strip() or _default_spotify_windows_ua()
    return {
        "Accept":           accept,
        "Accept-Language":  accept_lang,
        "Origin":           origin,
        "Referer":          referer,
        "User-Agent":       ua,
    }


class SpotifyShadowAuthManager:
    """Duck-typed auth_manager for Spotipy using the Spotify Web Player shadow API."""

    def __init__(self, cache_handler):
        self.cache_handler = cache_handler

    def get_access_token(self, as_dict: bool = False):
        manual = os.getenv("SPOTIFY_MANUAL_BEARER", "").strip()
        if manual:
            token = manual.replace("Bearer ", "")
            return {"access_token": token} if as_dict else token

        info = self.cache_handler.get_cached_token()
        if info and time.time() < (info.get("expires_at", 0) - 300):
            tok = info["access_token"]
            return {"access_token": tok} if as_dict else tok

        sp_dc = os.getenv("SPOTIFY_SP_DC", "").strip()
        if not sp_dc:
            raise RuntimeError("SPOTIFY_SP_DC missing from .env")

        headers = dict(spotify_navigation_headers())
        headers["Cookie"] = f"sp_dc={sp_dc}"
        headers["App-Platform"] = "WebPlayer"
        for env_key, header in [
            ("SPOTIFY_APP_VERSION", "Spotify-App-Version"),
            ("SPOTIFY_CLIENT_TOKEN", "Client-Token"),
        ]:
            val = os.getenv(env_key)
            if val:
                headers[header] = val

        resp = requests.get(
            "https://open.spotify.com/get_access_token"
            "?reason=transport&productType=web_player",
            headers=headers, timeout=10,
        )
        if resp.status_code == 403:
            raise RuntimeError("Spotify 403: refresh sp_dc and Client-Token.")
        resp.raise_for_status()
        data      = resp.json()
        exp_ms    = data.get("accessTokenExpirationTimestampMs")
        expires_at = int(exp_ms / 1000) if exp_ms else int(time.time()) + 3300
        token     = data.get("accessToken")
        if not token:
            raise RuntimeError("Shadow API returned no accessToken.")

        self.cache_handler.save_token_to_cache(
            {"access_token": token, "expires_at": expires_at}
        )
        return {"access_token": token} if as_dict else token


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §5  MATCH ENGINE — Module-level helpers
#     (Shared by MusicApiService and AppState.transfer_playlist)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Compiled regex (zero runtime cost) ───────────────────────────────

# Strips well-known noise tokens that appear inside parentheses/brackets in
# YouTube Music titles. Keeps the semantic nucleus intact.
#   "Clint Eastwood (Remastered 2001)"  →  "Clint Eastwood"
#   "Bohemian Rhapsody (Official Video)" →  "Bohemian Rhapsody"
_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:'
    r'remaster(?:ed)?(?:\s+\d{4})?'
    r'|official\s+(?:video|audio|lyric\s+video|music\s+video|visualizer)'
    r'|explicit'
    r'|single'
    r'|hd|hq|4k'
    r'|stereo|mono'
    r'|radio\s+edit'
    r'|bonus\s+track'
    r'|live(?:\s+(?:at|from|version)\b[^)\]]*)?'
    r')\s*[\)\]]',
    re.IGNORECASE,
)

# Removes full parenthetical groups and feat./ft. suffixes from local titles.
_CLEAN_RE = re.compile(
    r'\s*[\(\[].*?[\)\]]|\s+feat\.?\s.*|\s+ft\.?\s.*',
    re.IGNORECASE,
)

# CJK / Hangul detector for the Asian bypass layer.
_ASIAN_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\u3130-\u318f]')

# Words that unconditionally reject a result regardless of score.
_LETHAL_WORDS: frozenset[str] = frozenset({'cover', 'karaoke', 'tribute', 'parody'})


def _normalize_title(text: str) -> str:
    """
    Canonical normalisation for LOCAL titles (what the user has in their library).
    NFC → strip parentheticals + feat → strip trailing dash → lowercase → collapse spaces.
    """
    text    = unicodedata.normalize('NFC', str(text))
    text    = _CLEAN_RE.sub('', text)
    text    = re.sub(r'\s*[-–]\s*$', '', text)
    return ' '.join(text.split()).strip().lower()


def _strip_noise(text: str) -> str:
    """
    Removes noise-only suffixes from REMOTE titles (YouTube Music results).
    Preserves the original if the regex would wipe everything.
    """
    cleaned = _NOISE_RE.sub('', text).strip()
    return cleaned if cleaned else text.strip()


# V4.0 — Protocolo "The Purge": paréntesis, corcheos y ruido explícito
_PURGE_BRACKETS_RE = re.compile(r'\([^)]*\)|\[[^\]]*\]')
_PURGE_NOISE_WORDS = re.compile(
    r'\b(?:official\s+video|remaster(?:ed)?|live|deluxe|video\s+edit|feat\.?|ft\.?)\b',
    re.IGNORECASE,
)
# Hunter / Anti-Lazy — umbrales RapidFuzz (token_sort sobre núcleos limpios)
FUZZY_IDEAL = 85
FUZZY_LOG_BAND_LOW = 70
FUZZY_REVISION_THRESHOLD = 40
# Artista casi exacto (≥99): título puede validar desde 60% (Recovery / Joji)
FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT = 60
ARTIST_EXACT_MIN = 99  # token_sort_ratio artista «casi exacto»
ARTIST_PERFECT = 100   # coincidencia de artista al 100% (título puede bajar a 60%)


def clean_metadata(title: str, artist: str) -> tuple[str, str]:
    """
    Paso previo obligatorio antes de cualquier búsqueda en APIs externas.
    Extrae el núcleo semántico (sin paréntesis/corcheos ni ruido catalogado).
    """
    t = unicodedata.normalize('NFC', str(title).strip())
    a = unicodedata.normalize('NFC', str(artist).strip())
    t = _PURGE_BRACKETS_RE.sub('', t)
    a = _PURGE_BRACKETS_RE.sub('', a)
    t = _PURGE_NOISE_WORDS.sub('', t)
    a = _PURGE_NOISE_WORDS.sub('', a)
    t = _strip_noise(t) if t else t
    a = _strip_noise(a) if a else a
    t = ' '.join(t.split()).strip()
    a = ' '.join(a.split()).strip()
    if not t:
        t = str(title).strip()
    if not a:
        a = str(artist).strip()
    return t, a


def build_search_query(title: str, artist: str) -> str:
    """
    ══════════════════════════════════════════════════════════════════
    REGLA 2 — Normalización de Búsqueda (Prioridad de Obra)
    ══════════════════════════════════════════════════════════════════
    Reconstruye la cadena de búsqueda siguiendo SIEMPRE el orden canónico:
        [Nombre de la Canción] + [Artista]

    Sin importar cómo lleguen los datos en origen (archivo, portapapeles
    o API), la query enviada a Spotify / Apple / YTM es SIEMPRE título
    primero. Los algoritmos de búsqueda priorizan las primeras palabras
    clave: colocar el título garantiza el match con el track específico
    en lugar de perderse en la discografía general del artista.

    Uso: build_search_query(track.name, track.artist)
         → "Clint Eastwood Gorillaz"   ✓
         → "Gorillaz Clint Eastwood"   ✗  (artist-first, PROHIBIDO)
    """
    t = title.strip() if title else ""
    a = artist.strip() if artist else ""
    if t and a:
        return f"{t} {a}"
    return t or a


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §2b  UNIVERSAL LOCAL PLAYLIST PARSER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LOCAL_TRACK_NUM_RE  = re.compile(r'^\d{1,3}[\s.\-_]+')
_LOCAL_FILE_EXT_RE   = re.compile(r'\.(mp3|flac|aac|ogg|wav|m4a|wma|opus|aiff?)$', re.IGNORECASE)
_LOCAL_BRACKETS_RE   = re.compile(r'\s*[\(\[][^\)\]]{1,60}[\)\]]')
_LOCAL_EXTINF_RE     = re.compile(r'^#EXTINF\s*:\s*-?\d+\s*,\s*', re.IGNORECASE)
_LOCAL_SEPARATORS    = (' – ', ' - ', ' — ', ' _ ')


def _parse_local_line(raw: str) -> Optional[tuple[str, str]]:
    """Return (artist, title) from a single raw text line, or None if blank."""
    s = unicodedata.normalize('NFC', raw.strip())
    s = _LOCAL_EXTINF_RE.sub('', s)
    s = _LOCAL_FILE_EXT_RE.sub('', s)
    s = _LOCAL_TRACK_NUM_RE.sub('', s)
    s = _LOCAL_BRACKETS_RE.sub('', s)
    s = ' '.join(s.split()).strip()
    if not s:
        return None
    for sep in _LOCAL_SEPARATORS:
        if sep in s:
            parts = s.split(sep, 1)
            title  = parts[0].strip()
            artist = parts[1].strip()
            if title:
                return (artist, title)
    return ("", s)


def _parse_xspf(text: str) -> list[tuple[str, str]]:
    try:
        root = ET.fromstring(text)
        ns   = {'s': 'http://xspf.org/ns/0/'}
        pairs: list[tuple[str, str]] = []
        for track in root.findall('.//s:track', ns):
            title  = (track.findtext('s:title',  default='', namespaces=ns) or '').strip()
            artist = (track.findtext('s:creator', default='', namespaces=ns) or '').strip()
            if title:
                pairs.append((artist, title))
        return pairs
    except ET.ParseError:
        return []


def _parse_wpl(text: str) -> list[tuple[str, str]]:
    try:
        root   = ET.fromstring(text)
        pairs: list[tuple[str, str]] = []
        for media in root.findall('.//media'):
            src  = media.get('src', '')
            base = os.path.basename(src.replace('\\', '/'))
            pair = _parse_local_line(base)
            if pair:
                pairs.append(pair)
        return pairs
    except ET.ParseError:
        return []


def parse_local_playlist(text: str, filename: str = "") -> list[tuple[str, str]]:
    """
    Parse raw text from supported file formats into (artist, title) pairs.
    Supported: .txt .csv .m3u .m3u8 .pls .wpl .xspf .xml and bare text.
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""

    if ext in ('.xspf', '.xml'):
        pairs = _parse_xspf(text)
        if pairs:
            return pairs

    if ext == '.wpl':
        pairs = _parse_wpl(text)
        if pairs:
            return pairs

    lines = text.splitlines()

    # PLS: TitleN=...
    if ext == '.pls' or any(l.strip().lower() == '[playlist]' for l in lines[:5]):
        pairs: list[tuple[str, str]] = []
        for line in lines:
            m = re.match(r'^Title\d+=(.+)$', line.strip(), re.IGNORECASE)
            if m:
                pair = _parse_local_line(m.group(1))
                if pair:
                    pairs.append(pair)
        if pairs:
            return pairs

    # CSV (first try header-sniff, then naive split)
    if ext == '.csv':
        pairs = _parse_csv(text)
        if pairs:
            return pairs

    # M3U / M3U8 / generic text
    pairs = []
    pending = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^#EXTINF\s*:\s*-?\d+\s*,\s*(.+)$', line, re.IGNORECASE)
        if m:
            pending = m.group(1)
            continue
        if line.startswith('#'):
            continue
        raw = pending if pending else os.path.basename(line.replace('\\', '/'))
        pending = ""
        pair = _parse_local_line(raw)
        if pair:
            pairs.append(pair)
    return pairs


def _parse_csv(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    reader = csv.reader(io.StringIO(text))
    rows   = list(reader)
    if not rows:
        return pairs
    # Detect header row
    start = 1 if rows and any(
        kw in (rows[0][0].lower() if rows[0] else '')
        for kw in ('title', 'name', 'track', 'song', 'artis')
    ) else 0
    cols = [c.strip().lower() for c in rows[0]] if rows else []
    ti   = next((i for i, c in enumerate(cols) if 'title' in c or 'name' in c or 'track' in c or 'song' in c), None)
    ai   = next((i for i, c in enumerate(cols) if 'artist' in c or 'author' in c), None)
    for row in rows[start:]:
        if not row:
            continue
        if ti is not None and ai is not None and len(row) > max(ti, ai):
            title  = row[ti].strip().strip('"')
            artist = row[ai].strip().strip('"')
        elif len(row) >= 2:
            title  = row[0].strip().strip('"')
            artist = row[1].strip().strip('"')
        elif len(row) == 1:
            p = _parse_local_line(row[0])
            if p:
                pairs.append(p)
            continue
        else:
            continue
        if title:
            pairs.append((artist, title))
    return pairs


def build_local_tracks(pairs: list[tuple[str, str]]) -> list:
    """Convert (artist, title) pairs into Track objects with platform='local'."""
    tracks = []
    for artist, title in pairs:
        if not title.strip():
            continue
        tracks.append(Track(
            id=f"local_{uuid.uuid4().hex[:12]}",
            name=title.strip(),
            artist=artist.strip(),
            album="",
            duration="",
            img_url="",
            platform="local",
            selected=True,
            transfer_status="local_pending",
            failure_reason="",
        ))
    return tracks


def _fuzzy_score_pair(
    orig_title: str,
    orig_artist: str,
    found_title: str,
    found_artist: str,
) -> int:
    """RapidFuzz token_sort_ratio sobre núcleos limpios (0–100)."""
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
    """(combined, title_only, artist_only) para elasticidad Joji / Fantasy."""
    if not HAS_RAPIDFUZZ:
        return 100, 100, 100
    ct, ca = clean_metadata(orig_title, orig_artist)
    found_t, fa = clean_metadata(found_title, found_artist)
    comb = int(
        _fuzz.token_sort_ratio(
            f"{ct} {ca}".lower(),
            f"{found_t} {fa}".lower(),
        )
    )
    tit = int(_fuzz.token_sort_ratio(ct.lower(), found_t.lower()))
    art = int(_fuzz.token_sort_ratio(ca.lower(), fa.lower()))
    return comb, tit, art


def _ideal_pass_hunter(comb: int, tit: int, art: int) -> bool:
    """Paso «ideal» Hunter: combinado ≥85% o artista fuerte + título ≥60%."""
    if comb >= FUZZY_IDEAL:
        return True
    if art == ARTIST_PERFECT and tit >= FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT:
        return True
    if art >= ARTIST_EXACT_MIN and tit >= FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT:
        return True
    return False


def _fuzzy_flags_elastic(comb: int, tit: int, art: int) -> tuple[bool, bool]:
    """
    (needs_review, low_confidence). Revisión si comb <40% salvo rescate por artista + título ≥60%.
    """
    salvaged = (
        art >= ARTIST_EXACT_MIN
        and tit >= FUZZY_TITLE_IDEAL_WHEN_ARTIST_EXACT
    )
    needs_review = comb < FUZZY_REVISION_THRESHOLD and not salvaged
    ideal = _ideal_pass_hunter(comb, tit, art)
    low_conf = (
        ideal
        and not needs_review
        and (
            (FUZZY_LOG_BAND_LOW <= comb < FUZZY_IDEAL)
            or (
                art >= ARTIST_EXACT_MIN
                and FUZZY_LOG_BAND_LOW <= tit < FUZZY_IDEAL
            )
        )
    )
    return needs_review, low_conf


def _fuzzy_flags(score: int) -> tuple[bool, bool]:
    """Fallback monocanal (sin triple): delega en scores artificiales."""
    return _fuzzy_flags_elastic(score, score, score)


def _joji_trikeyword_query(title: str, artist: str) -> str:
    """Primeras 3 palabras clave del título + artista (títulos largos / condicionales)."""
    ct, ca = clean_metadata(title, artist)
    words = [w for w in ct.split() if w][:3]
    if not words:
        return ""
    return f"{' '.join(words)} {ca}".strip()


def _duration_to_seconds(dur: str) -> Optional[int]:
    """
    "4:19"    → 259
    "1:04:19" → 3859
    ""        → None
    """
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
    local_title:  str,
    local_artist: str,
    remote_result: dict,
    _local_duration_s: Optional[int] = None,
) -> bool:
    """
    Motor de validación multi-capa para resultados de ytmusicapi 1.11.5.

    ESQUEMA DE RESPUESTA (crítico):
        remote_result = {
            "title":           str,
            "artists":         list[{"name": str, "id": str}],  # SIEMPRE lista de dicts
            "resultType":      "song" | "video" | "album" | ...,
            "duration":        "4:19",
            "duration_seconds": 259,
            "videoId":         str,
            ...
        }

    CAPAS:
        L0 — Bypass asiático  : scripts CJK/Hangul → match inmediato
        L1 — Prueba de ácido  : substring + solapamiento de artista → MATCH
        L2 — Filtro letal     : cover / karaoke / tribute → REJECT
        L3 — Fuzzy safety net : SequenceMatcher ≥ 0.65 → MATCH / REJECT

    RAÍZ DEL BUG "Clint Eastwood":
        El título remoto era "Clint Eastwood (Remastered 2001)".
        El viejo _fuzzy_best comparaba el string SIN limpiar → 73.5%  < umbral 85%.
        Ahora: _strip_noise → "Clint Eastwood", substring check → True ✓
    """
    # ── Extracción segura del resultado remoto ────────────────────────────────
    # artists en ytmusicapi es SIEMPRE list[dict]; nunca asumir que es string.
    raw_artists: list = remote_result.get('artists') or []
    r_artists: list[str] = [
        unicodedata.normalize("NFKC", str(a.get('name', ''))).lower()
        for a in raw_artists
        if isinstance(a, dict) and a.get('name')
    ]
    r_artist_str: str = ' '.join(r_artists)

    # Título remoto: NFKC → quitar ruido → lowercase
    r_title_raw: str = remote_result.get('title', '')
    r_title: str = _strip_noise(
        unicodedata.normalize("NFKC", str(r_title_raw))
    ).lower()

    # ── Normalización local (NFKC: acentos / compatibilidad Unicode) ───────────
    l_title:  str = _normalize_title(
        unicodedata.normalize("NFKC", str(local_title))
    )
    l_artist: str = _normalize_title(
        unicodedata.normalize("NFKC", str(local_artist))
    )

    # ── Capa 0: Bypass asiático ───────────────────────────────────────────────
    # Scripts CJK y Hangul no admiten comparación por substring en latín.
    if _ASIAN_RE.search(l_title) or _ASIAN_RE.search(r_title):
        return True

    # ── Capa 1: Prueba de ácido — Inclusión cruzada + artista ─────────────────
    # Un título es válido si el núcleo local CONTIENE o ES CONTENIDO en el remoto.
    title_match: bool = (l_title in r_title) or (r_title in l_title)

    # Artist match flexible: maneja artistas compuestos ("Gorillaz feat. Del")
    # y listas múltiples ("Post Malone, Swae Lee")
    artist_match: bool = (
        l_artist in r_artist_str
        or r_artist_str in l_artist
        or any(word in r_artist_str for word in l_artist.split() if len(word) > 2)
    )

    if title_match and artist_match:
        return True

    # ── Capa 2: Filtro letal ──────────────────────────────────────────────────
    # Si el título remoto contiene estos tokens, la pista es una versión
    # derivada y se rechaza aunque el fuzzy la apruebe.
    if any(word in r_title for word in _LETHAL_WORDS):
        return False

    # ── Capa 3: Fuzzy safety net (NFKC en cadena completa) ─────────────────────
    l_full: str = unicodedata.normalize(
        "NFKC", f"{l_title} {l_artist}",
    )
    r_full: str = unicodedata.normalize(
        "NFKC", f"{r_title} {r_artist_str}",
    )
    similarity = SequenceMatcher(None, l_full, r_full).ratio()
    return similarity >= 0.65


def _yt_select_best(
    name:             str,
    artist:           str,
    results:          list[dict],
    local_duration_s: Optional[int],
) -> Optional[str]:
    """
    Evalúa los primeros 3 resultados de ytmusicapi.search(), aplica
    validar_match a cada uno y elige el mejor mediante tie-breaker.

    PIPELINE:
        1. Filtrar resultados inválidos (sin videoId).
        2. Aplicar validar_match (capas L0→L3) a cada candidato.
        3. Tie-breaker A: preferir resultType == 'song'.
        4. Tie-breaker B: si se conoce la duración local, preferir
           el resultado cuya duración se aleje ≤ 5 segundos.
        5. Devolver el videoId del ganador, o None si ninguno pasó.

    Regla 3 (búsqueda multicapa): si el resultado #1 falla validar_match,
    se evalúan obligatoriamente los resultados #2 y #3 antes de marcar error.
    """
    DURATION_MARGIN_S = 5

    # ── Paso 1-2: Filtrar y validar ───────────────────────────────────────────
    candidates: list[dict] = []
    for result in results[:3]:          # evalúa hasta 3, no solo el primero
        if not result.get('videoId'):
            continue
        if validar_match(name, artist, result, local_duration_s):
            candidates.append(result)

    if not candidates:
        return None

    # ── Tie-breaker A: preferir resultType == 'song' ──────────────────────────
    songs = [c for c in candidates if c.get('resultType') == 'song']
    pool  = songs if songs else candidates

    # ── Tie-breaker B: duración más cercana al original ───────────────────────
    if local_duration_s is not None and len(pool) > 1:
        def _delta(c: dict) -> float:
            remote_s = (
                c.get('duration_seconds')                  # ytmusicapi provee esto
                or _duration_to_seconds(c.get('duration', ''))
            )
            return abs(remote_s - local_duration_s) if remote_s is not None else float('inf')

        within_margin = [c for c in pool if _delta(c) <= DURATION_MARGIN_S]
        if within_margin:
            pool = within_margin

    return pool[0].get('videoId')


# ── Transfer / lazy-scan / Spotify API throttling ─────────────────────
NETWORK_CONCURRENCY = 5
SEARCH_INTER_REQUEST_DELAY_S = 0.1
RATE_LIMIT_BACKOFF_STEPS = 10
# Semáforo global: todas las búsquedas y peticiones externas comparten el mismo límite
GLOBAL_API_SEMAPHORE = asyncio.Semaphore(NETWORK_CONCURRENCY)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §6  MUSIC API SERVICE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MusicApiService:
    """
    Unified async façade over Spotify, YouTube Music and Apple Music.

    v4.5: Hunter Recovery · semáforo real · post-mortem · cierre seguro de sesiones HTTP
    """

    def __init__(self, circuit_breakers: dict[str, "CircuitBreaker"]):
        self._cb  = circuit_breakers
        self._sp  = None
        self._ytm = None
        self._am_headers:    dict = {}
        self._am_storefront: str  = "us"
        self._search_cache: dict[str, SearchResult] = {}
        self._shutdown_cleaned: bool = False
        self.youtube_auth_error: str = ""  # último error YTM (p. ej. 401) para diagnóstico
        # Apple Music / Spotify shadow: sesión HTTP compartida (Keep-Alive)
        # User-Agent: SPOTIFY_USER_AGENT en .env
        self._http_session = requests.Session()
        ua = os.getenv("SPOTIFY_USER_AGENT", "").strip() or _default_spotify_windows_ua()
        self._http_session.headers.update({"User-Agent": ua})
        # YouTube Music: sesión dedicada (no mezclar con cabeceras de Apple Music)
        self._yt_http_session = requests.Session()

    def _cleanup_sessions(self) -> None:
        if getattr(self, "_shutdown_cleaned", False):
            return
        self._shutdown_cleaned = True
        try:
            self._http_session.close()
        except OSError:
            pass
        try:
            self._yt_http_session.close()
        except OSError:
            pass
        try:
            if self._sp and hasattr(self._sp, '_session'):
                self._sp._session.close()
        except OSError:
            pass
        self._sp = None
        self._ytm = None
        self._am_headers = {}

    # ── Authentication ─────────────────────────────────────────────────

    async def init_spotify(self) -> bool:
        return await asyncio.to_thread(self._sync_init_spotify)

    def _sync_init_spotify(self) -> bool:
        if not HAS_SPOTIFY:
            return False
        try:
            am    = getattr(self, "auth_manager", None)
            token = am.get_spotify_web_token() if am else None
            if not token:
                return False
            self._sp = spotipy.Spotify(auth=token)
            self._sp.current_user()
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # spotipy y requests pueden lanzar distintas excepciones en el login
            print(f"[Spotify] init failed: {exc}")
            return False

    def _safe_sp_call(self, method_name: str, *args, **kwargs):
        try:
            return getattr(self._sp, method_name)(*args, **kwargs)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Spotipy puede lanzar SpotifyException o subclases de requests.RequestException
            if (
                HAS_SPOTIFY
                and SpotifyException is not None
                and isinstance(exc, SpotifyException)
                and getattr(exc, "http_status", None) == 401
            ):
                am = getattr(self, "auth_manager", None)
                new_token = am.get_spotify_web_token() if am else None
                if new_token:
                    self._sp = spotipy.Spotify(auth=new_token)
                    return getattr(self._sp, method_name)(*args, **kwargs)
            raise

    async def init_youtube(self) -> bool:
        return await asyncio.to_thread(self._sync_init_youtube)

    def _sync_init_youtube(self) -> bool:
        if not HAS_YTMUSIC:
            return False
        if not BROWSER_JSON.exists():
            print("[YouTube Music] No se encontró browser.json")
            self.youtube_auth_error = "missing browser.json"
            return False
        self.youtube_auth_error = ""
        try:
            self._ytm = YTMusic(
                auth=str(BROWSER_JSON),
                requests_session=self._yt_http_session,
            )
            self._ytm.get_library_playlists(limit=1)
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # ytmusicapi no define una jerarquía de excepciones pública estable
            self.youtube_auth_error = str(exc)
            if _is_ytm_unauthorized(exc):
                print(_YTM_401_MSG)
            else:
                print(f"[YouTube Music] init failed: {exc}")
            self._ytm = None
            return False

    async def init_apple(self) -> bool:
        return await asyncio.to_thread(self._sync_init_apple)

    def _sync_init_apple(self) -> bool:
        raw  = os.getenv("APPLE_AUTH_BEARER", "").strip()
        utok = os.getenv("APPLE_MUSIC_USER_TOKEN", "").strip()
        if not raw or not utok:
            return False
        bearer = raw if raw.startswith("Bearer ") else f"Bearer {raw}"
        am_headers = {
            "Authorization":            bearer,
            "media-user-token":         utok,
            "x-apple-music-user-token": utok,
            "Origin":  "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Accept":  "application/json",
        }
        try:
            resp = self._http_session.get(
                "https://amp-api.music.apple.com/v1/me/storefront",
                headers=am_headers, timeout=10,
            )
            if resp.status_code == 200:
                self._am_headers = am_headers
                self._http_session.headers.update(am_headers)
                self._am_storefront = (
                    resp.json().get("data", [{}])[0].get("id", "us")
                )
                return True
            print(f"[Apple Music] login {resp.status_code}: {resp.text[:120]}")
            return False
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # requests + Apple Music API pueden lanzar distintas excepciones
            print(f"[Apple Music] init failed: {exc}")
            return False

    # ── Playlist Fetching ──────────────────────────────────────────────

    async def fetch_playlist(
        self,
        platform: str,
        playlist_id: str,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[str, list[Track]]:
        self._cb[platform].check_or_raise()
        if platform == "Spotify":
            return await self._async_fetch_spotify(playlist_id, progress_cb)
        elif platform == "YouTube Music":
            return await asyncio.to_thread(self._sync_fetch_youtube, playlist_id, progress_cb)
        elif platform == "Apple Music":
            return await asyncio.to_thread(self._sync_fetch_apple, playlist_id, progress_cb)
        else:
            raise ValueError(f"Unknown platform: {platform}")

    def _spotify_playlist_items_to_tracks(
        self, raw: list, name: str, cb: Optional[Callable[[int, int, str], None]]
    ) -> list[Track]:
        tracks: list[Track] = []
        total = len(raw)
        for i, item in enumerate(raw, 1):
            t = item.get("track")
            if not t or not t.get("id"):
                continue
            ms = t["duration_ms"]
            dur = f"{int(ms/60000)}:{int((ms/1000)%60):02d}"
            imgs = t.get("album", {}).get("images", [])
            tracks.append(Track(
                id=t["id"], name=t["name"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                album=t.get("album", {}).get("name", ""),
                duration=dur,
                img_url=imgs[-1]["url"] if imgs else "",
                platform="Spotify",
            ))
            if cb and i % 50 == 0:
                cb(i, total, name)
        return tracks

    async def _async_fetch_spotify(
        self, pid: str, cb: Optional[Callable[[int, int, str], None]]
    ) -> tuple[str, list[Track]]:
        """
        Paginación con semáforo global, delay aleatorio asíncrono y reintento ante 429.
        """
        if not self._sp:
            await asyncio.to_thread(self._sync_init_spotify)
        sp = self._sp
        if not sp:
            return "Spotify Playlist", []

        async def _one_call(fn, *args, **kwargs):
            await asyncio.sleep(random.uniform(0.5, 1.5))
            while True:
                try:
                    async with GLOBAL_API_SEMAPHORE:
                        return await asyncio.to_thread(fn, *args, **kwargs)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    # spotipy puede lanzar SpotifyException o subclases de RequestException
                    if _is_spotify_rate_limited(e):
                        print(
                            "[WARNING] ⚠️ Spotify Rate Limit. Reintentando en 2s..."
                        )
                        await asyncio.sleep(2)
                        continue
                    raise

        info = await _one_call(sp.playlist, pid, fields="name")
        name = info.get("name", "Spotify Playlist")
        result = await _one_call(sp.playlist_tracks, pid)
        raw = list(result["items"])
        while result.get("next"):
            result = await _one_call(sp.next, result)
            raw.extend(result["items"])
        tracks = self._spotify_playlist_items_to_tracks(raw, name, cb)
        return name, tracks

    def _sync_fetch_youtube(self, pid: str, cb) -> tuple[str, list[Track]]:
        if not self._ytm:
            self._sync_init_youtube()
        if not self._ytm:
            raise RuntimeError(
                "YouTube Music no disponible. Comprueba browser.json y la consola."
            )
        try:
            pl = self._ytm.get_playlist(pid, limit=None)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # ytmusicapi no define jerarquía de excepciones pública estable
            self.youtube_auth_error = str(exc)
            if _is_ytm_unauthorized(exc):
                print(_YTM_401_MSG)
                raise RuntimeError(
                    "Sesión YouTube Music expirada (401). Renueva browser.json."
                ) from exc
            raise
        name  = pl.get("title", "YouTube Playlist")
        raw   = pl.get("tracks", [])
        total = len(raw)
        tracks = []
        for i, t in enumerate(raw, 1):
            thumbs = t.get("thumbnails", [])
            # artists es list[dict] en ytmusicapi — nunca asumir string
            tracks.append(Track(
                id=t["videoId"], name=t["title"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                album=(t.get("album") or {}).get("name", "Single"),
                duration=t.get("duration", "0:00"),
                img_url=thumbs[-1]["url"] if thumbs else "",
                platform="YouTube Music",
            ))
            if cb and i % 50 == 0:
                cb(i, total, name)
        return name, tracks

    def _sync_fetch_apple(self, pid: str, cb) -> tuple[str, list[Track]]:
        base   = "https://amp-api.music.apple.com/v1"
        is_lib = pid.startswith("p.")
        info_url = (
            f"{base}/me/library/playlists/{pid}"
            if is_lib else
            f"{base}/catalog/{self._am_storefront}/playlists/{pid}"
        )
        name = "Apple Music Playlist"
        try:
            r = self._http_session.get(info_url, timeout=10)
            if r.status_code == 429:
                raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
            if r.ok:
                name = r.json()["data"][0]["attributes"].get("name", name)
        except RateLimitError:
            raise  # pylint: disable=try-except-raise
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # Fallo al obtener nombre de playlist: continuamos con nombre por defecto

        tracks, url = [], f"{info_url}/tracks"
        while url:
            full = url if url.startswith("http") else f"https://amp-api.music.apple.com{url}"
            r    = self._http_session.get(full, timeout=10)
            if r.status_code == 429:
                raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
            r.raise_for_status()
            data = r.json()
            for item in data.get("data", []):
                attrs  = item.get("attributes", {})
                ms     = attrs.get("durationInMillis", 0)
                arturl = attrs.get("artwork", {}).get("url", "")
                if arturl:
                    arturl = arturl.replace("{w}", "60").replace("{h}", "60")
                tracks.append(Track(
                    id=item["id"], name=attrs.get("name", "Unknown"),
                    artist=attrs.get("artistName", "Unknown"),
                    album=attrs.get("albumName", "Unknown"),
                    duration=f"{int(ms/60000)}:{int((ms/1000)%60):02d}",
                    img_url=arturl, platform="Apple Music",
                ))
            url = data.get("next")
            if cb:
                cb(len(tracks), 0, name)
        return name, tracks

    # ── Search (transfer engine) ───────────────────────────────────────

    async def search_track(
        self,
        platform:         str,
        name:             str,
        artist:           str,
        local_duration_s: Optional[int] = None,
    ) -> SearchResult:
        """
        Búsqueda V4.0 Hunter: The Purge + semáforo por petición API (no por toda la tanda).
        """
        ct, ca = clean_metadata(name, artist)
        self._cb[platform].check_or_raise()
        await asyncio.sleep(random.uniform(0.5, 1.5))

        if platform == "YouTube Music":
            return await self._yt_hunter_async(
                ct, ca, name, artist, local_duration_s,
            )
        if platform == "Apple Music":
            return await self._am_hunter_async(ct, ca, name, artist)
        if platform == "Spotify":
            return await self._spotify_search_track_async(
                ct, ca, name, artist, local_duration_s,
            )
        return SearchResult(None, False)

    async def _spotify_search_track_async(
        self,
        song_title: str,
        artist_name: str,
        orig_name: str,
        orig_artist: str,
        local_duration_s: Optional[int],
    ) -> SearchResult:
        while True:
            try:
                return await self._sp_hunter_async(
                    song_title,
                    artist_name,
                    orig_name,
                    orig_artist,
                    local_duration_s,
                )
            except Exception as e:  # pylint: disable=broad-exception-caught
                # spotipy puede lanzar SpotifyException o subclases de RequestException
                if _is_spotify_rate_limited(e):
                    print(
                        "[WARNING] ⚠️ Spotify Rate Limit. Reintentando en 2s..."
                    )
                    await asyncio.sleep(2)
                    continue
                raise

    async def search_with_fallback(
        self,
        platform:         str,
        name:             str,
        artist:           str,
        local_duration_s: Optional[int] = None,
    ) -> SearchResult:
        """
        Fallback por variantes de título tras clean_metadata obligatorio.
        Hunter ya aplica reintentos agresivos; revisión solo si fuzzy <40%.
        """
        _explicit_re = re.compile(
            r'\s*[\(\[]\s*explicit\s*[\)\]]|\bexplicit\b',
            re.IGNORECASE,
        )
        _num_re = re.compile(r'\s*\b\d{4}\b')

        base_t, base_a = clean_metadata(name, artist)
        passes: list[tuple[str, str]] = [
            (base_t, base_a),
            (name.strip(), artist.strip()),
            (_normalize_title(name), base_a),
            (_strip_noise(base_t), base_a),
            (
                _num_re.sub('', _explicit_re.sub('', _normalize_title(name))).strip(),
                base_a,
            ),
        ]

        seen: set[tuple[str, str]] = set()
        for t_pass, a_pass in passes:
            t_pass = t_pass.strip()
            if not t_pass:
                continue
            key = (t_pass.lower(), a_pass.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            result = await self.search_track(
                platform, t_pass, a_pass, local_duration_s,
            )
            if result.track_id:
                return result
        return SearchResult(None, False)

    # ── YouTube Music — Hunter (validar_match / _yt_select_best) ────────────────

    def _yt_pack_result(self, chosen: dict, orig_name: str, orig_artist: str) -> SearchResult:
        found_title = chosen.get("title", "")
        farts = ", ".join(
            a.get("name", "")
            for a in (chosen.get("artists") or [])
            if isinstance(a, dict)
        )
        comb, tit, art = _fuzzy_scores_triple(orig_name, orig_artist, found_title, farts)
        needs, low = _fuzzy_flags_elastic(comb, tit, art)
        return SearchResult(
            chosen.get("videoId"), needs, low_confidence=low,
        )

    def _yt_sync_search_round(
        self,
        query: str,
        orig_name: str,
        orig_artist: str,
        local_duration_s: Optional[int],
        cached_results: Optional[list] = None,
    ) -> Optional[tuple[dict, int, int, int]]:
        """Una ronda de búsqueda YTM (sync, para to_thread). Devuelve chosen + triple fuzzy."""
        if cached_results is not None:
            results = cached_results
        else:
            results = self._ytm.search(query, filter="songs", limit=8)
        if not results:
            return None
        vid = _yt_select_best(orig_name, orig_artist, results, local_duration_s)
        if not vid:
            return None
        chosen = None
        for r in results[:8]:
            if r.get("videoId") != vid:
                continue
            if validar_match(orig_name, orig_artist, r, local_duration_s):
                chosen = r
                break
        if not chosen:
            chosen = next((r for r in results if r.get("videoId") == vid), None)
        if not chosen:
            return None
        ft = chosen.get("title", "")
        farts = ", ".join(
            a.get("name", "")
            for a in (chosen.get("artists") or [])
            if isinstance(a, dict)
        )
        comb, tit, art = _fuzzy_scores_triple(orig_name, orig_artist, ft, farts)
        return chosen, comb, tit, art

    def _yt_search_songs_sync(self, query: str) -> list:
        """Una llamada YTM search(songs); devuelve lista (posible vacía)."""
        if not self._ytm:
            return []
        r = self._ytm.search(query, filter="songs", limit=8)
        return list(r) if r else []

    async def _yt_hunter_async(
        self,
        ct: str,
        ca: str,
        orig_name: str,
        orig_artist: str,
        local_duration_s: Optional[int],
    ) -> SearchResult:
        """
        Fase 1: consultas «estrictas» (metadatos depurados). Si la API devuelve
        siempre lista vacía, fase 2 «raw fallback»: texto plano y Joji (p. ej. Fantasy).
        """
        if not self._ytm:
            return SearchResult(None, False)
        nt = _normalize_title(orig_name)
        na = _normalize_title(orig_artist)
        # ── Regla 2 + Regla 5: Protocolo SEO — formato estricto [Título] + [Artista] ──
        # SIEMPRE título primero via build_search_query (Regla 2).
        # Nunca al revés, nunca con símbolos basura.
        # La query primaria usa metadatos depurados (clean_metadata).
        # El fallback usa texto plano original cuando la API no devuelve nada.
        strict_q: list[str] = []
        for q in (
            build_search_query(ct, ca),
            build_search_query(nt, na),
            nt or ct,
        ):
            q = (q or "").strip()
            if q and q not in strict_q:
                strict_q.append(q)
        raw_q: list[str] = []
        for q in (
            build_search_query(orig_name.strip(), orig_artist.strip()),
            _joji_trikeyword_query(orig_name, orig_artist),
        ):
            q = (q or "").strip()
            if q and q not in raw_q and q not in strict_q:
                raw_q.append(q)

        def _process_pack(pack: Optional[tuple]) -> Optional[SearchResult]:
            if not pack:
                return None
            chosen, comb, tit, art = pack
            if _ideal_pass_hunter(comb, tit, art):
                return self._yt_pack_result(chosen, orig_name, orig_artist)
            return None

        strict_empty_api = True
        best: Optional[dict] = None
        best_comb = -1
        for query in strict_q:
            async with GLOBAL_API_SEMAPHORE:
                try:
                    results = await asyncio.to_thread(
                        self._yt_search_songs_sync, query,
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    # ytmusicapi no define jerarquía de excepciones pública estable
                    self.youtube_auth_error = str(exc)
                    if _is_ytm_unauthorized(exc):
                        print(_YTM_401_MSG)
                        raise RuntimeError(
                            "Sesión YouTube Music expirada (401). Renueva browser.json."
                        ) from exc
                    raise
            if results:
                strict_empty_api = False
            if not results:
                continue
            pack = await asyncio.to_thread(
                self._yt_sync_search_round,
                query,
                orig_name,
                orig_artist,
                local_duration_s,
                results,
            )
            ideal = _process_pack(pack)
            if ideal is not None:
                return ideal
            if pack:
                chosen, comb, _, _ = pack
                if comb > best_comb:
                    best_comb, best = comb, chosen

        if strict_empty_api and raw_q:
            for query in raw_q:
                async with GLOBAL_API_SEMAPHORE:
                    try:
                        results = await asyncio.to_thread(
                            self._yt_search_songs_sync, query,
                        )
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        # ytmusicapi no define jerarquía de excepciones pública estable
                        self.youtube_auth_error = str(exc)
                        if _is_ytm_unauthorized(exc):
                            print(_YTM_401_MSG)
                            raise RuntimeError(
                                "Sesión YouTube Music expirada (401). Renueva browser.json."
                            ) from exc
                        raise
                if not results:
                    continue
                pack = await asyncio.to_thread(
                    self._yt_sync_search_round,
                    query,
                    orig_name,
                    orig_artist,
                    local_duration_s,
                    results,
                )
                ideal = _process_pack(pack)
                if ideal is not None:
                    return ideal
                if pack:
                    chosen, comb, _, _ = pack
                    if comb > best_comb:
                        best_comb, best = comb, chosen

        if best is None:
            return SearchResult(None, False)
        return self._yt_pack_result(best, orig_name, orig_artist)

    # ── Apple Music — catalog search (storefront dinámico) ────────────

    def _am_candidates_for_term(self, term: str) -> list[tuple[str, str]]:
        q = quote(term)
        url = (
            f"https://api.music.apple.com/v1/catalog/{self._am_storefront}"
            f"/search?types=songs&term={q}&limit=5"
        )
        r = self._http_session.get(url, timeout=10)
        if r.status_code == 429:
            raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
        songs = r.json().get("results", {}).get("songs", {}).get("data", [])
        return [
            (
                f"{s['attributes'].get('name', '')} - {s['attributes'].get('artistName', '')}",
                s["id"],
            )
            for s in songs
        ]

    def _am_pick_catalog_best(
        self, song_title: str, artist_name: str, candidates: list[tuple[str, str]],
    ) -> SearchResult:
        if not candidates:
            return SearchResult(None, False)
        if not HAS_RAPIDFUZZ:
            tid = candidates[0][1]
            parts = candidates[0][0].split(" - ", 1)
            found_t, fa = parts[0], parts[1] if len(parts) > 1 else ""
            comb, tit, art = _fuzzy_scores_triple(song_title, artist_name, found_t, fa)
            needs, low = _fuzzy_flags_elastic(comb, tit, art)
            return SearchResult(tid, needs, low_confidence=low)
        ct, ca = clean_metadata(song_title, artist_name)
        ref = f"{ct} {ca}".lower()
        best_id: Optional[str] = None
        best_score = -1
        best_cand = ""
        for cand_str, tid in candidates:
            sc = int(_fuzz.token_sort_ratio(ref, cand_str.lower()))
            if sc > best_score:
                best_score, best_id, best_cand = sc, tid, cand_str
        if not best_id:
            return SearchResult(None, False)
        parts = best_cand.split(" - ", 1)
        found_t, fa = parts[0], (parts[1] if len(parts) > 1 else "")
        comb, tit, art = _fuzzy_scores_triple(song_title, artist_name, found_t, fa)
        needs, low = _fuzzy_flags_elastic(comb, tit, art)
        return SearchResult(best_id, needs, low_confidence=low)

    async def _am_hunter_async(
        self,
        ct: str,
        ca: str,
        orig_name: str,
        orig_artist: str,
    ) -> SearchResult:
        # ── Regla 2: Protocolo SEO — [Título] + [Artista] siempre ──────────
        terms: list[str] = []
        for t in (
            build_search_query(ct, ca),
            build_search_query(
                _normalize_title(orig_name),
                _normalize_title(orig_artist),
            ),
            _normalize_title(orig_name),
        ):
            t = t.strip()
            if t and t not in terms:
                terms.append(t)
        merged: list[tuple[str, str]] = []
        seen: set[str] = set()
        for term in terms:
            async with GLOBAL_API_SEMAPHORE:
                chunk = await asyncio.to_thread(
                    self._am_candidates_for_term, term,
                )
            for c in chunk:
                cid = c[1]
                if cid not in seen:
                    seen.add(cid)
                    merged.append(c)
        return self._am_pick_catalog_best(orig_name, orig_artist, merged)

    # ── Spotify — Hunter (field operators + limit=5) ───────────────────────────

    def _sp_search_items(self, q: str) -> list:
        r = self._safe_sp_call("search", q=q, type="track", limit=10)
        if r is None:
            return []
        return r.get("tracks", {}).get("items", [])

    def _sp_pick_best_item(
        self,
        items: list,
        orig_name: str,
        orig_artist: str,
        local_duration_s: Optional[int],
    ) -> tuple[Optional[dict], int, int, int]:
        if not items:
            return None, 0, 0, 0
        scored: list[tuple[dict, int, int, int]] = []
        for t in items:
            ft = t.get("name", "")
            fa = ", ".join(a["name"] for a in t.get("artists", []))
            comb, tit, art = _fuzzy_scores_triple(orig_name, orig_artist, ft, fa)
            scored.append((t, comb, tit, art))
        ideal = [x for x in scored if _ideal_pass_hunter(x[1], x[2], x[3])]
        pool = ideal if ideal else scored

        def _sort_key(x: tuple) -> tuple:
            t, comb, tit, art = x
            dur_pen = 0.0
            if local_duration_s is not None and t.get("duration_ms"):
                dur_pen = abs(int(t["duration_ms"] / 1000) - local_duration_s)
            return (-comb, dur_pen)

        pool.sort(key=_sort_key)
        best_t, comb, tit, art = pool[0]
        return best_t, comb, tit, art

    def _build_spotify_result(
        self, t: dict, comb: int, tit: int, art: int,
    ) -> SearchResult:
        needs, low = _fuzzy_flags_elastic(comb, tit, art)
        return SearchResult(t["id"], needs, low_confidence=low)

    async def _sp_hunter_async(
        self,
        ct: str,
        ca: str,
        orig_name: str,
        orig_artist: str,
        local_duration_s: Optional[int],
    ) -> SearchResult:
        """
        Hunter: primero búsquedas estructuradas track: / artist:;
        Deep Scan: texto plano + Joji (3 palabras) como en la web.
        """
        if not self._sp:
            await asyncio.to_thread(self._sync_init_spotify)
        if not self._sp:
            return SearchResult(None, False)
        nt = _normalize_title(orig_name)
        na = _normalize_title(orig_artist)
        structured: list[str] = []
        for q in (
            f"track:{ct} artist:{ca}",
            f"track:{nt} artist:{na}" if na else f"track:{nt}",
            f"track:{nt}" if nt else "",
        ):
            q = q.strip()
            if q and q not in structured:
                structured.append(q)
        plain: list[str] = []
        for q in (
            build_search_query(ct, ca),                                # ← Regla 2: [Título]+[Artista]
            build_search_query(orig_name.strip(), orig_artist.strip()),
            _joji_trikeyword_query(orig_name, orig_artist),
        ):
            q = q.strip()
            if q and q not in plain and q not in structured:
                plain.append(q)

        best: Optional[tuple[int, dict, int, int, int]] = None
        best_comb = -1

        for q in structured:
            async with GLOBAL_API_SEMAPHORE:
                items = await asyncio.to_thread(self._sp_search_items, q)
            if not items:
                continue
            picked, comb, tit, art = self._sp_pick_best_item(
                items, orig_name, orig_artist, local_duration_s,
            )
            if picked is None:
                continue
            if _ideal_pass_hunter(comb, tit, art):
                return self._build_spotify_result(picked, comb, tit, art)
            if comb > best_comb:
                best_comb = comb
                best = (comb, picked, tit, art)

        # Texto libre: si track:/artist: no devolvió filas, aquí está el fallback;
        # si ya hubo ítems, sigue ampliando candidatos (comportamiento Hunter).
        for q in plain:
            async with GLOBAL_API_SEMAPHORE:
                items = await asyncio.to_thread(self._sp_search_items, q)
            if not items:
                continue
            picked, comb, tit, art = self._sp_pick_best_item(
                items, orig_name, orig_artist, local_duration_s,
            )
            if picked is None:
                continue
            if _ideal_pass_hunter(comb, tit, art):
                return self._build_spotify_result(picked, comb, tit, art)
            if comb > best_comb:
                best_comb = comb
                best = (comb, picked, tit, art)

        if best is None:
            return SearchResult(None, False)
        _c, picked, tit, art = best
        return self._build_spotify_result(picked, _c, tit, art)

    # ── Playlist Creation ──────────────────────────────────────────────

    async def create_playlist(
        self, platform: str, title: str, track_ids: list[str]
    ) -> tuple[bool, str, int, list[str]]:
        self._cb[platform].check_or_raise()
        if platform == "YouTube Music":
            return await asyncio.to_thread(self._yt_create, title, track_ids)
        elif platform == "Apple Music":
            return await asyncio.to_thread(self._am_create, title, track_ids)
        elif platform == "Spotify":
            return await self._async_sp_create(title, track_ids)
        return False, "Platform not supported", 0, []

    async def _async_sp_create(
        self, title: str, ids: list[str]
    ) -> tuple[bool, str, int, list[str]]:
        if not self._sp:
            await asyncio.to_thread(self._sync_init_spotify)
        sp = self._sp
        if not sp:
            return False, "Spotify no disponible", 0, []

        async def _one_call(fn, *args, **kwargs):
            await asyncio.sleep(random.uniform(0.5, 1.5))
            while True:
                try:
                    async with GLOBAL_API_SEMAPHORE:
                        return await asyncio.to_thread(fn, *args, **kwargs)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    # spotipy puede lanzar SpotifyException o subclases de RequestException
                    if _is_spotify_rate_limited(e):
                        print(
                            "[WARNING] ⚠️ Spotify Rate Limit. Reintentando en 2s..."
                        )
                        await asyncio.sleep(2)
                        continue
                    raise

        me = await _one_call(sp.current_user)
        me_id = me["id"]
        pl = await _one_call(
            sp.user_playlist_create,
            me_id,
            title,
            True,
            False,
            "Transferida por MelomaniacPass",
        )
        await _one_call(sp.playlist_add_items, pl["id"], ids)
        return True, pl["id"], len(ids), []

    def _yt_create(self, title: str, ids: list[str]) -> tuple[bool, str, int, list[str]]:
        try:
            pl_id = self._ytm.create_playlist(
                title, "Transferida por MelomaniacPass", video_ids=ids
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # ytmusicapi no define jerarquía de excepciones pública estable
            self.youtube_auth_error = str(exc)
            if _is_ytm_unauthorized(exc):
                print(_YTM_401_MSG)
                raise RuntimeError(
                    "Sesión YouTube Music expirada (401). Renueva browser.json."
                ) from exc
            raise
        # Verify what actually landed in the playlist (ghost track detection)
        try:
            items = self._ytm.get_playlist(pl_id, limit=len(ids) + 10)
            confirmed_video_ids = {
                t.get("videoId") for t in items.get("tracks", [])
                if t.get("videoId")
            }
            rejected = [vid for vid in ids if vid not in confirmed_video_ids]
            confirmed_count = len(confirmed_video_ids)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # ytmusicapi puede fallar en la verificación; continuamos con el conteo enviado
            if _is_ytm_unauthorized(exc):
                print(_YTM_401_MSG)
                raise RuntimeError(
                    "Sesión YouTube Music expirada (401). Renueva browser.json."
                ) from exc
            # If verification fails, trust the sent count
            confirmed_count = len(ids)
            rejected = []
        return True, pl_id, confirmed_count, rejected

    def _am_create(self, title: str, ids: list[str]) -> tuple[bool, str, int, list[str]]:
        payload = {
            "attributes": {"name": title, "description": "Transferida por MelomaniacPass"},
            "relationships": {"tracks": {"data": [{"id": i, "type": "songs"} for i in ids]}},
        }
        r = self._http_session.post(
            "https://amp-api.music.apple.com/v1/me/library/playlists",
            json=payload, timeout=15,
        )
        if r.status_code == 429:
            raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
        r.raise_for_status()
        return True, "Playlist creada", len(ids), []


async def _search_with_exponential_rl_backoff(
    service: MusicApiService,
    platform: str,
    name: str,
    artist: str,
    *,
    local_duration_s: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
) -> SearchResult:
    """
    Reintenta `search_with_fallback` ante 429: primero espera Retry-After (s);
    en cada 429 siguiente duplica el tiempo de espera respecto al último usado.
    """
    rl_backoff: Optional[float] = None
    for step in range(RATE_LIMIT_BACKOFF_STEPS):
        try:
            return await service.search_with_fallback(
                platform, name, artist, local_duration_s=local_duration_s
            )
        except RateLimitError as e:
            ra = max(1, int(e.retry_after))
            if rl_backoff is None:
                rl_backoff = float(ra)
            if log:
                log(
                    f"[WARN] 429 {platform}: esperando {int(rl_backoff)}s "
                    f"(backoff · {step + 1}/{RATE_LIMIT_BACKOFF_STEPS})"
                )
            await asyncio.sleep(rl_backoff)
            rl_backoff *= 2.0
    if log:
        log(f"[ERROR] 429: agotados reintentos en {platform}")
    raise RateLimitError(platform, int(rl_backoff or 60))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §7  APP STATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AppState:
    """
    BLoC-style ViewModel. The UI registers listeners; state mutates only here.
    All mutations happen on the asyncio event loop.
    """

    PLATFORMS = ["Apple Music", "Spotify", "YouTube Music"]

    # ── Arquitecto de Ingesta — fuentes locales ────────────────────────
    # Regla 2 (Archivo Local) + Regla 3 (Portapapeles):
    # Estos orígenes no tienen plataforma nativa → exigen destino explícito.
    LOCAL_SOURCES: frozenset = frozenset({"Archivo Local", "Pegar Texto"})
    # Dropdown de origen: plataformas API + fuentes locales
    SOURCE_OPTIONS = ["Apple Music", "Spotify", "YouTube Music", "Archivo Local", "Pegar Texto"]

    def __init__(self, service: MusicApiService):
        self.service = service

        self.source:      str = "Apple Music"
        self.destination: str = "YouTube Music"
        # Regla 4 — La Regla de Hierro:
        # destination_confirmed = False cuando se carga desde fuente local.
        # Solo se confirma cuando el usuario selecciona explícitamente un destino.
        self.destination_confirmed: bool = True

        self.playlist_id:   str         = ""
        self.playlist_name: str         = "Cargar una playlist"
        self.tracks:        list[Track] = []
        self.filtered:      list[Track] = []
        self.load_state:    LoadState   = LoadState.IDLE
        self.load_error:    str         = ""

        self.transfer_state:    TransferState = TransferState.IDLE
        self.transfer_progress: int           = 0
        self.transfer_total:    int           = 0
        self.log_lines:         list[str]     = []
        self.failed_tracks:     list[Track]   = []

        # ── Triple-counter coherence tracking ──────────────────────────
        self.count_detected:   int           = 0   # raw tracks from source
        self.count_candidates: int           = 0   # passed metadata filter
        self.count_processed:  int           = 0   # found by search engine
        self.count_confirmed:  int           = 0   # confirmed inserted by dest API
        self.api_rejected_tracks: list[Track] = [] # found but rejected by dest API

        self.search_query: str = ""

        self.cb: dict[str, CircuitBreaker] = {
            p: CircuitBreaker(p) for p in self.PLATFORMS
        }
        self.service._cb = self.cb

        self.auth_session_ok: dict[str, bool] = {p: True for p in self.PLATFORMS}
        self.auth_session_hint: dict[str, str] = {p: "" for p in self.PLATFORMS}

        self.pending_review_tracks: list[Track] = []
        self.transfer_error_tracks: list[Track] = []

        self.lazy_scan_running: bool = False
        self.lazy_scan_done:    bool = False

        self._listeners: list[Callable[[], None]] = []
        self._lazy_task: Optional[asyncio.Task] = None

    # ── Observer API ───────────────────────────────────────────────────

    def subscribe(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception as e:  # pylint: disable=broad-exception-caught
                # Los callbacks de UI pueden lanzar distintas excepciones de Flet
                print(f"🔴 UI ERROR: {e}")
                traceback.print_exc()

    # ── Computed properties ────────────────────────────────────────────

    @property
    def selected_count(self) -> int:
        return sum(1 for t in self.tracks if t.selected)

    @property
    def select_all(self) -> bool:
        return all(t.selected for t in self.tracks) if self.tracks else False

    @property
    def display_tracks(self) -> list[Track]:
        return self.filtered if self.search_query else self.tracks

    # ── Actions ────────────────────────────────────────────────────────

    async def load_playlist(self, playlist_id: str) -> None:
        if not playlist_id.strip():
            return
        self.playlist_id   = playlist_id.strip()
        self.tracks        = []
        self.filtered      = []
        self.search_query  = ""
        self.load_state    = LoadState.LOADING_META
        self.load_error    = ""
        self.playlist_name = "Cargando metadatos…"
        self.lazy_scan_running = False
        self.lazy_scan_done    = False
        self.notify()

        def _progress(_fetched: int, total: int, name: str) -> None:
            self.playlist_name = name
            if total:
                self.load_state = LoadState.LOADING_TRACKS
            self.notify()

        # Cancel any in-flight lazy scan from a previous playlist
        if self._lazy_task and not self._lazy_task.done():
            self._lazy_task.cancel()
            self._lazy_task = None

        try:
            name, tracks = await self.service.fetch_playlist(
                self.source, self.playlist_id, _progress
            )
            self.playlist_name = name
            self.tracks        = tracks
            self.load_state    = LoadState.READY
        except RateLimitError as e:
            self.cb[self.source].trip(e.retry_after)
            self.load_state = LoadState.ERROR
            self.load_error = f"Rate limit en {e.platform}. Espera {e.retry_after}s."
        except Exception as e:  # pylint: disable=broad-exception-caught
            # fetch_playlist puede fallar por red, auth o parsing según la plataforma
            self.load_state = LoadState.ERROR
            self.load_error = str(e)
        finally:
            self.notify()

    def load_local_tracks(self, tracks: list, playlist_name: str = "Playlist Local") -> None:
        """Ingest locally-parsed tracks. Bypasses the API fetch pipeline."""
        # Cancel any running lazy scan from a prior load
        self.cancel_lazy_scan()

        self.playlist_id   = f"local_{uuid.uuid4().hex[:8]}"
        self.playlist_name = playlist_name
        self.tracks        = list(tracks)
        self.filtered      = []
        self.search_query  = ""
        self.load_state    = LoadState.READY
        self.load_error    = ""
        self.lazy_scan_running = False
        self.lazy_scan_done    = False

        # Regla 4: pistas sin plataforma nativa → exige que el usuario elija destino.
        self.destination_confirmed = False

        self._log(f"[INFO] Ingesta local · {len(tracks)} pistas cargadas")
        self.notify()

    def reset_session(self) -> None:
        """Clear all loaded data and return to IDLE state (Cloud Sync option)."""
        self.cancel_lazy_scan()
        self.playlist_id   = ""
        self.playlist_name = "Cargar una playlist"
        self.tracks        = []
        self.filtered      = []
        self.search_query  = ""
        self.load_state    = LoadState.IDLE
        self.load_error    = ""
        self.lazy_scan_running = False
        self.lazy_scan_done    = False
        self.transfer_state    = TransferState.IDLE
        self.transfer_progress = 0
        self.transfer_total    = 0
        self.failed_tracks     = []
        self.api_rejected_tracks = []
        self.pending_review_tracks = []
        self.transfer_error_tracks = []
        self.log_lines         = []
        self.destination_confirmed = True   # sesión limpia → confirmación reseteada
        self.notify()

    async def transfer_playlist(self) -> None:
        """
        Transfer engine v4.0 — Triple-counter coherence
        ────────────────────────────────────────────────
        • count_detected   : raw selected tracks
        • count_candidates : passed metadata validation
        • count_processed  : found by search_with_fallback
        • count_confirmed  : verified inserted by dest API
        • api_rejected_tracks : found but silently dropped by platform
        • Progress bar tracks count_confirmed (post-API reality)
        """
        selected = [t for t in self.tracks if t.selected]
        if not selected:
            return

        # Evita que el lazy scan (mismas ~N búsquedas) compita por GLOBAL_API_SEMAPHORE
        self.cancel_lazy_scan()
        self.lazy_scan_running = False
        self.lazy_scan_done    = False

        # ── Reset all counters ─────────────────────────────────────────
        self.transfer_state       = TransferState.RUNNING
        self.transfer_progress    = 0
        self.transfer_total       = len(selected)
        self.failed_tracks        = []
        self.api_rejected_tracks  = []
        self.pending_review_tracks = []
        self.transfer_error_tracks = []
        self.count_detected       = len(selected)
        self.count_candidates     = 0
        self.count_processed      = 0
        self.count_confirmed      = 0
        self._log(
            f"[INFO] Iniciando transferencia · "
            f"{self.count_detected} detectadas → {self.destination}"
        )
        self.notify()

        dest_ids:       list[str]  = []
        dest_id_to_track: dict[str, Track] = {}
        completed_count = 0
        BATCH_SIZE      = 10
        batch_pending   = 0

        async def _transfer_one(track: Track) -> Optional[str]:
            nonlocal completed_count, batch_pending

            # ── Metadata validation (The Purge) ───────────────────────
            cn, ca = clean_metadata(track.name, track.artist)
            if not cn.strip():
                track.transfer_status = "error"
                track.failure_reason = "Metadatos vacíos tras The Purge"
                self._log(
                    f"[ERROR] Metadatos vacíos, saltando: '{track.name[:42]}'"
                )
                if track not in self.failed_tracks:
                    self.failed_tracks.append(track)
                completed_count += 1
                self.transfer_progress = completed_count
                batch_pending += 1
                if batch_pending >= BATCH_SIZE:
                    batch_pending = 0
                    self.notify()
                return None

            self.count_candidates += 1
            cache_key   = f"{cn.lower()}|||{ca.lower()}|||{self.destination}"
            local_dur_s = _duration_to_seconds(track.duration)

            # ── Cache hit ─────────────────────────────────────────────
            if cache_key in self.service._search_cache:
                raw = self.service._search_cache[cache_key]
                cached = (
                    raw
                    if isinstance(raw, SearchResult)
                    else SearchResult(raw, False) if isinstance(raw, str) and raw
                    else SearchResult(None, False)
                )
                if not cached.track_id:
                    track.transfer_status = "not_found"
                    track.failure_reason = track.failure_reason or "Sin resultados (caché)"
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                elif cached.needs_review:
                    track.transfer_status = "revision_necesaria"
                    track.failure_reason = "Fuzzy <40% (caché)"
                    if track not in self.pending_review_tracks:
                        self.pending_review_tracks.append(track)
                    self._log(
                        f"[WARN]  ⚠ Revisión (caché): {track.name[:42]}"
                    )
                else:
                    track.transfer_status = "found"
                    self.count_processed += 1
                self._log(f"[INFO]  ⚡ Caché: {track.name[:42]}")
                completed_count += 1
                self.transfer_progress = completed_count
                batch_pending += 1
                if batch_pending >= BATCH_SIZE:
                    batch_pending = 0
                    self.notify()
                return (
                    cached.track_id
                    if cached.track_id and not cached.needs_review
                    else None
                )

            track.transfer_status = "searching"
            self._log(f"[INFO]  🔍 Buscando: {track.name[:42]}")

            match = SearchResult(None, False)
            last_exc: Optional[BaseException] = None
            for attempt in range(3):
                try:
                    match = await _search_with_exponential_rl_backoff(
                        self.service,
                        self.destination,
                        track.name,
                        track.artist,
                        local_duration_s=local_dur_s,
                        log=self._log,
                    )
                    break
                except RateLimitError:
                    raise  # pylint: disable=try-except-raise
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    # Red, auth o parsing pueden fallar en distintas formas entre plataformas
                    last_exc = exc
                    wait_s = 2 ** attempt
                    if attempt < 2:
                        self._log(
                            f"[ERROR] Intento {attempt+1}/3 · "
                            f"{track.name[:30]} — reintentando en {wait_s}s"
                        )
                        await asyncio.sleep(wait_s)

            self.service._search_cache[cache_key] = match

            if match.track_id and match.needs_review:
                track.transfer_status = "revision_necesaria"
                track.failure_reason = (
                    f"Confianza fuzzy <{FUZZY_REVISION_THRESHOLD}% (título/artista)"
                )
                if track not in self.pending_review_tracks:
                    self.pending_review_tracks.append(track)
                self._log(
                    f"[WARN]  ⚠ Revisión necesaria (fuzzy <{FUZZY_REVISION_THRESHOLD}%): "
                    f"{track.name[:42]}"
                )
            elif match.track_id and match.low_confidence:
                if getattr(track, 'platform', '') == 'local':
                    track.transfer_status = "not_found"
                    track.failure_reason = f"Similitud <{FUZZY_IDEAL}% (umbral local estricto)"
                    self._log(
                        f"[WARN]  ✗ Local · fuzzy <{FUZZY_IDEAL}% rechazado: {track.name[:42]}"
                    )
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                else:
                    track.transfer_status = "found"
                    self.count_processed += 1
                    self._log(
                        f"[INFO]  Hunter · fuzzy 70–84% (aceptado): {track.name[:42]}"
                    )
            elif match.track_id:
                track.transfer_status = "found"
                self.count_processed += 1
                self._log(f"[SUCCESS] ✓ Encontrada: {track.name[:42]}")
            else:
                track.transfer_status = "not_found"
                if last_exc:
                    track.failure_reason = _failure_reason_from_exc(last_exc)
                else:
                    track.failure_reason = "Sin resultados en la API del destino"
                self._log(f"[ERROR]   ✗ No encontrada: {track.name[:42]}")
                if track not in self.failed_tracks:
                    self.failed_tracks.append(track)

            completed_count += 1
            self.transfer_progress = completed_count
            batch_pending += 1
            if batch_pending >= BATCH_SIZE:
                batch_pending = 0
                self.notify()

            return (
                match.track_id
                if match.track_id and not match.needs_review
                else None
            )

        try:
            init_ok = await self._ensure_auth(self.destination)
            if not init_ok:
                raise RuntimeError(f"No se pudo autenticar en {self.destination}")

            # Bucle secuencial con respiración: cada canción cede el control al
            # event loop de Flet antes de procesar la siguiente. Esto permite que
            # los clics en la UI (Consola, Monitor, Wizard) se atiendan entre
            # canciones sin que la barra de progreso congele la aplicación.
            results: list = []
            for t in selected:
                try:
                    result = await _transfer_one(t)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    # Captura intencional: la excepción se almacena como resultado fallido
                    result = exc
                results.append(result)
                await asyncio.sleep(0)  # Yield → Flet "respira" entre pistas

            for track, result in zip(selected, results):
                if isinstance(result, RateLimitError):
                    self.cb[self.destination].trip(result.retry_after)
                    track.transfer_status = "error"
                    track.failure_reason = f"Rate limit ({result.retry_after}s)"
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                    if track not in self.transfer_error_tracks:
                        self.transfer_error_tracks.append(track)
                elif isinstance(result, Exception):
                    track.transfer_status = "error"
                    track.failure_reason = _failure_reason_from_exc(result)
                    self._log(f"[ERROR] Excepción en '{track.name[:30]}': {result}")
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                    if track not in self.transfer_error_tracks:
                        self.transfer_error_tracks.append(track)
                elif result:
                    dest_ids.append(result)
                    dest_id_to_track[result] = track

            self._log(
                f"[INFO]  🔎 Resumen pre-insert · "
                f"Detectadas: {self.count_detected} · "
                f"Candidatas: {self.count_candidates} · "
                f"Procesadas: {self.count_processed}"
            )
            if self.pending_review_tracks:
                names = ", ".join(
                    t.name[:36] for t in self.pending_review_tracks[:15]
                )
                if len(self.pending_review_tracks) > 15:
                    names += "…"
                self._log(
                    f"[INFO]  📋 Pendientes de revisión ({len(self.pending_review_tracks)}): "
                    f"{names}"
                )

            if dest_ids:
                self._log(f"[INFO]  📁 Creando playlist con {len(dest_ids)} canciones…")
                self.notify()
                ok, msg, confirmed_count, rejected_ids = await self.service.create_playlist(
                    self.destination, f"{self.playlist_name}", dest_ids
                )
                if ok:
                    self.count_confirmed  = confirmed_count
                    self.transfer_progress = confirmed_count
                    # Ghost track detection: mark api-rejected tracks
                    for vid in rejected_ids:
                        t = dest_id_to_track.get(vid)
                        if t:
                            t.transfer_status = "error"
                            self._log(
                                f"[ERROR] ⚠ No insertada por API ({self.destination}): "
                                f"{t.name[:42]}"
                            )
                            if t not in self.api_rejected_tracks:
                                self.api_rejected_tracks.append(t)
                    self._log(
                        f"[SUCCESS] ✅ Transferencia completa · "
                        f"Detectadas: {self.count_detected} · "
                        f"Procesadas: {self.count_processed} · "
                        f"Confirmadas: {self.count_confirmed} · "
                        f"Rechazadas API: {len(rejected_ids)} · "
                        f"No encontradas: {len(self.failed_tracks)}"
                    )
                    self.transfer_state = TransferState.DONE
                else:
                    raise RuntimeError(msg)
            elif any(t.transfer_status == "revision_necesaria" for t in selected):
                self._log(
                    "[WARN]  Solo coincidencias con baja confianza (revisión); "
                    "no se creó playlist automática en el destino."
                )
                self.transfer_state = TransferState.DONE
            else:
                raise RuntimeError("No se encontraron coincidencias en el destino.")

        except RateLimitError as e:
            self.cb[e.platform].trip(e.retry_after)
            self._log(f"[ERROR] ⚠ Rate limit en {e.platform}: espera {e.retry_after}s")
            self.transfer_state = TransferState.ERROR
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Errores de red, auth o parsing varían entre plataformas
            self._log(f"[ERROR] ✗ Error general: {e}")
            self.transfer_state = TransferState.ERROR
        finally:
            self.notify()

    async def _ensure_auth(self, platform: str) -> bool:
        if platform == "Spotify":
            return await self.service.init_spotify()
        elif platform == "YouTube Music":
            return await self.service.init_youtube()
        elif platform == "Apple Music":
            return await self.service.init_apple()
        return False

    def toggle_select_all(self) -> None:
        new_val = not self.select_all
        for t in self.tracks:
            t.selected = new_val
        self.notify()

    def toggle_track(self, track_id: str) -> None:
        for t in self.tracks:
            if t.id == track_id:
                t.selected = not t.selected
                break
        self.notify()

    def apply_search(self, query: str) -> None:
        self.search_query = query
        if not query:
            self.filtered = []
        else:
            q = query.lower()
            self.filtered = [
                t for t in self.tracks
                if q in t.name.lower() or q in t.artist.lower() or q in t.album.lower()
            ]
        self.notify()

    def set_source(self, val: str) -> None:
        self.source = val
        # Regla 4: Fuente local → el destino debe ser reconfirmado explícitamente.
        if val in self.LOCAL_SOURCES:
            self.destination_confirmed = False
        else:
            self.destination_confirmed = True
        self.notify()

    def set_destination(self, val: str) -> None:
        self.destination = val
        self.destination_confirmed = True   # El usuario eligió destino de forma explícita
        self.notify()

    def _log(self, msg: str) -> None:
        self.log_lines.append(msg)
        if len(self.log_lines) > 200:
            self.log_lines = self.log_lines[-200:]

    def cancel_lazy_scan(self) -> None:
        """Cancel any in-flight lazy availability scan."""
        if self._lazy_task and not self._lazy_task.done():
            self._lazy_task.cancel()
            self._lazy_task = None

    async def _lazy_availability_scan(self, tracks: list) -> None:
        """
        Background availability pre-check — runs after the list is loaded.

        • Uses search_with_fallback (same engine as transfer).
        • Mismo semáforo y delay que transfer; backoff 429 compartido.
        • Updates transfer_status: found | not_found | revision_necesaria.
        • Notifies the UI in micro-batches of 5 to keep the list responsive.
        • Cancelled automatically when a new playlist is loaded.
        • Skips tracks already in the in-memory cache.
        """
        # Only run if destination auth is available
        dest_ok = await self._ensure_auth(self.destination)
        if not dest_ok:
            return

        self.lazy_scan_running = True
        self.lazy_scan_done = False
        self.transfer_total = len(tracks)
        self.transfer_progress = 0
        self.notify()

        BATCH_SIZE = 5
        done_count = 0

        async def _check_one(track: Track) -> None:
            nonlocal done_count
            cn, ca = clean_metadata(track.name, track.artist)
            cache_key = f"{cn.lower()}|||{ca.lower()}|||{self.destination}"
            local_dur_s = _duration_to_seconds(track.duration)

            # Use cache if available
            if cache_key in self.service._search_cache:
                raw = self.service._search_cache[cache_key]
                res = (
                    raw
                    if isinstance(raw, SearchResult)
                    else SearchResult(raw, False) if isinstance(raw, str) and raw
                    else SearchResult(None, False)
                )
                if not res.track_id:
                    track.transfer_status = "not_found"
                elif res.needs_review:
                    track.transfer_status = "revision_necesaria"
                else:
                    track.transfer_status = "found"
            else:
                try:
                    result = await _search_with_exponential_rl_backoff(
                        self.service,
                        self.destination,
                        track.name,
                        track.artist,
                        local_duration_s=local_dur_s,
                        log=self._log,
                    )
                except asyncio.CancelledError:
                    raise  # pylint: disable=try-except-raise
                except RateLimitError:
                    result = SearchResult(None, False)
                except Exception:  # pylint: disable=broad-exception-caught
                    # Errores de búsqueda en lazy scan: marcar como no encontrado
                    result = SearchResult(None, False)
                self.service._search_cache[cache_key] = result
                if not result.track_id:
                    track.transfer_status = "not_found"
                elif result.needs_review:
                    track.transfer_status = "revision_necesaria"
                else:
                    track.transfer_status = "found"

            done_count += 1
            self.transfer_progress = done_count
            if done_count % BATCH_SIZE == 0:
                self.notify()

        try:
            tasks = [_check_one(t) for t in tracks]
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            self.lazy_scan_running = False
            self.notify()
            return
        self.lazy_scan_running = False
        self.lazy_scan_done = True
        self.notify()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §8  DESIGN TOKENS  — Dark OLED (solid)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Base surfaces ─────────────────────────────────────────────────────
BG_DEEP      = "#FF000000"
BG_PANEL     = "#FF080808"
BG_SURFACE   = "#FF111118"
BG_HOVER     = "#FF1E1E28"
BG_INPUT     = "#FF16161F"
SIDEBAR_BG   = "#FF0E0E15"
# Misma familia que SIDEBAR_BG, un punto más clara (lista principal vs. lateral)
BG_LIST      = "#FF161622"

# ── Chips / elevated panels ───────────────────────────────────────────
CHIP_BG      = "#FF1A1A22"
BORDER_LIGHT = "#FF3D4455"
BORDER_MUTED = "#FF2A3040"

# ── Accent palette ────────────────────────────────────────────────────
ACCENT       = "#FF4F8BFF"
ACCENT_DIM   = "#FF2D5FCC"
ACCENT_HALO  = "#FF2A3F5C"
SUCCESS      = "#FF00D084"
WARNING      = "#FFFFA500"
ERROR_COL    = "#FFFF4444"

# ── Typography ────────────────────────────────────────────────────────
TEXT_PRIMARY = "#FFF2F6FF"
TEXT_MUTED   = "#FF7A8499"
TEXT_DIM     = "#FF3D4455"

# ── Skeleton ──────────────────────────────────────────────────────────
SKELETON_DARK  = "#FF0E1016"
SKELETON_LIGHT = "#FF181C24"

ITEM_H = 64   # px — fixed row height (enables Flutter's ListView skip)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §9  UI COMPONENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SkeletonRow(ft.Container):
    """Animated shimmer row shown while tracks are loading."""

    def __init__(self, _index: int):
        self._pulse_task: Optional[asyncio.Task] = None

        self._num    = ft.Container(width=28, height=10, border_radius=3, bgcolor=SKELETON_DARK)
        self._thumb  = ft.Container(width=55, height=55, border_radius=8, bgcolor=SKELETON_DARK)
        self._title  = ft.Container(width=180, height=10, border_radius=3, bgcolor=SKELETON_DARK)
        self._artist = ft.Container(width=110, height=10, border_radius=3, bgcolor=SKELETON_DARK)
        self._dur    = ft.Container(width=36,  height=10, border_radius=3, bgcolor=SKELETON_DARK)
        self._chk    = ft.Container(width=18,  height=18, border_radius=4, bgcolor=SKELETON_DARK)

        super().__init__(
            height=ITEM_H,
            padding=ft.Padding.symmetric(horizontal=20, vertical=12),
            border=ft.Border.only(bottom=ft.BorderSide(0.5, "#FF252530")),
            content=ft.Row(
                controls=[self._num, self._thumb, self._title,
                           ft.Container(expand=True),
                           self._artist, self._dur, self._chk],
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            opacity=1.0,
        )

    async def start_pulse(self) -> None:
        self._pulse_task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    def stop_pulse(self) -> None:
        if self._pulse_task:
            self._pulse_task.cancel()


def _status_icon(status: str) -> ft.Control:
    icons = {
        "found":                (ft.Icons.CHECK_CIRCLE_OUTLINE,  SUCCESS),
        "not_found":            (ft.Icons.CANCEL_OUTLINED,        ERROR_COL),
        "searching":            (ft.Icons.LOOP,                   ACCENT),
        "transferred":          (ft.Icons.CLOUD_DONE_OUTLINED,    SUCCESS),
        "error":                (ft.Icons.ERROR_OUTLINE,          ERROR_COL),
        "pending":              (ft.Icons.RADIO_BUTTON_UNCHECKED, TEXT_DIM),
        "local_pending":        (ft.Icons.FOLDER_OPEN_OUTLINED,   WARNING),
        "revision_necesaria":   (ft.Icons.FLAG_OUTLINED,          WARNING),
    }
    ico, col = icons.get(status, (ft.Icons.RADIO_BUTTON_UNCHECKED, TEXT_DIM))
    return ft.Icon(ico, color=col, size=15)


class SongRow(ft.Container):
    """
    Song row. Hover uses solid BG_HOVER.
    Index column is centered; Title/Artist are left-aligned.
    """

    def __init__(self, track: Track, index: int, on_toggle: Callable[[str], None]):
        self._track     = track
        self._on_toggle = on_toggle

        # ── Regla 6: Protocolo de Arte ────────────────────────────────────
        # Pistas sin img_url (origen local o portapapeles) reciben un ícono
        # genérico de forma inmediata, sin intentar cargar imagen ni hacer scrapeo.
        if track.img_url:
            _thumb_content = ft.Image(
                src=track.img_url,
                fit=ft.BoxFit.COVER,
                error_content=ft.Icon(ft.Icons.MUSIC_NOTE, color=TEXT_DIM, size=18),
            )
        else:
            _thumb_content = ft.Container(
                content=ft.Icon(ft.Icons.MUSIC_NOTE, color=TEXT_DIM, size=20),
                alignment=ft.Alignment.CENTER,
            )

        self._thumb = ft.Container(
            width=55, height=55,
            border_radius=8,
            bgcolor=SKELETON_DARK,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=_thumb_content,
        )

        self._chk = ft.Checkbox(
            value=track.selected,
            fill_color={
                ft.ControlState.SELECTED: ACCENT,
                ft.ControlState.DEFAULT:  BG_SURFACE,
            },
            check_color=TEXT_PRIMARY,
            border_side=ft.BorderSide(1.5, TEXT_DIM),
            on_change=lambda e: on_toggle(track.id),
        )

        self._status_icon = _status_icon(track.transfer_status)

        # ── # column — centered ────────────────────────────────────────
        num_label = ft.Text(
            str(index), size=11, color=TEXT_MUTED,
            font_family="IBM Plex Sans",
            weight=ft.FontWeight.W_500,
            text_align=ft.TextAlign.CENTER,
            opacity=1.0,
        )

        title_text = ft.Text(
            track.name, size=13, color=TEXT_PRIMARY,
            font_family="IBM Plex Sans",
            weight=ft.FontWeight.W_600,
            overflow=ft.TextOverflow.ELLIPSIS,
            max_lines=1,
            opacity=1.0,
        )
        artist_text = ft.Text(
            track.artist, size=11, color=TEXT_MUTED,
            font_family="IBM Plex Sans",
            overflow=ft.TextOverflow.ELLIPSIS,
            max_lines=1,
            opacity=1.0,
        )
        dur_text = ft.Text(
            track.duration, size=11, color=TEXT_DIM,
            font_family="IBM Plex Sans",
            weight=ft.FontWeight.W_500,
            opacity=1.0,
        )

        row_content = ft.Row(
            controls=[
                # # column: centered
                ft.Container(
                    content=num_label,
                    width=32,
                    alignment=ft.Alignment.CENTER,
                ),
                self._thumb,
                # Title / Artist: left-aligned, expands
                ft.Column(
                    controls=[title_text, artist_text],
                    spacing=1,
                    tight=True,
                    expand=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Container(content=dur_text,          width=48,
                             alignment=ft.Alignment.CENTER),
                ft.Container(content=self._status_icon, width=26,
                             alignment=ft.Alignment.CENTER),
                ft.Container(content=self._chk,         width=32,
                             alignment=ft.Alignment.CENTER),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        super().__init__(
            height=ITEM_H,
            padding=ft.Padding.symmetric(horizontal=16, vertical=0),
            border=ft.Border.only(bottom=ft.BorderSide(0.5, "#FF252530")),
            border_radius=0,
            bgcolor=BG_LIST,
            animate=ft.Animation(100, ft.AnimationCurve.EASE_OUT),
            on_hover=self._on_hover,
            content=row_content,
        )

    def _on_hover(self, e: ft.HoverEvent) -> None:
        self.bgcolor = BG_HOVER if e.data == "true" else BG_LIST
        self.update()

    def refresh(self, track: Track) -> None:
        self._track      = track
        self._chk.value  = track.selected
        status_cell      = self.content.controls[4]
        status_cell.content = _status_icon(track.transfer_status)
        self.update()


def _section_label(text: str) -> ft.Text:
    return ft.Text(
        text, size=9, color=TEXT_DIM,
        font_family="IBM Plex Sans",
        weight=ft.FontWeight.W_700,
        style=ft.TextStyle(letter_spacing=1.4),
        opacity=1.0,
    )


def _primary_btn(text: str, icon: str, on_click, width=None, height=None) -> ft.Button:
    return ft.Button(
        content=ft.Text(text, opacity=1.0),
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.DEFAULT: ACCENT,
                ft.ControlState.HOVERED: "#6BA3FF",
                ft.ControlState.PRESSED: ACCENT_DIM,
            },
            color=TEXT_PRIMARY,
            elevation={ft.ControlState.DEFAULT: 0, ft.ControlState.HOVERED: 6},
            shadow_color={ft.ControlState.HOVERED: ACCENT_HALO},
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            animation_duration=120,
        ),
        width=width,
        height=height,
    )


def _ghost_btn(text: str, icon: str, on_click, width=None, height=None, disabled: bool = False) -> ft.OutlinedButton:
    return ft.OutlinedButton(
        content=ft.Text(text, opacity=1.0),
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        style=ft.ButtonStyle(
            color={
                ft.ControlState.DEFAULT: TEXT_MUTED,
                ft.ControlState.HOVERED: TEXT_PRIMARY,
            },
            side={
                ft.ControlState.DEFAULT: ft.BorderSide(0.8, "#2A3040"),
                ft.ControlState.HOVERED: ft.BorderSide(0.8, ACCENT),
            },
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            animation_duration=120,
        ),
        width=width,
        height=height,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §9b  TELEMETRY DRAWER  (Dual-Axis Adaptive)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TELE_HANDLE_H   = 35
_TELE_PANEL_H    = 300
_TELE_DOCKED_MIN = 700   # px — below: handle anchored; at/above: panel integrated in sidebar
_TELE_ANIM       = ft.Animation(400, ft.AnimationCurve.DECELERATE)


class TelemetryDrawer:
    """
    DOCKED   (window ≥ 700 px): 300 px panel integrated as last element of the sidebar
      scroll column; handle hidden; sidebar scroll=ADAPTIVE covers it naturally.
    OVERLAY  (window < 700 px): 35 px handle ANCHORED to the sidebar bottom (Stack);
      on click the panel expands RIGHTWARD via page.overlay (animate_size on width).
    Uses a plain ft.Row custom tab bar — no ft.TabBar/ft.Tabs (Flet 0.83 incompatibility).
    Two independent widget trees prevent any control having two parents.
    Default active tab: Consola (index 1) so logs are immediately visible.
    """

    _TAB_LABELS = ["Monitor", "Consola", "Post-Mortem"]
    _DEFAULT_TAB = 1   # Consola

    def __init__(self, page: ft.Page, sidebar_width: int = 300) -> None:
        self.page          = page
        self.sidebar_width = sidebar_width
        self._open         = False

        # ── Per-view data widgets (separate instances, no shared parents) ──
        self._d_log  = ft.ListView(spacing=0, expand=True)
        self._o_log  = ft.ListView(spacing=0, expand=True)
        self._d_pm   = ft.ListView(spacing=0, expand=True)
        self._o_pm   = ft.ListView(spacing=0, expand=True)
        self._d_pm_ph = ft.Text("Sin errores registrados", size=10, color=TEXT_DIM,
                                font_family="IBM Plex Sans", opacity=0.6,
                                text_align=ft.TextAlign.CENTER)
        self._o_pm_ph = ft.Text("Sin errores registrados", size=10, color=TEXT_DIM,
                                font_family="IBM Plex Sans", opacity=0.6,
                                text_align=ft.TextAlign.CENTER)
        self._d_cnts = self._mk_cnts()
        self._o_cnts = self._mk_cnts()

        self._last_failed: list = []          # persists until next playlist load
        self._pm_meta:    dict = {}           # destination/confirmed/detected metadata

        # ── Bodies: returns (column, panels, tab_btns) ─────────────────────
        d_body, self._d_panels, self._d_tab_btns = self._build_body(
            self._d_cnts, self._d_log, self._d_pm, self._d_pm_ph)
        o_body, self._o_panels, self._o_tab_btns = self._build_body(
            self._o_cnts, self._o_log, self._o_pm, self._o_pm_ph)

        # ── DOCKED panel ──────────────────────────────────────────────
        self.container = ft.Container(
            content=d_body,
            bgcolor=BG_PANEL,
            border=ft.Border.all(0.8, BORDER_LIGHT),
            border_radius=8,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            height=_TELE_PANEL_H,
            visible=True,
        )

        # ── OVERLAY panel (page.overlay, expands rightward) ──────────────
        self._overlay_panel = ft.Container(
            content=o_body,
            left=sidebar_width,
            bottom=0,
            width=0,
            height=_TELE_PANEL_H,
            bgcolor=BG_PANEL,
            border=ft.Border(
                top=ft.BorderSide(0.8, BORDER_LIGHT),
                right=ft.BorderSide(0.8, BORDER_LIGHT),
            ),
            border_radius=ft.BorderRadius.only(top_right=12),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            animate_size=_TELE_ANIM,
        )

        # ── Handle: pinned bottom via Stack, visible in OVERLAY mode only ──
        self._arrow = ft.Icon(icon=ft.Icons.ARROW_FORWARD, color=TEXT_DIM, size=14)
        self.handle = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(width=15, height=2, bgcolor=TEXT_DIM,
                                 border_radius=2, opacity=0.5),
                    ft.Container(width=5),
                    ft.Container(width=15, height=2, bgcolor=TEXT_DIM,
                                 border_radius=2, opacity=0.5),
                    ft.Container(width=6),
                    self._arrow,
                ],
                spacing=0,
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=80,
            height=_TELE_HANDLE_H,
            bottom=0,
            left=0,
            bgcolor=BG_PANEL,
            border_radius=ft.BorderRadius.only(top_left=12, top_right=12),
            border=ft.Border(
                top=ft.BorderSide(0.8, BORDER_LIGHT),
                left=ft.BorderSide(0.8, BORDER_LIGHT),
                right=ft.BorderSide(0.8, BORDER_LIGHT),
            ),
            on_click=self._toggle,
            ink=True,
            alignment=ft.Alignment.CENTER,
            visible=False,
        )

    # ── Internal builders ──────────────────────────────────────────────

    def _mk_cnts(self) -> dict:
        def _t(color): return ft.Text("—", size=11, color=color,
                                       font_family="IBM Plex Sans",
                                       weight=ft.FontWeight.W_600, opacity=1.0)
        return {
            "detected":   _t(TEXT_MUTED),
            "candidates": _t(TEXT_MUTED),
            "processed":  _t(ACCENT),
            "confirmed":  _t(SUCCESS),
            "rejected":   _t(ERROR_COL),
        }

    def _build_body(
        self, cnts: dict, log_list: ft.ListView,
        pm_list: ft.ListView, pm_ph: ft.Text,
    ) -> tuple:
        """Returns (body_column, panels_list, tab_btn_list)."""
        def _crow(label: str, val_node: ft.Text) -> ft.Row:
            lbl = ft.Text(label, size=9, color=TEXT_DIM,
                          font_family="IBM Plex Sans", opacity=1.0)
            return ft.Row([lbl, ft.Container(expand=True), val_node], spacing=0)

        # Panels — Consola visible by default
        panel_monitor = ft.Container(
            content=ft.Column([
                _crow("Detectadas",     cnts["detected"]),
                _crow("Candidatas",     cnts["candidates"]),
                _crow("Procesadas",     cnts["processed"]),
                _crow("Confirmadas",    cnts["confirmed"]),
                _crow("Rechazadas API", cnts["rejected"]),
            ], spacing=5),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            expand=True, visible=(self._DEFAULT_TAB == 0),
        )
        panel_consola = ft.Container(
            content=log_list,
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            expand=True, visible=(self._DEFAULT_TAB == 1),
        )
        export_btn = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.DOWNLOAD_OUTLINED, size=11, color=ACCENT),
                ft.Text("Exportar a TXT", size=10, color=ACCENT,
                        font_family="IBM Plex Sans", opacity=1.0),
            ], spacing=4, tight=True),
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            border=ft.Border.all(0.8, ACCENT),
            on_click=lambda _: self._do_export(),
            ink=True,
        )
        panel_postmortem = ft.Container(
            content=ft.Column([
                ft.Stack([
                    ft.Container(content=pm_ph, alignment=ft.Alignment.CENTER, expand=True),
                    pm_list,
                ], expand=True),
                ft.Container(
                    content=ft.Row([export_btn],
                                   alignment=ft.MainAxisAlignment.END),
                    padding=ft.Padding.only(right=4, bottom=4, top=2),
                ),
            ], spacing=0, expand=True),
            padding=ft.Padding.symmetric(horizontal=6, vertical=6),
            expand=True, visible=(self._DEFAULT_TAB == 2),
        )
        panels = [panel_monitor, panel_consola, panel_postmortem]

        # Custom tab row — no ft.TabBar/ft.Tabs dependency
        tab_btns: list[ft.Container] = []
        for i, label in enumerate(self._TAB_LABELS):
            active = (i == self._DEFAULT_TAB)
            btn = ft.Container(
                content=ft.Text(
                    label, size=10,
                    color=TEXT_PRIMARY if active else TEXT_DIM,
                    font_family="IBM Plex Sans",
                    weight=ft.FontWeight.W_600 if active else ft.FontWeight.W_400,
                    opacity=1.0,
                ),
                padding=ft.Padding.symmetric(horizontal=8, vertical=5),
                border_radius=ft.BorderRadius.only(top_left=5, top_right=5),
                bgcolor=BG_HOVER if active else ft.Colors.TRANSPARENT,
                border=ft.Border(bottom=ft.BorderSide(
                    1.5 if active else 0, ACCENT if active else ft.Colors.TRANSPARENT
                )),
                ink=True,
            )
            tab_btns.append(btn)

        # wire clicks using shared _switch_to_tab helper
        def _make_on_click(idx, _panels, _btns):
            def _on_click(_):
                self._switch_to_tab(idx, _panels, _btns)
                self.page.update()
            return _on_click

        for i, btn in enumerate(tab_btns):
            btn.on_click = _make_on_click(i, panels, tab_btns)

        tab_row = ft.Container(
            content=ft.Row(tab_btns, spacing=2),
            border=ft.Border(bottom=ft.BorderSide(0.8, BORDER_MUTED)),
            padding=ft.Padding.only(left=6, right=6, top=4, bottom=0),
        )

        body = ft.Column(
            controls=[tab_row, ft.Stack(controls=panels, expand=True)],
            spacing=0, expand=True,
        )
        return body, panels, tab_btns

    # ── Tab / Post-Mortem helpers ──────────────────────────────────

    def _switch_to_tab(self, idx: int, panels: list, btns: list) -> None:
        for j, (p, b) in enumerate(zip(panels, btns)):
            is_sel = (j == idx)
            p.visible = is_sel
            b.bgcolor = BG_HOVER if is_sel else ft.Colors.TRANSPARENT
            b.content.color  = TEXT_PRIMARY if is_sel else TEXT_DIM
            b.content.weight = ft.FontWeight.W_600 if is_sel else ft.FontWeight.W_400
            b.border = ft.Border(bottom=ft.BorderSide(
                1.5 if is_sel else 0,
                ACCENT if is_sel else ft.Colors.TRANSPARENT,
            ))

    def show_postmortem(self) -> None:
        """Focus Post-Mortem tab. In OVERLAY mode also opens the panel."""
        self._switch_to_tab(2, self._d_panels, self._d_tab_btns)
        self._switch_to_tab(2, self._o_panels, self._o_tab_btns)
        h = self.page.height or self.page.window.height or 720
        if h < _TELE_DOCKED_MIN and not self._open:
            self._open_overlay()
        else:
            self.page.update()

    def clear_postmortem(self) -> None:
        """Wipe Post-Mortem data. Called when a new playlist load starts."""
        self._last_failed = []
        self._pm_meta     = {}
        for lst, ph in ((self._d_pm, self._d_pm_ph), (self._o_pm, self._o_pm_ph)):
            lst.controls.clear()
            ph.visible = True

    def _snack(self, msg: str) -> None:
        s = ft.SnackBar(
            content=ft.Text(msg, font_family="IBM Plex Sans", size=12, opacity=1.0),
            bgcolor=BG_PANEL, duration=3500,
        )
        self.page.overlay.append(s)
        s.open = True
        self.page.update()

    def _do_export(self) -> None:
        import datetime as _dt
        tracks = self._last_failed
        if not tracks:
            self._snack("No hay datos de Post-Mortem para exportar.")
            return
        meta = self._pm_meta
        log_path = "transfer_failed_report.txt"
        lines = [
            "# MelomaniacPass — Reporte Post-Mortem\n",
            f"# Fecha: {_dt.datetime.now().isoformat()}\n",
            f"# Destino: {meta.get('destination', '?')}\n",
            f"# Confirmadas: {meta.get('confirmed', 0)} / "
            f"Detectadas: {meta.get('detected', 0)}\n\n",
        ]
        for t in tracks:
            reason = (getattr(t, "failure_reason", "") or "").strip()
            lines.append(f"{t.name} | {t.artist} | {reason or '—'}\n")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            self._snack(f"✓ Reporte guardado → {log_path}")
        except OSError as exc:
            self._snack(f"Error al exportar: {exc}")

    def sync_mode(self) -> None:
        """
        DOCKED  (≥700 px canvas): panel integrated in sidebar scroll column.
        OVERLAY (<700 px canvas): 35 px handle anchored to sidebar bottom.
        Uses page.height (canvas, no OS chrome) with fallback to window.height.
        """
        h = self.page.height or self.page.window.height or 720
        docked = h >= _TELE_DOCKED_MIN
        if docked:
            self.container.visible = True
            self.handle.visible    = False
            if self._open:
                self._close_overlay()
        else:
            self.container.visible = False
            self.handle.visible    = True

    # ── Overlay toggle ─────────────────────────────────────────────

    def _toggle(self, _=None) -> None:
        if self._open:
            self._close_overlay()
        else:
            self._open_overlay()

    def _open_overlay(self) -> None:
        self._open = True
        target_w = max(400, int((self.page.window.width or 1200) * 0.4))
        if self._overlay_panel not in self.page.overlay:
            self.page.overlay.append(self._overlay_panel)
        self._overlay_panel.width = target_w
        self._arrow.icon = ft.Icons.ARROW_BACK
        self.page.update()

    def _close_overlay(self) -> None:
        self._open = False
        self._overlay_panel.width = 0
        self._arrow.icon = ft.Icons.ARROW_FORWARD
        self.page.update()

    # ── Public data API ─────────────────────────────────────────────

    def update_counters(self, detected: int, candidates: int, processed: int,
                        confirmed: int, rejected: int) -> None:
        def _f(n: int) -> str: return str(n) if n else "—"
        for c in (self._d_cnts, self._o_cnts):
            c["detected"].value   = _f(detected)
            c["candidates"].value = _f(candidates)
            c["processed"].value  = _f(processed)
            c["confirmed"].value  = _f(confirmed)
            c["rejected"].value   = _f(rejected)

    def update_log(self, log_lines: list[str]) -> None:
        for lst in (self._d_log, self._o_log):
            lst.controls.clear()
            for line in log_lines[-80:]:
                col = (SUCCESS   if "[SUCCESS]" in line else
                       ERROR_COL if "[ERROR]"   in line else TEXT_MUTED)
                lst.controls.append(
                    ft.Text(f"› {line}", size=9, color=col,
                            font_family="IBM Plex Sans", opacity=1.0)
                )

    def update_postmortem(
        self, failed_tracks, *,
        destination: str = "", confirmed: int = 0, detected: int = 0,
    ) -> None:
        self._last_failed = list(failed_tracks)
        self._pm_meta = dict(destination=destination, confirmed=confirmed, detected=detected)
        has = bool(self._last_failed)
        for lst, ph in ((self._d_pm, self._d_pm_ph), (self._o_pm, self._o_pm_ph)):
            lst.controls.clear()
            ph.visible = not has
            for t in self._last_failed:
                reason = (getattr(t, "failure_reason", "") or "").strip()
                label  = f"✗  {t.name}  —  {t.artist}"
                if reason:
                    label += f"  ·  {reason[:48]}"
                lst.controls.append(
                    ft.Text(label, size=9, color=ERROR_COL,
                            font_family="IBM Plex Sans", opacity=1.0)
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §10  MelomaniacPass UI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PlaylistManagerUI:
    """
    Pure UI class. Knows nothing about APIs. Reacts to AppState changes.

    Layout
    ──────
        ┌─────────────────────┬────────────────────────────────────────┐
        │  SIDEBAR            │  MAIN (track list)                    │
        │  ─────────────────  │  ┌──────────────────────────────────┐  │
        │  Logo               │  │ Header (title · search · sel-all) │  │
        │  Platforms          │  ├──────────────────────────────────┤  │
        │  Playlist ID        │  │ Column headers                    │  │
        │  Actions            │  ├──────────────────────────────────┤  │
        │  Rate-limit         │  │ Virtual ListView (rows/skeletons) │  │
        │  Log console        │  └──────────────────────────────────┘  │
        └─────────────────────┴────────────────────────────────────────┘
    """

    SKELETON_COUNT = 14

    def __init__(self, page: ft.Page, state: AppState):
        self.page  = page
        self.state = state

        self._search_task:       Optional[asyncio.Task] = None
        self._skeleton_tasks:    list[asyncio.Task]     = []
        self._row_cache:         dict[str, SongRow]     = {}
        self._failed_dialog_shown:    bool  = False
        self._transfer_start:         float = 0.0
        self._completion_snack_shown: bool  = False
        self._pm_cleared_for_load:    bool  = False

        # ── Universal Ingestion ────────────────────────────────────────
        self._file_picker = ft.FilePicker()
        page.services.append(self._file_picker)

        self._paste_field = ft.TextField(
            multiline=True,
            min_lines=10,
            max_lines=10,
            hint_text="Pega aquí tu lista  (ej: Título - Artista, una por línea)",
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            bgcolor=BG_INPUT,
            border_color=BORDER_LIGHT,
            focused_border_color=ACCENT,
            border_radius=10,
            expand=True,
        )

        self._build_sidebar()
        self._build_content()

        # BG_LIST: lo que asoma en las esquinas redondeadas del sidebar debe ser el mismo
        # tono que el panel de lista, no el negro de BG_DEEP.
        self.root = ft.Container(
            content=ft.Row(
                controls=[self._sidebar, self._content],
                spacing=0,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            bgcolor=BG_LIST,
            expand=True,
        )

        state.subscribe(self._on_state_changed)
        for platform, cb in state.cb.items():
            cb.subscribe(
                lambda is_open, rem, p=platform: self._on_circuit_change(p, is_open, rem)
            )
        page.on_resize = lambda _: (
            self._telemetry.sync_mode(), self.page.update()
        )

        async def _initial_sync():
            await asyncio.sleep(0.15)   # wait for first render to get real page.height
            self._telemetry.sync_mode()
            self.page.update()

        page.run_task(_initial_sync)

    async def _refresh_auth_live(self) -> None:
        """Validación activa de sesiones (semáforo real, no solo color)."""
        am = getattr(self, "auth_manager", None)
        if am:
            await am.refresh_session_icons()

    async def _on_auth_probe(self, platform: str) -> None:
        """Valida sesiones y abre el wizard en la pestaña correcta si la plataforma falla."""
        am = getattr(self, "auth_manager", None)
        if not am:
            return
        self.state._log(f"[INFO] ⏳ Revalidando sesión de {platform}...")
        self.page.update()
        results = await am.check_all_sessions()
        am.ingest_preflight_results(results)
        for r in results:
            if r.platform != platform:
                continue
            if not r.ok:
                self.state._log(
                    f"[ERROR] ⚠ {platform} falló la validación. Abriendo wizard."
                )
                am.open_wizard(platform)
            else:
                self.state._log(f"[SUCCESS] ✓ {platform} validada correctamente.")
                self._snack(f"Sesión de {platform} válida y activa.")
            break

    def _on_open_wizard(self, _e: ft.ControlEvent) -> None:
        am = getattr(self, "auth_manager", None)
        if not am:
            self.state._log("[ERROR] AuthManager no disponible (wizard).")
            return
        am.open_wizard()

    def _close_postmortem_dialog(self) -> None:
        s = self.state
        s.pending_review_tracks.clear()
        s.failed_tracks.clear()
        s.api_rejected_tracks.clear()
        s.transfer_error_tracks.clear()
        self._failed_dialog_shown = False
        try:
            self.page.pop_dialog()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # page.pop_dialog() puede fallar si no hay diálogo abierto

    # ══════════════════════════════════════════════════════════════════
    # BUILD — SIDEBAR
    # ══════════════════════════════════════════════════════════════════

    def _build_sidebar(self) -> None:
        s = self.state

        self.btn_wizard = ft.IconButton(
            icon=ft.Icons.SETTINGS_OUTLINED,
            icon_color=TEXT_DIM,
            icon_size=16,
            tooltip="Configurar credenciales",
            on_click=self._on_open_wizard,
            style=ft.ButtonStyle(
                overlay_color={ft.ControlState.HOVERED: BG_HOVER},
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
        )
        logo = ft.Column([
            ft.Row([
                ft.Container(
                    content=ft.Icon(ft.Icons.HEADPHONES, color=ACCENT, size=22),
                    # Subtle glow halo around the logo icon
                    bgcolor=ACCENT_HALO,
                    border_radius=8,
                    padding=ft.Padding.all(6),
                ),
                ft.Column([
                    ft.Text(spans=[
                        ft.TextSpan(
                            "Melomaniac", 
                            ft.TextStyle(
                                size=16, 
                                weight=ft.FontWeight.W_300, # Un poco más fino
                                color=TEXT_PRIMARY, 
                                font_family="IBM Plex Sans"
                            )
                        ),
                        ft.TextSpan(
                            "Pass", 
                            ft.TextStyle(
                                size=16, 
                                weight=ft.FontWeight.W_700, # Más pesado para el "Pass"
                                color=TEXT_PRIMARY, 
                                font_family="IBM Plex Sans"
                            )
                        ),
                    ],
                    opacity=1.0),
                    ft.Text("v4.5", size=9, color=TEXT_DIM,
                            font_family="IBM Plex Sans",
                            style=ft.TextStyle(letter_spacing=0.8),
                            opacity=1.0),
                ], spacing=0, tight=True, expand=True),
                self.btn_wizard,
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=0)

        # ── Platform selectors ─────────────────────────────────────────
        _dd_style = dict(
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT,
            label_style=ft.TextStyle(color=TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            border_radius=10, expand=True,
        )
        def _on_src_select(e) -> None:
            val = e.control.value
            s.set_source(val)
            if val == "Archivo Local":
                # Regla 2: abre el explorador nativo inmediatamente
                asyncio.create_task(self._do_local_pick())
            elif val == "Pegar Texto":
                # Regla 3: despliega el cuadro de diálogo de portapapeles
                self._open_paste_dialog()
            else:
                # Regla 1: plataforma streaming → refrescar auth, mostrar campo ID
                asyncio.create_task(self._refresh_auth_live())

        def _on_dst_select(e) -> None:
            s.set_destination(e.control.value)
            asyncio.create_task(self._refresh_auth_live())

        self._src_dd = ft.Dropdown(
            label="Origen", value=s.source,
            options=[ft.DropdownOption(key=p, text=p) for p in AppState.SOURCE_OPTIONS],
            on_select=_on_src_select,
            **_dd_style,
        )
        self._dst_dd = ft.Dropdown(
            label="Destino", value=s.destination,
            options=[ft.DropdownOption(key=p, text=p) for p in AppState.PLATFORMS],
            on_select=_on_dst_select,
            **_dd_style,
        )
        self._status_badge = ft.Text("", size=10, color=SUCCESS, font_family="IBM Plex Sans",
                                     opacity=1.0)
        self._dest_session_warn = ft.Text(
            "", size=9, color=ERROR_COL, font_family="IBM Plex Sans", visible=False,
        )

        platform_section = ft.Column([
            _section_label("PLATAFORMAS"),
            ft.Row([self._src_dd, self._dst_dd], spacing=8),
            self._status_badge,
            self._dest_session_warn,
        ], spacing=8)

        # ── Playlist ID ────────────────────────────────────────────────
        self._id_field = ft.TextField(
            label="ID de la Playlist",
            hint_text="pl.u-xxxx  /  PLxxxx  /  37i9dQ…",
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT,
            label_style=ft.TextStyle(color=TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            border_radius=10, focused_border_color=ACCENT,
            on_submit=self._do_cloud_load,
        )

        # ── Playlist ID — visible solo cuando el origen es una plataforma streaming ──
        # Regla 1: el campo de texto es el punto de entrada para API directa.
        # Reglas 2 & 3: las fuentes locales no necesitan un ID de playlist.
        self._playlist_section = ft.Column([
            _section_label("PLAYLIST"),
            self._id_field,
        ], spacing=8, visible=(s.source not in AppState.LOCAL_SOURCES))

        self._playlist_divider = ft.Divider(
            height=1, color=BORDER_MUTED, thickness=0.5,
            visible=(s.source not in AppState.LOCAL_SOURCES),
        )

        # ── Action buttons — rejilla 2×2, tamaño fijo ──────────────────
        _BTN_W, _BTN_H = 129, 44
        self._load_btn     = _primary_btn("Cargar",      ft.Icons.DOWNLOAD,   self._on_load,     width=_BTN_W, height=_BTN_H)
        self._transfer_btn = _ghost_btn(  "Transferir",  ft.Icons.SWAP_HORIZ, self._on_transfer, width=_BTN_W, height=_BTN_H)
        self._sync_btn     = _ghost_btn(  "Sincronizar", ft.Icons.SYNC,       lambda _: None,    width=_BTN_W, height=_BTN_H, disabled=True)
        self._split_btn    = _ghost_btn(  "Dividir",     ft.Icons.CALL_SPLIT, lambda _: None,    width=_BTN_W, height=_BTN_H, disabled=True)

        actions = ft.Column([
            ft.Row([self._load_btn,  self._transfer_btn], spacing=6),
            ft.Row([self._sync_btn, self._split_btn],     spacing=6),
        ], spacing=6)

        # ── Rate-limit banner ──────────────────────────────────────────
        self._rl_banner = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.TIMER_OUTLINED, color=WARNING, size=14),
                ft.Text("", size=10, color=WARNING, font_family="IBM Plex Sans", opacity=1.0),
            ], spacing=6),
            bgcolor=BG_PANEL,
            border=ft.Border.all(0.8, WARNING),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            visible=False,
        )

        # ── Progress bar ───────────────────────────────────────────────
        self._progress_bar = ft.ProgressBar(
            value=0, bgcolor=BG_SURFACE, color=ACCENT, border_radius=4,
        )
        self._progress_row = ft.Container(
            content=ft.Column([
                self._progress_bar,
                ft.Row([
                    ft.Text("", size=10, color=TEXT_MUTED, font_family="IBM Plex Sans", opacity=1.0),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ], spacing=4),
            visible=False,
            border=ft.Border.all(0.8, BORDER_LIGHT),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )

        # ── Telemetry Drawer ────────────────────────────────────────────
        self._telemetry = TelemetryDrawer(self.page, sidebar_width=300)

        # ── Assemble sidebar ───────────────────────────────────────────
        fixed_top = ft.Column(
            controls=[
                logo,
                ft.Divider(height=1, color=BORDER_MUTED, thickness=0.5),
                platform_section,
                ft.Divider(height=1, color=BORDER_MUTED, thickness=0.5),
                self._playlist_section,
                self._playlist_divider,
                _section_label("ACCIONES"),
                actions,
            ],
            spacing=12,
        )

        scrollable_bottom = ft.Column(
            controls=[
                self._rl_banner,
                self._telemetry.container,
            ],
            spacing=12,
            scroll=ft.ScrollMode.ADAPTIVE,
            expand=True,
        )

        # Sidebar uses Stack so the OVERLAY handle can be pinned to bottom=0
        sidebar_col = ft.Column(
            controls=[fixed_top, scrollable_bottom],
            spacing=12,
            expand=True,
        )
        sidebar_stack = ft.Stack(
            controls=[sidebar_col, self._telemetry.handle],
            expand=True,
        )

        self._sidebar = ft.Container(
            width=300,
            padding=ft.Padding.all(18),
            bgcolor=SIDEBAR_BG,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            border_radius=ft.BorderRadius.only(top_right=14, bottom_right=14),
            border=ft.Border.only(right=ft.BorderSide(1, BORDER_LIGHT)),
            content=sidebar_stack,
        )

    # ══════════════════════════════════════════════════════════════════
    # BUILD — CONTENT AREA
    # ══════════════════════════════════════════════════════════════════

    def _build_content(self) -> None:
        self._playlist_title = ft.Text(
            "Cargar una playlist", size=22, weight=ft.FontWeight.W_700,
            color=TEXT_PRIMARY, font_family="IBM Plex Sans",
            opacity=1.0,
        )
        self._track_count = ft.Text(
            "", size=12, color=TEXT_MUTED, font_family="IBM Plex Sans",
            opacity=1.0,
        )

        self._search_field = ft.TextField(
            hint_text="Buscar título, artista…",
            prefix_icon=ft.Icons.SEARCH,
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT,
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            border_radius=10, focused_border_color=ACCENT,
            width=240, height=38,
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            on_change=self._on_search_change,
        )

        _ib_style = dict(
            icon_size=17,
            style=ft.ButtonStyle(
                padding=4,
                bgcolor={ft.ControlState.DEFAULT: ft.Colors.TRANSPARENT},
            ),
        )
        self._auth_yt = ft.IconButton(
            icon=ft.Icons.VIDEO_LIBRARY_OUTLINED,
            icon_color=TEXT_DIM,
            tooltip="YouTube Music · clic = validar sesión ahora",
            on_click=lambda _: asyncio.create_task(
                self._on_auth_probe("YouTube Music")
            ),
            **_ib_style,
        )
        self._auth_sp = ft.IconButton(
            icon=ft.Icons.MUSIC_NOTE,
            icon_color=TEXT_DIM,
            tooltip="Spotify · clic = validar sesión ahora",
            on_click=lambda _: asyncio.create_task(self._on_auth_probe("Spotify")),
            **_ib_style,
        )
        self._auth_am = ft.IconButton(
            icon=ft.Icons.APPLE,
            icon_color=TEXT_DIM,
            tooltip="Apple Music · clic = validar sesión ahora",
            on_click=lambda _: asyncio.create_task(
                self._on_auth_probe("Apple Music")
            ),
            **_ib_style,
        )
        self._auth_strip = ft.Row(
            controls=[self._auth_yt, self._auth_sp, self._auth_am],
            spacing=2,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self._select_all_chk = ft.Checkbox(
            label="Todo",
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11, font_family="IBM Plex Sans"),
            fill_color={ft.ControlState.SELECTED: ACCENT},
            check_color=TEXT_PRIMARY,
            border_side=ft.BorderSide(1.5, TEXT_DIM),
            on_change=lambda _: self.state.toggle_select_all(),
        )

        # ── Content-area progress (transferencia / lazy scan) ────────────
        self._content_progress_bar = ft.ProgressBar(
            value=0, bgcolor=BG_SURFACE, color=ACCENT, border_radius=4,
        )
        self._content_prog_label = ft.Text(
            "", size=10, color=TEXT_MUTED, font_family="IBM Plex Sans", opacity=0.6,
        )
        self._content_eta_label = ft.Text(
            "", size=10, color=TEXT_DIM, font_family="IBM Plex Sans", opacity=0.45,
        )
        self._content_progress = ft.Container(
            content=ft.Column([
                self._content_progress_bar,
                ft.Row([
                    self._content_prog_label,
                    ft.Container(expand=True),
                    self._content_eta_label,
                ], spacing=0),
            ], spacing=4),
            visible=False,
            border=ft.Border.all(0.8, BORDER_LIGHT),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )

        header_bar = ft.Row(
            controls=[
                ft.Column([self._playlist_title, self._track_count], spacing=2),
                ft.Container(expand=True),
                self._auth_strip,
                self._search_field,
                self._select_all_chk,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ── Column headers ─────────────────────────────────────────────
        def _col_header(
            text: str,
            width=None,
            expand=False,
            center: bool = False,
        ) -> ft.Container:
            align = ft.Alignment.CENTER if center else ft.Alignment.CENTER_LEFT
            ctrl  = ft.Text(
                text, size=9, color=TEXT_DIM,
                weight=ft.FontWeight.W_700,
                font_family="IBM Plex Sans",
                style=ft.TextStyle(letter_spacing=0.8),
                text_align=ft.TextAlign.CENTER if center else ft.TextAlign.LEFT,
                opacity=1.0,
            )
            return ft.Container(
                content=ctrl, width=width, expand=expand, alignment=align,
            )

        col_headers = ft.Container(
            content=ft.Row(
                controls=[
                    _col_header("#",               width=32, center=True),   # centered
                    _col_header("PORTADA",         width=55, center=True),
                    _col_header("TÍTULO / ARTISTA", expand=True),             # left
                    _col_header("DUR.",            width=48, center=True),
                    _col_header("",                width=26, center=True),
                    _col_header("SEL.",            width=32, center=True),
                ],
                spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=8),
            bgcolor=CHIP_BG,
            border_radius=12,
            border=ft.Border.all(0.8, BORDER_LIGHT),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

        # ── Virtual list ───────────────────────────────────────────────
        self._list_view = ft.ListView(
            item_extent=ITEM_H,
            spacing=0,
            expand=True,
            padding=ft.Padding.only(bottom=20),
        )
        # Stack: solo hijos directos pueden usar left/top/right/bottom (equivalente a Positioned.fill)
        _stack_fill = dict(left=0, top=0, right=0, bottom=0)

        self._list_view_wrap = ft.Container(
            content=self._list_view,
            bgcolor=BG_LIST,
            visible=False,
            **_stack_fill,
        )

        # ── Skeletons ──────────────────────────────────────────────────
        self._skeletons = [SkeletonRow(i) for i in range(self.SKELETON_COUNT)]
        self._skeleton_view = ft.ListView(
            item_extent=ITEM_H,
            spacing=0,
            expand=True,
            controls=self._skeletons,
            visible=True,
        )
        self._skeleton_view_wrap = ft.Container(
            content=self._skeleton_view,
            bgcolor=BG_LIST,
            visible=False,
            **_stack_fill,
        )

        # ── Empty state (debe ir encima del wrap de lista en el Stack — si no, el fondo la tapa)
        self._empty_hint_text = ft.Text(
            "Introduce el ID en el panel izquierdo y pulsa «Cargar».",
            size=12,
            color=TEXT_DIM,
            font_family="IBM Plex Sans",
            opacity=1.0,
            text_align=ft.TextAlign.CENTER,
        )
        self._empty_state = ft.Container(
            bgcolor=BG_LIST,
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Icon(ft.Icons.LIBRARY_MUSIC, size=52, color=TEXT_DIM),
                        bgcolor=CHIP_BG,
                        border=ft.Border.all(0.8, BORDER_LIGHT),
                        border_radius=20,
                        padding=ft.Padding.all(20),
                    ),
                    ft.Text(
                        "Carga una playlist",
                        size=20,
                        color=TEXT_PRIMARY,
                        font_family="IBM Plex Sans",
                        weight=ft.FontWeight.W_700,
                        opacity=1.0,
                    ),
                    ft.Text(
                        "Sin playlist cargada",
                        size=14,
                        color=TEXT_MUTED,
                        font_family="IBM Plex Sans",
                        weight=ft.FontWeight.W_500,
                        opacity=1.0,
                    ),
                    self._empty_hint_text,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            alignment=ft.Alignment.CENTER,
            visible=True,
            **_stack_fill,
        )

        # ── Error state ────────────────────────────────────────────────
        self._error_text  = ft.Text(
            "", size=13, color=ERROR_COL, font_family="IBM Plex Sans", opacity=1.0,
        )
        self._error_state = ft.Container(
            bgcolor=BG_LIST,
            content=ft.Column([
                ft.Icon(ft.Icons.ERROR_OUTLINE, size=48, color=ERROR_COL),
                self._error_text,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            alignment=ft.Alignment.CENTER,
            visible=False,
            **_stack_fill,
        )

        list_area = ft.Stack(
            controls=[
                self._list_view_wrap,
                self._skeleton_view_wrap,
                self._empty_state,
                self._error_state,
            ],
            expand=True,
        )

        self._content = ft.Container(
            expand=True,
            bgcolor=BG_LIST,
            padding=ft.Padding.all(24),
            content=ft.Column(
                controls=[self._content_progress, header_bar, col_headers, list_area],
                spacing=10,
                expand=True,
            ),
        )

    # ══════════════════════════════════════════════════════════════════
    # STATE REACTIONS
    # ══════════════════════════════════════════════════════════════════

    def _on_state_changed(self) -> None:
        s = self.state

        # ── Arquitecto de Ingesta — visibilidad adaptativa ────────────────
        # Reglas 1 vs 2/3: el campo de ID de playlist solo aplica a APIs streaming.
        # Para fuentes locales se oculta; la sección de ACCIONES sube al hueco.
        is_local_src = s.source in AppState.LOCAL_SOURCES
        if self._playlist_section.visible != (not is_local_src):
            self._playlist_section.visible = not is_local_src
            self._playlist_divider.visible = not is_local_src
            self._playlist_section.update()
            self._playlist_divider.update()

        # Hint del empty state — contextual según origen
        _hint_map = {
            "Archivo Local": "Pulsa «Cargar» para abrir el explorador de archivos.",
            "Pegar Texto":   "Pulsa «Cargar» para pegar tu lista de canciones.",
        }
        new_hint = _hint_map.get(
            s.source,
            "Introduce el ID en el panel izquierdo y pulsa «Cargar».",
        )
        if self._empty_hint_text.value != new_hint:
            self._empty_hint_text.value = new_hint
            self._empty_hint_text.update()

        # Regla 4: La Regla de Hierro — resalte visual del destino.
        # Si el origen es local y el destino no ha sido confirmado explícitamente,
        # el borde del selector de destino se pone en WARNING como demanda visual.
        dest_needs_confirm = is_local_src and not s.destination_confirmed
        new_dst_border = WARNING if dest_needs_confirm else BORDER_LIGHT
        if self._dst_dd.border_color != new_dst_border:
            self._dst_dd.border_color = new_dst_border
            self._dst_dd.focused_border_color = ACCENT if not dest_needs_confirm else WARNING
            self._dst_dd.update()

        # ── Header ────────────────────────────────────────────────────
        self._playlist_title.value = s.playlist_name
        n     = len(s.display_tracks)
        total = len(s.tracks)
        self._track_count.value = (
            f"{n} canciones" if not s.search_query
            else f"{n} de {total} coincidencias"
        )

        _pam = (
            ("YouTube Music", self._auth_yt),
            ("Spotify", self._auth_sp),
            ("Apple Music", self._auth_am),
        )
        for plat, ic in _pam:
            ok = s.auth_session_ok.get(plat, True)
            ic.icon_color = SUCCESS if ok else ERROR_COL
            base = f"{plat}: clic para revalidar ahora"
            hint = s.auth_session_hint.get(plat) or ""
            ic.tooltip = f"{base} · {hint}" if hint else f"{base} · {'OK' if ok else 'fallo'}"

        dest_ok = s.auth_session_ok.get(s.destination, True)
        self._dest_session_warn.visible = not dest_ok
        self._dest_session_warn.value = (
            "" if dest_ok else f"Sesión expirada en {s.destination}"
        )

        # ── Platform badge ─────────────────────────────────────────────
        if s.source == s.destination:
            self._status_badge.value = "⚠ Origen y destino iguales"
            self._status_badge.color = WARNING
        else:
            self._status_badge.value = f"✓ {s.source} → {s.destination}"
            self._status_badge.color = SUCCESS

        self._select_all_chk.value = s.select_all

        # ── Visibility ────────────────────────────────────────────────
        is_loading = s.load_state in (LoadState.LOADING_META, LoadState.LOADING_TRACKS)
        is_ready   = s.load_state == LoadState.READY
        is_error   = s.load_state == LoadState.ERROR
        is_idle    = s.load_state == LoadState.IDLE

        self._empty_state.visible        = is_idle
        self._skeleton_view_wrap.visible = is_loading
        self._list_view_wrap.visible     = is_ready and not is_error
        self._error_state.visible        = is_error

        if is_error:
            self._error_text.value = s.load_error
        if is_loading:
            self._ensure_skeletons_pulsing()
        if is_ready:
            self._stop_skeleton_pulse()
            self._sync_list_view(s.display_tracks)

        # ── Transfer / búsqueda lazy: barra en área de trabajo ────────────
        is_transferring = s.transfer_state == TransferState.RUNNING
        is_transfer_done = s.transfer_state == TransferState.DONE
        is_transfer_err = s.transfer_state == TransferState.ERROR
        xfer_active = is_transferring or is_transfer_done or is_transfer_err
        is_scan_run = getattr(s, "lazy_scan_running", False)
        is_scan_done = getattr(s, "lazy_scan_done", False)
        idle_xfer = s.transfer_state == TransferState.IDLE
        show_progress = xfer_active or is_scan_run or (is_scan_done and idle_xfer)

        if is_transferring:
            if self._transfer_start == 0.0:
                self._transfer_start = time.monotonic()
                self._completion_snack_shown = False
        elif not xfer_active:
            self._transfer_start = 0.0

        self._content_progress.visible = show_progress

        _accent_ok = ft.Colors.GREEN_ACCENT

        if show_progress and s.transfer_total:
            if (is_scan_run or is_scan_done) and idle_xfer and not xfer_active:
                frac = min(1.0, s.transfer_progress / max(s.transfer_total, 1))
                self._content_progress_bar.value = frac
                if is_scan_done:
                    ok_n = sum(
                        1 for t in s.tracks
                        if getattr(t, "transfer_status", "") == "found"
                    )
                    fail_n = sum(
                        1 for t in s.tracks
                        if getattr(t, "transfer_status", "") in ("not_found", "error")
                    )
                    self._content_prog_label.value = (
                        f"Búsqueda finalizada: {ok_n} Éxitos / {fail_n} Fallos"
                    )
                    self._content_prog_label.color = _accent_ok
                    self._content_eta_label.value = ""
                    self._content_progress.border = ft.Border.all(0.9, _accent_ok)
                else:
                    self._content_prog_label.value = f"Búsqueda en destino… {int(frac * 100)}%"
                    self._content_prog_label.color = TEXT_MUTED
                    self._content_eta_label.value = ""
                    self._content_progress.border = ft.Border.all(0.8, BORDER_LIGHT)
            elif xfer_active:
                if s.transfer_state == TransferState.DONE and s.count_detected:
                    frac = s.count_confirmed / s.count_detected
                else:
                    frac = s.transfer_progress / s.transfer_total
                self._content_progress_bar.value = min(1.0, frac)
                fallidas = len(s.failed_tracks)
                rechazadas = len(s.api_rejected_tracks)
                ejec = len(s.transfer_error_tracks)
                porcentaje = int(frac * 100)
                if s.transfer_state == TransferState.DONE:
                    ok_n = s.count_confirmed
                    fail_n = fallidas + rechazadas + ejec
                    self._content_prog_label.value = f"Completado · {porcentaje}%"
                    self._content_prog_label.color = _accent_ok
                    self._content_eta_label.value = ""
                    self._content_progress.border = ft.Border.all(0.9, _accent_ok)
                    if not self._completion_snack_shown:
                        self._completion_snack_shown = True
                        _snack = ft.SnackBar(
                            content=ft.Text(
                                f"Transferencia completada: {ok_n} exitosas, {fail_n} errores",
                                color=ft.Colors.WHITE, font_family="IBM Plex Sans",
                                size=12, opacity=1.0,
                            ),
                            action="Ver Detalles" if fail_n > 0 else None,
                            on_action=(
                                lambda _: self._telemetry.show_postmortem()
                            ) if fail_n > 0 else None,
                            bgcolor=BG_PANEL,
                            duration=6000,
                            behavior=ft.SnackBarBehavior.FLOATING,
                            width=440,
                            show_close_icon=True,
                            close_icon_color=ACCENT,
                        )
                        self.page.overlay.append(_snack)
                        _snack.open = True
                elif s.transfer_state == TransferState.ERROR:
                    self._content_prog_label.value = (
                        f"{porcentaje}%  ·  error · "
                        f"{fallidas + rechazadas} incidencias"
                    )
                    self._content_prog_label.color = WARNING
                    self._content_eta_label.value = ""
                    self._content_progress.border = ft.Border.all(0.8, WARNING)
                else:
                    eta_text = ""
                    if self._transfer_start > 0 and s.transfer_progress > 0:
                        elapsed = time.monotonic() - self._transfer_start
                        remaining = s.transfer_total - s.transfer_progress
                        eta_s = (elapsed / s.transfer_progress) * remaining
                        if 0 < eta_s < 3600:
                            eta_text = (
                                f"~{int(eta_s)}s restantes"
                                if eta_s < 60
                                else f"~{int(eta_s // 60)}m {int(eta_s % 60)}s restantes"
                            )
                    self._content_prog_label.value = (
                        f"{porcentaje}%  ·  "
                        f"{s.count_processed} ok  /  "
                        f"{fallidas + rechazadas} errores"
                    )
                    self._content_prog_label.color = TEXT_MUTED
                    self._content_eta_label.value = eta_text
                    self._content_progress.border = ft.Border.all(0.8, BORDER_LIGHT)

        # ── Telemetry drawer ───────────────────────────────────────────
        if xfer_active:
            self._telemetry.update_counters(
                s.count_detected, s.count_candidates, s.count_processed,
                s.count_confirmed, len(s.api_rejected_tracks),
            )
        self._telemetry.update_log(s.log_lines)
        if s.transfer_state == TransferState.DONE:
            self._telemetry.update_postmortem(
                list(getattr(s, "failed_tracks", []))
                + list(getattr(s, "api_rejected_tracks", []))
                + list(getattr(s, "transfer_error_tracks", [])),
                destination=s.destination,
                confirmed=s.count_confirmed,
                detected=s.count_detected,
            )
        if is_loading and not self._pm_cleared_for_load:
            self._pm_cleared_for_load = True
            self._telemetry.clear_postmortem()
        elif not is_loading:
            self._pm_cleared_for_load = False
        self._telemetry.sync_mode()

        # ── Button states ──────────────────────────────────────────────
        net_blocked = any(cb.is_open for cb in s.cb.values())
        self._load_btn.disabled     = net_blocked or is_loading

        # Regla 4: Transferir bloqueado si fuente local y destino sin confirmar.
        rule4_blocked = is_local_src and not s.destination_confirmed
        self._transfer_btn.disabled = (
            net_blocked or is_transferring or not is_ready or not dest_ok or rule4_blocked
        )
        if rule4_blocked:
            self._transfer_btn.tooltip = "⚠ Elige un destino antes de transferir"
        elif not dest_ok:
            self._transfer_btn.tooltip = f"Sesión expirada en {s.destination}"
        else:
            self._transfer_btn.tooltip = "Transferir selección al destino"

        self.page.update()

    def _sync_list_view(self, tracks: list[Track]) -> None:
        lv           = self._list_view
        existing_ids = {c._track.id for c in lv.controls if hasattr(c, "_track")}
        incoming_ids = {t.id for t in tracks}

        if existing_ids != incoming_ids:
            # Full rebuild: new playlist or search filter changed
            lv.controls.clear()
            self._row_cache.clear()
            for i, track in enumerate(tracks, 1):
                row = SongRow(track, i, self.state.toggle_track)
                self._row_cache[track.id] = row
                lv.controls.append(row)
        else:
            # Incremental refresh via O(1) cache — column # is never touched
            track_map = {t.id: t for t in tracks}
            for tid, row in self._row_cache.items():
                current = track_map.get(tid)
                if current:
                    row.refresh(current)

    def _show_failed_dialog(self) -> None:
        """Post-mortem: solo si hubo failed_traces tras transferencia (gather terminado)."""
        s = self.state
        if s.transfer_state != TransferState.DONE:
            return
        failed = list(getattr(s, "failed_tracks", []))
        if not failed:
            return
        if self._failed_dialog_shown:
            return
        self._failed_dialog_shown = True

        failed_lv = ft.ListView(
            controls=[
                ft.Text(
                    f"{t.name} — {t.artist}",
                    size=11,
                    color=TEXT_PRIMARY,
                    font_family="IBM Plex Sans",
                    opacity=1.0,
                )
                for t in failed
            ],
            spacing=2,
            height=min(380, max(140, len(failed) * 22 + 24)),
            padding=ft.Padding.all(6),
        )

        def _export_report(_: ft.ControlEvent) -> None:
            import datetime as _dt

            log_path = "transfer_failed_report.txt"
            lines = [
                "# MelomaniacPass — Reporte canciones fallidas\n",
                f"# Fecha: {_dt.datetime.now().isoformat()}\n",
                f"# Destino: {s.destination}\n",
                f"# Confirmadas: {s.count_confirmed} / Detectadas: {s.count_detected}\n\n",
            ]
            for t in failed:
                reason = (t.failure_reason or "").strip()
                lines.append(f"{t.name} | {t.artist} | {reason or '—'}\n")
            try:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                self._snack(f"✓ Reporte guardado → {log_path}")
            except OSError as exc:
                self._snack(f"Error al exportar: {exc}", error=True)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.SEARCH_OFF, color=ERROR_COL, size=18),
                ft.Text(
                    f"No encontradas en destino · {len(failed)}",
                    size=13,
                    weight=ft.FontWeight.W_600,
                    color=TEXT_PRIMARY,
                    font_family="IBM Plex Sans",
                    opacity=1.0,
                ),
            ], spacing=8),
            content=ft.Container(
                content=failed_lv,
                width=480,
                bgcolor=BG_SURFACE,
                border_radius=8,
            ),
            actions=[
                ft.TextButton(
                    "Exportar Reporte (.txt)",
                    icon=ft.Icons.DOWNLOAD_OUTLINED,
                    on_click=_export_report,
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT}),
                ),
                ft.TextButton(
                    "Cerrar",
                    on_click=lambda _: self._close_postmortem_dialog(),
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED}),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_PANEL,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        self.page.show_dialog(dlg)

    # ══════════════════════════════════════════════════════════════════
    # EVENT HANDLERS
    # ══════════════════════════════════════════════════════════════════

    # ── Ingestion entry-point ──────────────────────────────────────────

    def _on_load(self, _) -> None:
        """
        Arquitecto de Ingesta — Reglas 1, 2 y 3.
        El enrutamiento depende del origen activo en state.source:
          · Plataforma streaming  → Regla 1: carga directa desde API (campo ID)
          · Archivo Local         → Regla 2: abre el explorador nativo del SO
          · Pegar Texto           → Regla 3: despliega cuadro de diálogo de portapapeles

        ── REGLA 3 — Validación de Enrutamiento (Destino Obligatorio) ───────
        Las fuentes locales no poseen metadatos de plataforma.
        El sistema NO puede "traducir" si no sabe a qué API se dirige.
        La carga queda BLOQUEADA hasta que el usuario confirme un destino.
        """
        src = self.state.source
        # ── Regla 3: bloqueo preventivo de carga para fuentes locales ─────
        if src in AppState.LOCAL_SOURCES and not self.state.destination_confirmed:
            self._snack(
                "⚠ Selecciona primero una plataforma de Destino",
                error=True,
            )
            self._dst_dd.border_color = WARNING
            self._dst_dd.focused_border_color = WARNING
            self._dst_dd.update()
            return

        if src == "Archivo Local":
            asyncio.create_task(self._do_local_pick())
        elif src == "Pegar Texto":
            self._open_paste_dialog()
        else:
            # Regla 1: plataforma streaming seleccionada + campo de texto presente
            asyncio.create_task(self._do_cloud_load())

    async def _do_cloud_load(self, _=None) -> None:
        pid = self._id_field.value.strip()
        if not pid:
            self._snack("Introduce un ID de playlist")
            return
        self._completion_snack_shown = False
        await self.state.load_playlist(pid)

    def _open_load_sheet(self) -> None:
        """Open the bottom-sheet ingestion source picker."""

        def _close(_=None):
            self.page.pop_dialog()

        def _pick_cloud(_):
            self.page.pop_dialog()
            self.state.reset_session()
            self._completion_snack_shown = False
            self._id_field.focus()
            self.page.update()

        def _pick_file(_):
            self.page.pop_dialog()
            asyncio.create_task(self._do_local_pick())

        def _pick_paste(_):
            self.page.pop_dialog()
            self._open_paste_dialog()

        def _option_row(icon, title, subtitle, on_click):
            return ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Icon(icon, color=ACCENT, size=22),
                        bgcolor=ACCENT_HALO,
                        border_radius=10,
                        padding=ft.Padding.all(8),
                        width=44, height=44,
                    ),
                    ft.Column([
                        ft.Text(title, size=13, color=TEXT_PRIMARY,
                                font_family="IBM Plex Sans",
                                weight=ft.FontWeight.W_600, opacity=1.0),
                        ft.Text(subtitle, size=10, color=TEXT_MUTED,
                                font_family="IBM Plex Sans", opacity=1.0),
                    ], spacing=1, tight=True, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=TEXT_DIM, size=16),
                ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding.symmetric(horizontal=20, vertical=12),
                border_radius=10,
                on_click=on_click,
                ink=True,
                bgcolor=ft.Colors.TRANSPARENT,
            )

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column([
                    ft.Container(
                        padding=ft.Padding.only(left=20, top=20, bottom=4),
                        content=ft.Text(
                            "Seleccionar fuente de ingesta",
                            size=14, color=TEXT_PRIMARY,
                            font_family="IBM Plex Sans",
                            weight=ft.FontWeight.W_700, opacity=1.0,
                        ),
                    ),
                    ft.Divider(height=1, color=BORDER_MUTED),
                    _option_row(
                        ft.Icons.CLOUD_SYNC_OUTLINED,
                        "Cloud Sync",
                        "Limpia la sesión y carga desde Spotify / Apple Music / YouTube Music",
                        _pick_cloud,
                    ),
                    ft.Divider(height=1, color=BORDER_MUTED),
                    _option_row(
                        ft.Icons.FOLDER_OPEN_OUTLINED,
                        "Archivo Local",
                        ".txt  .csv  .m3u  .m3u8  .pls  .wpl  .xspf  .xml",
                        _pick_file,
                    ),
                    ft.Divider(height=1, color=BORDER_MUTED),
                    _option_row(
                        ft.Icons.CONTENT_PASTE_OUTLINED,
                        "Pegar Texto",
                        "Pega manualmente el listado de canciones",
                        _pick_paste,
                    ),
                    ft.Container(height=12),
                ], spacing=0, tight=True),
                bgcolor=BG_PANEL,
            ),
            bgcolor=BG_PANEL,
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.show_dialog(sheet)

    def _open_paste_dialog(self) -> None:
        self._paste_field.value = ""

        def _close_paste():
            self.page.pop_dialog()

        def _process(_):
            text = self._paste_field.value or ""
            _close_paste()
            if not text.strip():
                self._snack("El campo de texto está vacío", error=True)
                return
            # ── Regla 1: exigir nombre de playlist antes de procesar una sola línea ──
            import datetime as _dt
            default_ts = _dt.datetime.now().strftime("%H:%M")
            self._ask_playlist_name_then_ingest(
                text=text,
                filename="",
                suggested_name=f"Local_Import_{default_ts}",
            )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Pegar Texto", color=TEXT_PRIMARY,
                          font_family="IBM Plex Sans", size=14,
                          weight=ft.FontWeight.W_700),
            content=ft.Container(
                content=self._paste_field,
                width=480,
                height=220,
            ),
            actions=[
                ft.TextButton(
                    "Procesar",
                    icon=ft.Icons.PLAY_ARROW_OUTLINED,
                    on_click=_process,
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT}),
                ),
                ft.TextButton(
                    "Cancelar",
                    on_click=lambda _: _close_paste(),
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED}),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_PANEL,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        self.page.show_dialog(dlg)

    def _ask_playlist_name_then_ingest(
        self,
        text: str,
        filename: str,
        suggested_name: str,
    ) -> None:
        """
        ══════════════════════════════════════════════════════════════════
        REGLA 1 — Protocolo de Identidad (Nombre de Playlist Obligatorio)
        ══════════════════════════════════════════════════════════════════
        Antes de procesar una sola línea del archivo o del portapapeles,
        el sistema exige (o asigna) un nombre que identifique la ingesta
        en el Monitor de Estado. Ninguna ingesta local puede ser anónima.

        Si el usuario acepta el campo vacío, se usa el nombre sugerido
        (basado en el nombre de archivo o en la marca de tiempo).
        """
        import datetime as _dt

        name_field = ft.TextField(
            value=suggested_name,
            hint_text=f"Ej. {suggested_name}",
            label="Nombre de la Playlist",
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            label_style=ft.TextStyle(color=TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=13, font_family="IBM Plex Sans"),
            bgcolor=BG_INPUT,
            border_color=BORDER_LIGHT,
            focused_border_color=ACCENT,
            border_radius=10,
            autofocus=True,
            on_submit=lambda _: _confirm(None),
        )

        def _close():
            self.page.pop_dialog()

        def _confirm(_):
            raw = (name_field.value or "").strip()
            # Si el usuario dejó el campo vacío, usa el nombre sugerido (con timestamp)
            final_name = raw if raw else (
                suggested_name or
                f"Local_Import_{_dt.datetime.now().strftime('%H:%M')}"
            )
            _close()
            self._ingest_text(text, label=final_name, filename=filename)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.DRIVE_FILE_RENAME_OUTLINE, color=ACCENT, size=18),
                ft.Text(
                    "Nombra esta playlist",
                    size=14, weight=ft.FontWeight.W_700,
                    color=TEXT_PRIMARY, font_family="IBM Plex Sans",
                ),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column([
                    ft.Text(
                        "Asigna un nombre antes de importar. "
                        "Si lo dejas vacío se usará el nombre sugerido.",
                        size=11, color=TEXT_MUTED, font_family="IBM Plex Sans",
                    ),
                    name_field,
                ], spacing=10, tight=True),
                width=400,
                padding=ft.Padding.only(top=6),
            ),
            actions=[
                ft.TextButton(
                    "Importar",
                    icon=ft.Icons.CHECK_OUTLINED,
                    on_click=_confirm,
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT}),
                ),
                ft.TextButton(
                    "Cancelar",
                    on_click=lambda _: _close(),
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED}),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_PANEL,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        self.page.show_dialog(dlg)

    async def _do_local_pick(self) -> None:
        files = await self._file_picker.pick_files(
            dialog_title="Seleccionar playlist",
            allowed_extensions=["txt", "csv", "m3u", "m3u8", "pls", "wpl", "xspf", "xml"],
        )
        if not files:
            return
        f = files[0]
        try:
            with open(f.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            self.state._log(f"[ERROR] No se pudo leer '{f.name}': {exc}")
            self._snack(f"Error leyendo archivo: {exc}", error=True)
            return
        # ── Regla 1: exigir nombre de playlist antes de procesar una sola línea ──
        base_name = os.path.splitext(os.path.basename(f.name))[0] or "Playlist Local"
        self._ask_playlist_name_then_ingest(
            text=text,
            filename=f.name,
            suggested_name=base_name,
        )

    def _ingest_text(self, text: str, label: str = "", filename: str = "") -> None:
        """Parse text → build tracks → load into state."""
        try:
            pairs = parse_local_playlist(text, filename=filename or label)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # parse_local_playlist puede lanzar distintas excepciones según el formato
            self.state._log(f"[ERROR] Parser ingesta: {exc}")
            self._snack(f"Error en el parser: {exc}", error=True)
            return

        if not pairs:
            self.state._log(
                f"[WARN] No se encontraron pistas en '{label or 'texto'}'"
            )
            self._snack("No se reconocieron pistas en el archivo", error=True)
            return

        tracks = build_local_tracks(pairs)
        # label ahora es el nombre definitivo elegido por el usuario (Regla 1).
        # filename es el path original, usado solo por el parser; no lo mangleamos.
        name = label.strip() if label and label.strip() else "Playlist Local"
        self._completion_snack_shown = False
        self._pm_cleared_for_load    = True
        self._telemetry.clear_postmortem()
        self.state.load_local_tracks(tracks, playlist_name=name or "Playlist Local")
        self._snack(f"{len(tracks)} canciones importadas de '{name}'")
        self.state._log(f"[INFO] Ingesta completa · {len(tracks)} pistas desde '{label}'")

    async def _on_transfer(self, _) -> None:
        if self.state.source == self.state.destination:
            self._snack("Origen y destino no pueden ser iguales", error=True)
            return

        # ── Regla 4: La Regla de Hierro ─────────────────────────────────
        # Pistas de fuente local no tienen plataforma nativa.
        # El enrutamiento a ciegas está prohibido: el destino debe ser
        # confirmado explícitamente por el usuario antes de continuar.
        if self.state.source in AppState.LOCAL_SOURCES:
            if not self.state.destination_confirmed:
                self._snack(
                    "⚠ Selecciona una plataforma de destino antes de transferir",
                    error=True,
                )
                # Demanda visual: resalta el selector de destino en WARNING
                self._dst_dd.border_color = WARNING
                self._dst_dd.focused_border_color = WARNING
                self._dst_dd.update()
                return

        if self.state.selected_count == 0:
            self._snack("Selecciona al menos una canción", error=True)
            return
        await self.state.transfer_playlist()

    async def _on_search_change(self, e: ft.ControlEvent) -> None:
        if self._search_task and not self._search_task.done():
            self._search_task.cancel()
        query = e.control.value
        self._search_task = asyncio.create_task(self._do_search(query))

    async def _do_search(self, query: str) -> None:
        await asyncio.sleep(0.30)
        self.state.apply_search(query)

    # ══════════════════════════════════════════════════════════════════
    # SKELETON PULSE
    # ══════════════════════════════════════════════════════════════════

    def _ensure_skeletons_pulsing(self) -> None:
        if self._skeleton_tasks:
            return
        for sk in self._skeletons:
            task = asyncio.create_task(sk.start_pulse())
            self._skeleton_tasks.append(task)

    def _stop_skeleton_pulse(self) -> None:
        for task in self._skeleton_tasks:
            task.cancel()
        self._skeleton_tasks.clear()
        for sk in self._skeletons:
            sk.stop_pulse()

    # ══════════════════════════════════════════════════════════════════
    # CIRCUIT BREAKER REACTIONS
    # ══════════════════════════════════════════════════════════════════

    def _on_circuit_change(self, platform: str, is_open: bool, remaining: int) -> None:
        banner_text = self._rl_banner.content.controls[1]
        self._rl_banner.visible = is_open
        if is_open:
            banner_text.value = f"Rate limit en {platform} · {remaining}s"
            asyncio.create_task(self._countdown(platform, remaining))
        else:
            banner_text.value = ""
        self._rl_banner.update()

    async def _countdown(self, platform: str, seconds: int) -> None:
        banner_text = self._rl_banner.content.controls[1]
        for rem in range(seconds, 0, -1):
            banner_text.value = f"Rate limit en {platform} · {rem}s"
            try:
                banner_text.update()
            except Exception:  # pylint: disable=broad-exception-caught
                break  # El control puede estar desmontado si el usuario navega
            await asyncio.sleep(1)

    # ══════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════

    def _snack(self, msg: str, error: bool = False) -> None:
        snack = ft.SnackBar(
            content=ft.Text(msg, color=ft.Colors.WHITE,
                            font_family="IBM Plex Sans", size=12, opacity=1.0),
            bgcolor=ERROR_COL if error else BG_PANEL,
            duration=3000,
            behavior=ft.SnackBarBehavior.FLOATING,
            width=380,
            show_close_icon=True,
            close_icon_color=ACCENT,
        )
        self.page.overlay.append(snack)
        snack.open = True
        self.page.update()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §11  ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main(page: ft.Page) -> None:
    try:
        page.title          = "MelomaniacPass"
        page.window.bgcolor = BG_LIST
        page.bgcolor        = BG_LIST
        page.window.width   = 1200
        page.window.height  = 650
        page.window.min_width  = 1030
        page.window.min_height = 600
        page.padding        = 0
        page.spacing        = 0
        page.theme_mode     = ft.ThemeMode.DARK

        def _exception_handler(loop, context: dict) -> None:
            exc = context.get("exception")
            if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                return
            msg = context.get("message", "")
            if "_call_connection_lost" in msg or "shutdown" in msg:
                return
            loop.default_exception_handler(context)

        asyncio.get_event_loop().set_exception_handler(_exception_handler)

        page.fonts = {
            "IBM Plex Sans": (
                "https://fonts.gstatic.com/s/ibmplexsans/v19/"
                "zYXgKVElMYYaJe8bpLHnCwDKjR7_MIZs.woff2"
            ),
            "IBM Plex Sans JP": (
                "https://fonts.gstatic.com/s/ibmplexsansjp/v5/"
                "Z9XLDn9KbTDf6_f7dISNqYf_-aYNey-sDg.woff2"
            ),
        }
        page.theme = ft.Theme(
            font_family="IBM Plex Sans",
            color_scheme=ft.ColorScheme(
                primary=ACCENT,
                surface=BG_SURFACE,
                on_primary=TEXT_PRIMARY,
                on_surface=TEXT_PRIMARY,
            ),
        )

        circuit_breakers = {p: CircuitBreaker(p) for p in AppState.PLATFORMS}
        service      = MusicApiService(circuit_breakers)
        state        = AppState(service)
        ui           = PlaylistManagerUI(page, state)
        auth_manager = AuthManager(page, service, state)

        # Expose auth_manager to UI so the settings button can open the wizard
        ui.auth_manager = auth_manager
        service.auth_manager = auth_manager

        page.add(ui.root)

        async def _startup() -> None:
            await auth_manager.run_startup_check()
            state.notify()

        asyncio.create_task(_startup())

        async def _auth_poll_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(90)
                    await auth_manager.refresh_session_icons()
            except asyncio.CancelledError:
                raise  # pylint: disable=try-except-raise

        ui._auth_poll_task = asyncio.create_task(_auth_poll_loop())

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # §6  PROTOCOLO DE EXTERMINIO (HARD EXIT)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        shutdown_event = asyncio.Event()
        _shutdown_once = [False]

        # Pasamos explícitamente la instancia (app) para evitar líos de scope
        async def hard_cleanup(app_inst, state_inst, auth_inst, service_inst) -> None:
            if _shutdown_once[0]:
                shutdown_event.set()
                return
            _shutdown_once[0] = True
            
            try:
                app_inst._stop_skeleton_pulse()
                if app_inst._search_task and not app_inst._search_task.done():
                    app_inst._search_task.cancel()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # Durante el shutdown cualquier control Flet puede estar desmontado

            try:
                state_inst.cancel_lazy_scan()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # ídem

            # Cancelar tareas de auth
            auth_reload = getattr(auth_inst, "_reload_task", None)
            if auth_reload and not auth_reload.done():
                auth_reload.cancel()

            # Barrido de tareas (Filtro por nombre para no matar el cleanup)
            current = asyncio.current_task()
            for task in asyncio.all_tasks():
                if task is current or task.done(): continue
                try:
                    name = task.get_name() # Más fiable en Python 3.8+
                    if "hard_cleanup" in name or "main" in name: continue
                except AttributeError:
                    pass  # task.get_name() no disponible en Python < 3.8
                task.cancel()

            await asyncio.sleep(0.05)
            import gc
            gc.collect()

            # Limpieza de hilos de red
            if hasattr(service_inst, "_cleanup_sessions"):
                await asyncio.to_thread(service_inst._cleanup_sessions)
            
            shutdown_event.set()

        # Al llamar a la tarea, le pasamos "la mercancía" (self y sus hijos)
        def _on_close(_) -> None:
            asyncio.create_task(hard_cleanup(ui, state, auth_manager, service))

        page.on_close = _on_close

        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await page.window.destroy()
        except Exception:  # pylint: disable=broad-exception-caught
            os._exit(0)



if __name__ == "__main__":
    try:
        ft.run(main)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass