"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║              Servicio Unificado de APIs Musicales                    ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: services/api_service.py
Descripción: Fachada unificada asíncrona sobre las APIs de Spotify,
            YouTube Music y Apple Music. Abstrae las diferencias entre
            plataformas proporcionando una interfaz consistente.

Estrategia de Diseño - Patrón Facade:
    MusicApiService actúa como punto único de acceso a múltiples APIs
    externas, ocultando su complejidad y diferencias:
    
    1. Abstracción de Plataformas:
       - Interfaz unificada para búsqueda, carga y transferencia
       - Normalización de respuestas a modelos comunes (Track, SearchResult)
       - Manejo consistente de errores entre plataformas
    
    2. Gestión de Autenticación:
       - Spotify: OAuth 2.0 con spotipy
       - YouTube Music: Headers de sesión (Cookie + Authorization)
       - Apple Music: Bearer token + User token
    
    3. Resiliencia y Rate Limiting:
       - Circuit breakers por plataforma
       - Reintentos con backoff exponencial
       - Semáforo global para limitar concurrencia
       - Detección de 401/429 con mensajes específicos
    
    4. Optimizaciones:
       - Caché de búsquedas para evitar peticiones duplicadas
       - Sesiones HTTP reutilizables
       - Búsquedas concurrentes con límite de semáforo
    
    5. Sistema Hunter Recovery:
       - Búsqueda con fallback a queries alternativos
       - Matching fuzzy con umbrales adaptativos
       - Selección inteligente de mejor resultado

Constantes:
    - SPOTIFY_REQUIRED_SCOPES: Permisos OAuth necesarios
    - NETWORK_CONCURRENCY: Límite de peticiones concurrentes (5)
    - RATE_LIMIT_BACKOFF_STEPS: Reintentos ante rate limiting (10)

Funciones Auxiliares:
    - _is_spotify_rate_limited: Detecta HTTP 429 de Spotify
    - _spotify_retry_after: Extrae tiempo de espera del header
    - _is_ytm_unauthorized: Detecta HTTP 401 de YouTube Music

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from typing import Callable, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from auth_manager import BROWSER_JSON
from core.models import Track, SearchResult
from utils.circuit_breaker import CircuitBreaker, RateLimitError, SpotifyBanException
from engine.normalizer import (
    clean_metadata, build_search_query, _normalize_title, FUZZY_IDEAL,
)
from engine.match import (
    _fuzzy_scores_triple, _fuzzy_flags_elastic, _ideal_pass_hunter,
    _joji_trikeyword_query, _duration_to_seconds,
    validar_match, _yt_select_best, score_spotify_match,
)

load_dotenv()

# ══════════════════════════════════════════════════════════════════════
# DETECCIÓN DE LIBRERÍAS OPCIONALES
# ══════════════════════════════════════════════════════════════════════

try:
    import spotipy
    from spotipy.exceptions import SpotifyException
    HAS_SPOTIFY = True
except ImportError:
    HAS_SPOTIFY = False
    SpotifyException = None  # type: ignore

try:
    from ytmusicapi import YTMusic
    HAS_YTMUSIC = True
except ImportError:
    HAS_YTMUSIC = False

# ══════════════════════════════════════════════════════════════════════
# CONSTANTES DE CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════

# Scopes OAuth requeridos para Spotify
SPOTIFY_REQUIRED_SCOPES = (
    "playlist-modify-public playlist-modify-private user-library-read"
)

# Ruta del archivo de caché de tokens de Spotify
SPOTIFY_CACHE_PATH = ".spotify_cache"

# Límite de peticiones HTTP concurrentes para evitar sobrecarga
NETWORK_CONCURRENCY = 2

# Número de reintentos ante rate limiting (HTTP 429)
RATE_LIMIT_BACKOFF_STEPS = 10

# Semáforo global para limitar concurrencia de peticiones
GLOBAL_API_SEMAPHORE = asyncio.Semaphore(NETWORK_CONCURRENCY)


class SpotifyRateLimiter:
    """
    Rate limiter de ventana deslizante para peticiones de búsqueda a Spotify.

    Objetivo principal: prevenir activamente que se reciba un HTTP 429
    mediante tres capas de protección escalonadas:

      Nivel 1 — Pacemaker: mínimo 0.5s + micro-jitter 0.1-0.2s entre
                peticiones (~1.5 req/s). Absorbe la latencia de red
                calculando el tiempo real transcurrido desde el último call.

      Nivel 2 — Sliding Window: ventana de 30s con límite de 28 requests.
                Si se alcanza el límite, pausa 20s forzados para dejar que
                el token bucket de Spotify se recargue al 100%.

      Nivel 3 — Kill Switch (emergencia): si Spotify devuelve un 429 a
                pesar de los niveles anteriores, trip() registra el ban y
                acquire() lanza SpotifyBanException inmediatamente para
                todas las tareas en cola, abortando la transferencia de
                forma limpia en lugar de esperar horas bloqueado.
    """

    WINDOW_S     = 30
    WINDOW_LIMIT = 28
    WINDOW_SLEEP = 20.0
    PACE_MIN     = 0.5
    JITTER_MIN   = 0.1
    JITTER_MAX   = 0.2

    def __init__(self) -> None:
        self._lock        = asyncio.Lock()
        self._timestamps: deque[float] = deque()
        self._last        = 0.0
        self._unblock_at  = 0.0

    def trip(self, retry_after: float) -> None:
        """Registra un 429: acquire() lanzará SpotifyBanException hasta que expire."""
        self._unblock_at = time.monotonic() + retry_after

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()

            # ── Nivel 3: Kill Switch ──────────────────────────────────────
            if now < self._unblock_at:
                raise SpotifyBanException(self._unblock_at - now)

            # ── Nivel 2: Sliding Window ───────────────────────────────────
            while self._timestamps and now - self._timestamps[0] > self.WINDOW_S:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.WINDOW_LIMIT:
                await asyncio.sleep(self.WINDOW_SLEEP)
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > self.WINDOW_S:
                    self._timestamps.popleft()

            # ── Nivel 1: Pacemaker + micro-jitter ────────────────────────
            elapsed = now - self._last
            wait    = (self.PACE_MIN - elapsed) + random.uniform(self.JITTER_MIN, self.JITTER_MAX)
            if wait > 0:
                await asyncio.sleep(wait)

            # ── Registrar timestamp ───────────────────────────────────────
            now = time.monotonic()
            self._timestamps.append(now)
            self._last = now


_SP_LIMITER = SpotifyRateLimiter()

# Mensaje de error para sesión expirada de YouTube Music
_YTM_401_MSG = (
    "[ERROR] YouTube Music: la sesion de browser.json ha expirado (401). "
    "Renueva Cookie + Authorization desde el navegador."
)


def _is_spotify_rate_limited(exc: BaseException) -> bool:
    """
    Detecta si una excepción es un rate limit (HTTP 429) de Spotify.
    
    Args:
        exc: Excepción capturada durante llamada a API de Spotify.
    
    Returns:
        True si es un HTTP 429, False en caso contrario.
    
    Note:
        Requiere que spotipy esté instalado. Si no lo está, retorna False.
    """
    if SpotifyException is None:
        return False
    return isinstance(exc, SpotifyException) and getattr(exc, "http_status", None) == 429


def _spotify_retry_after(exc: BaseException, default: int = 30) -> int:
    """
    Extrae el tiempo de espera del header Retry-After de Spotify.
    
    Args:
        exc: Excepción SpotifyException con headers.
        default: Tiempo por defecto si el header no está presente.
    
    Returns:
        Segundos a esperar antes de reintentar (mínimo 1).
    
    Note:
        El header Retry-After es estándar HTTP y indica cuándo se puede
        reintentar la petición sin ser bloqueado nuevamente.
    """
    headers = getattr(exc, "headers", None) or {}
    try:
        return max(1, int(headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


def _is_ytm_unauthorized(exc: BaseException) -> bool:
    """
    Detecta si una excepción es un error de autenticación (HTTP 401) de YouTube Music.
    
    Verifica múltiples indicadores de 401:
    - String "401" en el mensaje de error
    - String "unauthorized" en el mensaje
    - Atributo response.status_code == 401
    
    Args:
        exc: Excepción capturada durante llamada a API de YouTube Music.
    
    Returns:
        True si es un HTTP 401, False en caso contrario.
    
    Note:
        Un 401 indica que los headers de browser.json han expirado y
        necesitan ser renovados desde el navegador.
    """
    s = str(exc).lower()
    if "401" in str(exc) or "status code: 401" in s or "unauthorized" in s:
        return True
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 401


class MusicApiService:
    """
    Fachada unificada asíncrona sobre APIs de plataformas de streaming.
    
    Proporciona una interfaz consistente para interactuar con Spotify,
    YouTube Music y Apple Music, ocultando las diferencias de sus APIs
    y manejando autenticación, rate limiting y errores de forma unificada.
    
    Attributes:
        _cb: Diccionario de circuit breakers por plataforma.
        _sp: Cliente de Spotify (spotipy.Spotify).
        _ytm: Cliente de YouTube Music (YTMusic).
        _am_headers: Headers para peticiones a Apple Music.
        _am_storefront: Código de país para Apple Music (default: "us").
        _search_cache: Caché de resultados de búsqueda.
        auth_manager: Referencia a AuthManager (inyectada externamente).
    
    Methods:
        init_spotify: Inicializa cliente de Spotify con OAuth.
        init_youtube: Inicializa cliente de YouTube Music con headers.
        init_apple: Inicializa headers de Apple Music.
        search_with_fallback: Búsqueda con fallback a queries alternativos.
        load_playlist: Carga playlist desde una plataforma.
        add_to_playlist: Agrega canción a playlist destino.
        cleanup_sessions: Limpia sesiones HTTP al cerrar.
    
    Example:
        >>> service = MusicApiService(circuit_breakers)
        >>> await service.init_spotify(auth_manager)
        >>> result = await service.search_with_fallback(
        ...     "Spotify", "Bohemian Rhapsody", "Queen"
        ... )
    
    Note:
        Este servicio es stateful: mantiene clientes autenticados y
        sesiones HTTP. Debe llamarse cleanup_sessions() al cerrar la
        aplicación para liberar recursos correctamente.
    """

    def __init__(self, circuit_breakers: dict[str, CircuitBreaker]):
        """
        Inicializa el servicio con circuit breakers para cada plataforma.
        
        Args:
            circuit_breakers: Diccionario {plataforma: CircuitBreaker}.
        
        Note:
            Los clientes de plataformas (_sp, _ytm) se inicializan bajo
            demanda cuando se necesitan, no en el constructor.
        """
        self._cb  = circuit_breakers
        self._sp  = None
        self._ytm = None
        self._am_headers:    dict = {}
        self._am_storefront: str  = "us"
        self._search_cache: dict[str, SearchResult] = {}
        self._shutdown_cleaned: bool = False
        self.youtube_auth_error: str = ""
        self.auth_manager = None

        # ──────────────────────────────────────────────────────────────
        # SESIONES HTTP REUTILIZABLES
        # ──────────────────────────────────────────────────────────────
        # Mantener sesiones abiertas mejora performance al reutilizar
        # conexiones TCP y evitar handshakes SSL repetidos
        
        _am_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        )
        self._http_session = requests.Session()
        self._http_session.headers.update({"User-Agent": _am_ua})
        self._yt_http_session = requests.Session()

    # ══════════════════════════════════════════════════════════════════
    # GESTIÓN DE SESIONES
    # ══════════════════════════════════════════════════════════════════

    def _cleanup_sessions(self) -> None:
        if getattr(self, "_shutdown_cleaned", False):
            return
        self._shutdown_cleaned = True
        for sess in (self._http_session, self._yt_http_session):
            try:
                sess.close()
            except OSError:
                pass
        try:
            if self._sp and hasattr(self._sp, '_session'):
                self._sp._session.close()  # pylint: disable=protected-access
        except OSError:
            pass
        self._sp = None
        self._ytm = None
        self._am_headers = {}

    def cleanup_sessions(self) -> None:
        self._cleanup_sessions()

    @property
    def search_cache(self) -> dict:
        return self._search_cache

    # ── Spotify Auth ───────────────────────────────────────────────────

    async def init_spotify(self) -> bool:
        return await asyncio.to_thread(self._sync_init_spotify)

    def _sync_init_spotify(self) -> bool:
        if not HAS_SPOTIFY:
            return False
        try:
            from spotipy.oauth2 import SpotifyOAuth          # pylint: disable=import-outside-toplevel
            from cache_handler import CacheFileHandler        # pylint: disable=import-outside-toplevel

            client_id     = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
            client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
            redirect_uri  = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip()

            if not client_id or not client_secret:
                return False

            cache_handler = CacheFileHandler(cache_path=SPOTIFY_CACHE_PATH)
            oauth = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope=SPOTIFY_REQUIRED_SCOPES,
                cache_handler=cache_handler,
                open_browser=False,
            )
            token_info = cache_handler.get_cached_token()
            if not token_info:
                self._sp_oauth = oauth
                self._sp = None
                return False

            self._sp       = spotipy.Spotify(auth_manager=oauth)
            self._sp_oauth = oauth
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"[Spotify] init failed: {exc}")
            return False

    def _safe_sp_call(self, method_name: str, *args, **kwargs):
        try:
            return getattr(self._sp, method_name)(*args, **kwargs)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if HAS_SPOTIFY and SpotifyException is not None and isinstance(exc, SpotifyException):
                hs = getattr(exc, "http_status", None)
                if hs == 429:
                    raise RateLimitError("Spotify", _spotify_retry_after(exc)) from exc
                if hs == 401:
                    if self._sync_init_spotify() and self._sp:
                        return getattr(self._sp, method_name)(*args, **kwargs)
            raise

    def get_spotify_auth_url(self) -> Optional[str]:
        if not HAS_SPOTIFY:
            return None
        if not hasattr(self, "_sp_oauth") or self._sp_oauth is None:
            self._sync_init_spotify()
        oauth = getattr(self, "_sp_oauth", None)
        if oauth is None:
            return None
        try:
            return oauth.get_authorize_url()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"[Spotify] get_authorize_url failed: {exc}")
            return None

    async def handle_spotify_redirect(self, redirect_response: str) -> bool:
        if not HAS_SPOTIFY:
            return False
        oauth = getattr(self, "_sp_oauth", None)
        if oauth is None:
            return False
        try:
            code = await asyncio.to_thread(oauth.parse_response_code, redirect_response)
            if not code or code == redirect_response:
                return False
            token_info = await asyncio.to_thread(
                oauth.get_access_token, code, as_dict=True, check_cache=False
            )
            if token_info:
                self._sp = spotipy.Spotify(auth_manager=oauth)
                return True
            return False
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"[Spotify] handle_redirect failed: {exc}")
            return False

    # ── YouTube Music Auth ─────────────────────────────────────────────

    async def init_youtube(self) -> bool:
        return await asyncio.to_thread(self._sync_init_youtube)

    def _sync_init_youtube(self) -> bool:
        if not HAS_YTMUSIC:
            return False
        if not BROWSER_JSON.exists():
            self.youtube_auth_error = "missing browser.json"
            return False
        self.youtube_auth_error = ""
        try:
            self._ytm = YTMusic(auth=str(BROWSER_JSON), requests_session=self._yt_http_session)
            self._ytm.get_library_playlists(limit=1)
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.youtube_auth_error = str(exc)
            if _is_ytm_unauthorized(exc):
                print(_YTM_401_MSG)
            else:
                print(f"[YouTube Music] init failed: {exc}")
            self._ytm = None
            return False

    # ── Apple Music Auth ───────────────────────────────────────────────

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
                self._am_headers    = am_headers
                self._http_session.headers.update(am_headers)
                self._am_storefront = resp.json().get("data", [{}])[0].get("id", "us")
                return True
            return False
        except Exception as exc:  # pylint: disable=broad-exception-caught
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
        raise ValueError(f"Unknown platform: {platform}")

    def _spotify_playlist_items_to_tracks(self, raw: list, name: str, cb) -> list[Track]:
        tracks: list[Track] = []
        total = len(raw)
        for i, item in enumerate(raw, 1):
            t = item.get("track")
            if not t or not t.get("id"):
                continue
            ms  = t["duration_ms"]
            dur = f"{int(ms/60000)}:{int((ms/1000)%60):02d}"
            imgs = t.get("album", {}).get("images", [])
            tracks.append(Track(
                id=t["id"], name=t["name"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                album=t.get("album", {}).get("name", ""),
                duration=dur,
                img_url=imgs[-1]["url"] if imgs else "",
                platform="Spotify",
                duration_ms=ms,
                is_explicit=t.get("explicit", False),
            ))
            if cb and i % 50 == 0:
                cb(i, total, name)
        return tracks

    async def _async_fetch_spotify(self, pid: str, cb) -> tuple[str, list[Track]]:
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
                    if _is_spotify_rate_limited(e):
                        wait = _spotify_retry_after(e)
                        self._cb["Spotify"].trip(wait)
                        raise RateLimitError("Spotify", wait) from e
                    raise

        info   = await _one_call(sp.playlist, pid, fields="name")
        name   = info.get("name", "Spotify Playlist")
        result = await _one_call(sp.playlist_tracks, pid)
        raw    = list(result["items"])
        while result.get("next"):
            result = await _one_call(sp.next, result)
            raw.extend(result["items"])
        return name, self._spotify_playlist_items_to_tracks(raw, name, cb)

    def _sync_fetch_youtube(self, pid: str, cb) -> tuple[str, list[Track]]:
        if not self._ytm:
            self._sync_init_youtube()
        if not self._ytm:
            raise RuntimeError("YouTube Music no disponible. Comprueba browser.json.")
        try:
            pl = self._ytm.get_playlist(pid, limit=None)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.youtube_auth_error = str(exc)
            if _is_ytm_unauthorized(exc):
                raise RuntimeError("Sesion YouTube Music expirada (401). Renueva browser.json.") from exc
            raise
        name  = pl.get("title", "YouTube Playlist")
        raw   = pl.get("tracks", [])
        total = len(raw)
        tracks = []
        for i, t in enumerate(raw, 1):
            thumbs = t.get("thumbnails", [])
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
        base     = "https://amp-api.music.apple.com/v1"
        is_lib   = pid.startswith("p.")
        info_url = (
            f"{base}/me/library/playlists/{pid}" if is_lib
            else f"{base}/catalog/{self._am_storefront}/playlists/{pid}"
        )
        name = "Apple Music Playlist"
        try:
            r = self._http_session.get(info_url, timeout=10)
            if r.status_code == 429:
                raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
            if r.ok:
                name = r.json()["data"][0]["attributes"].get("name", name)
        except RateLimitError:
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            pass

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


    # ── Search ─────────────────────────────────────────────────────────

    async def search_track(
        self,
        platform: str,
        name: str,
        artist: str,
        local_duration_s: Optional[int] = None,
        local_duration_ms: int = 0,
        local_is_explicit: bool = False,
    ) -> SearchResult:
        ct, ca = clean_metadata(name, artist)
        self._cb[platform].check_or_raise()
        await asyncio.sleep(random.uniform(0.5, 1.5))
        if platform == "YouTube Music":
            return await self._yt_hunter_async(ct, ca, name, artist, local_duration_s)
        if platform == "Apple Music":
            return await self._am_hunter_async(ct, ca, name, artist)
        if platform == "Spotify":
            return await self._sp_hunter_async(ct, ca, name, artist, local_duration_ms, local_is_explicit)
        return SearchResult(None, False)

    async def search_with_fallback(
        self,
        platform: str,
        name: str,
        artist: str,
        local_duration_s: Optional[int] = None,
        local_duration_ms: int = 0,
        local_is_explicit: bool = False,
    ) -> SearchResult:
        base_t, base_a = clean_metadata(name, artist)
        passes = [
            (base_t, base_a),
            (name.strip(), artist.strip()),
            (_normalize_title(name), base_a),
        ]
        seen: set[tuple[str, str]] = set()
        for idx, (t_pass, a_pass) in enumerate(passes):
            t_pass = t_pass.strip()
            if not t_pass:
                continue
            key = (t_pass.lower(), a_pass.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            result = await self.search_track(
                platform, t_pass, a_pass, local_duration_s,
                local_duration_ms=local_duration_ms,
                local_is_explicit=local_is_explicit,
            )
            if result.track_id:
                if idx == 0 and not result.needs_review and not result.low_confidence:
                    return result
                return result
        return SearchResult(None, False)

    # ── YouTube Music Hunter ───────────────────────────────────────────

    def _yt_pack_result(self, chosen: dict, orig_name: str, orig_artist: str) -> SearchResult:
        found_title = chosen.get("title", "")
        farts = ", ".join(
            a.get("name", "") for a in (chosen.get("artists") or []) if isinstance(a, dict)
        )
        comb, tit, art = _fuzzy_scores_triple(orig_name, orig_artist, found_title, farts)
        needs, low = _fuzzy_flags_elastic(comb, tit, art)
        return SearchResult(chosen.get("videoId"), needs, low_confidence=low)

    def _yt_sync_search_round(self, query, orig_name, orig_artist, local_duration_s, cached_results=None):
        results = cached_results if cached_results is not None else self._ytm.search(query, filter="songs", limit=8)
        if not results:
            return None
        vid = _yt_select_best(orig_name, orig_artist, results, local_duration_s)
        if not vid:
            return None
        chosen = next(
            (r for r in results[:8] if r.get("videoId") == vid and validar_match(orig_name, orig_artist, r, local_duration_s)),
            next((r for r in results if r.get("videoId") == vid), None)
        )
        if not chosen:
            return None
        found_title = chosen.get("title", "")
        farts = ", ".join(a.get("name", "") for a in (chosen.get("artists") or []) if isinstance(a, dict))
        comb, tit, art = _fuzzy_scores_triple(orig_name, orig_artist, found_title, farts)
        return chosen, comb, tit, art

    def _yt_search_songs_sync(self, query: str) -> list:
        if not self._ytm:
            return []
        r = self._ytm.search(query, filter="songs", limit=8)
        return list(r) if r else []

    async def _yt_hunter_async(self, ct, ca, orig_name, orig_artist, local_duration_s) -> SearchResult:
        if not self._ytm:
            return SearchResult(None, False)
        nt = _normalize_title(orig_name)
        na = _normalize_title(orig_artist)
        strict_q: list[str] = []
        for q in (build_search_query(ct, ca), build_search_query(nt, na), nt or ct):
            q = (q or "").strip()
            if q and q not in strict_q:
                strict_q.append(q)
        raw_q: list[str] = []
        for q in (build_search_query(orig_name.strip(), orig_artist.strip()), _joji_trikeyword_query(orig_name, orig_artist)):
            q = (q or "").strip()
            if q and q not in raw_q and q not in strict_q:
                raw_q.append(q)

        def _process_pack(pack):
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
                    results = await asyncio.to_thread(self._yt_search_songs_sync, query)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    self.youtube_auth_error = str(exc)
                    if _is_ytm_unauthorized(exc):
                        raise RuntimeError("Sesion YouTube Music expirada (401).") from exc
                    raise
            if results:
                strict_empty_api = False
            if not results:
                continue
            pack = await asyncio.to_thread(self._yt_sync_search_round, query, orig_name, orig_artist, local_duration_s, results)
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
                        results = await asyncio.to_thread(self._yt_search_songs_sync, query)
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        self.youtube_auth_error = str(exc)
                        if _is_ytm_unauthorized(exc):
                            raise RuntimeError("Sesion YouTube Music expirada (401).") from exc
                        raise
                if not results:
                    continue
                pack = await asyncio.to_thread(self._yt_sync_search_round, query, orig_name, orig_artist, local_duration_s, results)
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


    # ── Apple Music Hunter ─────────────────────────────────────────────

    def _am_candidates_for_term(self, term: str) -> list[tuple[str, str]]:
        q   = quote(term)
        url = (
            f"https://api.music.apple.com/v1/catalog/{self._am_storefront}"
            f"/search?types=songs&term={q}&limit=5"
        )
        r = self._http_session.get(url, timeout=10)
        if r.status_code == 429:
            raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
        songs = r.json().get("results", {}).get("songs", {}).get("data", [])
        return [
            (f"{s['attributes'].get('name','')} - {s['attributes'].get('artistName','')}", s["id"])
            for s in songs
        ]

    def _am_pick_catalog_best(self, song_title, artist_name, candidates) -> SearchResult:
        if not candidates:
            return SearchResult(None, False)
        try:
            from rapidfuzz import fuzz as _fuzz  # pylint: disable=import-outside-toplevel
            ct, ca = clean_metadata(song_title, artist_name)
            ref = f"{ct} {ca}".lower()
            best_id, best_score, best_cand = None, -1, ""
            for cand_str, tid in candidates:
                sc = int(_fuzz.token_sort_ratio(ref, cand_str.lower()))
                if sc > best_score:
                    best_score, best_id, best_cand = sc, tid, cand_str
        except ImportError:
            best_id   = candidates[0][1]
            best_cand = candidates[0][0]
        if not best_id:
            return SearchResult(None, False)
        parts = best_cand.split(" - ", 1)
        found_t, fa = parts[0], (parts[1] if len(parts) > 1 else "")
        comb, tit, art = _fuzzy_scores_triple(song_title, artist_name, found_t, fa)
        needs, low = _fuzzy_flags_elastic(comb, tit, art)
        return SearchResult(best_id, needs, low_confidence=low)

    async def _am_hunter_async(self, ct, ca, orig_name, orig_artist) -> SearchResult:
        terms: list[str] = []
        for t in (
            build_search_query(ct, ca),
            build_search_query(_normalize_title(orig_name), _normalize_title(orig_artist)),
            _normalize_title(orig_name),
        ):
            t = t.strip()
            if t and t not in terms:
                terms.append(t)
        merged: list[tuple[str, str]] = []
        seen: set[str] = set()
        for term in terms:
            async with GLOBAL_API_SEMAPHORE:
                chunk = await asyncio.to_thread(self._am_candidates_for_term, term)
            for c in chunk:
                if c[1] not in seen:
                    seen.add(c[1])
                    merged.append(c)
        return self._am_pick_catalog_best(orig_name, orig_artist, merged)

    # ── Spotify Hunter ─────────────────────────────────────────────────

    def _sp_search_items(self, q: str) -> list:
        r = self._safe_sp_call("search", q=q, type="track", limit=10)
        if r is None:
            return []
        return r.get("tracks", {}).get("items", [])

    def _sp_pick_best_item(self, items, orig_name, orig_artist, local_duration_ms=0, local_is_explicit=False):
        if not items:
            return None, 0, 0, 0
        scored = []
        for t in items:
            found_title = t.get("name", "")
            fa = ", ".join(a["name"] for a in t.get("artists", []))
            sp_dur_ms   = t.get("duration_ms", 0)
            sp_explicit = t.get("explicit", False)
            sc = score_spotify_match(
                orig_name, orig_artist, local_duration_ms, local_is_explicit,
                found_title, fa, sp_dur_ms, sp_explicit,
            )
            comb, tit, art = _fuzzy_scores_triple(orig_name, orig_artist, found_title, fa)
            scored.append((t, sc, comb, tit, art))
        scored.sort(key=lambda x: -x[1])
        best_t, _sc, comb, tit, art = scored[0]
        return best_t, comb, tit, art

    def _build_spotify_result(self, t, comb, tit, art) -> SearchResult:
        needs, low = _fuzzy_flags_elastic(comb, tit, art)
        isrc: Optional[str] = (t.get("external_ids") or {}).get("isrc")
        return SearchResult(t["id"], needs, low_confidence=low, isrc=isrc)

    async def _sp_hunter_async(self, ct, ca, orig_name, orig_artist, local_duration_ms=0, local_is_explicit=False) -> SearchResult:
        if not self._sp:
            await asyncio.to_thread(self._sync_init_spotify)
        if not self._sp:
            return SearchResult(None, False)

        nt = _normalize_title(orig_name)
        na = _normalize_title(orig_artist)
        queries_structured = [
            f"track:{ct} artist:{ca}",
            f"track:{nt} artist:{na}" if na else f"track:{nt}",
        ]
        best_match = None
        best_comb  = -1

        for q in queries_structured:
            if not q:
                continue
            await _SP_LIMITER.acquire()
            async with GLOBAL_API_SEMAPHORE:
                try:
                    items = await asyncio.to_thread(self._sp_search_items, q)
                except RateLimitError:
                    raise
                except Exception as e:  # pylint: disable=broad-exception-caught
                    if _is_spotify_rate_limited(e):
                        wait = _spotify_retry_after(e)
                        self._cb["Spotify"].trip(wait)
                        _SP_LIMITER.trip(wait)
                        raise SpotifyBanException(wait) from e
                    raise
            if not items:
                continue
            picked, comb, tit, art = self._sp_pick_best_item(items, orig_name, orig_artist, local_duration_ms, local_is_explicit)
            if picked:
                if comb >= FUZZY_IDEAL or _ideal_pass_hunter(comb, tit, art):
                    return self._build_spotify_result(picked, comb, tit, art)
                if comb > best_comb:
                    best_comb  = comb
                    best_match = (picked, comb, tit, art)

        if best_comb < 60:
            query_plain = build_search_query(ct, ca)
            await _SP_LIMITER.acquire()
            async with GLOBAL_API_SEMAPHORE:
                try:
                    items = await asyncio.to_thread(self._sp_search_items, query_plain)
                except RateLimitError:
                    raise
                except Exception as e:  # pylint: disable=broad-exception-caught
                    if _is_spotify_rate_limited(e):
                        wait = _spotify_retry_after(e)
                        self._cb["Spotify"].trip(wait)
                        _SP_LIMITER.trip(wait)
                        raise SpotifyBanException(wait) from e
                    raise
            if items:
                picked, comb, tit, art = self._sp_pick_best_item(items, orig_name, orig_artist, local_duration_ms, local_is_explicit)
                if picked:
                    if comb >= FUZZY_IDEAL or _ideal_pass_hunter(comb, tit, art):
                        return self._build_spotify_result(picked, comb, tit, art)
                    if comb > best_comb:
                        best_match = (picked, comb, tit, art)

        if best_match:
            return self._build_spotify_result(*best_match)
        return SearchResult(None, False)

    # ── Playlist Creation ──────────────────────────────────────────────

    async def create_playlist(self, platform: str, title: str, track_ids: list[str]) -> tuple[bool, str, int, list[str]]:
        self._cb[platform].check_or_raise()
        if platform == "YouTube Music":
            return await asyncio.to_thread(self._yt_create, title, track_ids)
        elif platform == "Apple Music":
            return await asyncio.to_thread(self._am_create, title, track_ids)
        elif platform == "Spotify":
            return await self._async_sp_create(title, track_ids)
        return False, "Platform not supported", 0, []

    async def _async_sp_create(self, title: str, ids: list[str]) -> tuple[bool, str, int, list[str]]:
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
                    if _is_spotify_rate_limited(e):
                        wait = _spotify_retry_after(e)
                        self._cb["Spotify"].trip(wait)
                        raise RateLimitError("Spotify", wait) from e
                    raise

        me    = await _one_call(sp.current_user)
        me_id = me["id"]
        pl    = await _one_call(sp.user_playlist_create, me_id, title, True, False, "Transferida por MelomaniacPass")
        for offset in range(0, len(ids), 100):
            await _one_call(sp.playlist_add_items, pl["id"], ids[offset:offset + 100])
        return True, pl["id"], len(ids), []

    def _yt_create(self, title: str, ids: list[str]) -> tuple[bool, str, int, list[str]]:
        try:
            pl_id = self._ytm.create_playlist(title, "Transferida por MelomaniacPass", video_ids=ids)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.youtube_auth_error = str(exc)
            if _is_ytm_unauthorized(exc):
                raise RuntimeError("Sesion YouTube Music expirada (401).") from exc
            raise
        try:
            items = self._ytm.get_playlist(pl_id, limit=len(ids) + 10)
            confirmed_ids = {t.get("videoId") for t in items.get("tracks", []) if t.get("videoId")}
            rejected      = [vid for vid in ids if vid not in confirmed_ids]
            return True, pl_id, len(confirmed_ids), rejected
        except Exception:  # pylint: disable=broad-exception-caught
            return True, pl_id, len(ids), []

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
