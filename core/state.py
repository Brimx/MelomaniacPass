"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                    Estado Global de la Aplicación                    ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: core/state.py
Descripción: Implementa AppState, el ViewModel central de la aplicación
            siguiendo el patrón BLoC (Business Logic Component). Coordina
            toda la lógica de negocio y actúa como única fuente de verdad
            para el estado de la aplicación.

Estrategia de Diseño - Patrón BLoC:
    AppState implementa un ViewModel reactivo que separa completamente
    la lógica de negocio de la UI:
    
    1. Única Fuente de Verdad:
       - Todo el estado de la aplicación reside en AppState
       - La UI solo lee estado, nunca lo modifica directamente
       - Todas las mutaciones ocurren en el event loop de asyncio
    
    2. Patrón Observer:
       - UI se suscribe a cambios con subscribe()
       - AppState notifica cambios con notify()
       - Flujo unidireccional: State → UI (nunca UI → State)
    
    3. Coordinación de Servicios:
       - AppState orquesta llamadas a MusicApiService
       - Maneja reintentos con backoff exponencial
       - Gestiona circuit breakers para rate limiting
    
    4. Gestión de Errores:
       - Post-mortem detallado de fallos
       - Tracking de canciones fallidas
       - Logs estructurados para debugging
    
    5. Estados de Carga y Transferencia:
       - LoadState: IDLE → LOADING_META → LOADING_TRACKS → READY/ERROR
       - TransferState: IDLE → RUNNING → DONE/ERROR
       - Progress tracking granular para feedback visual

Funciones Auxiliares:
    - _failure_reason_from_exc: Extrae razón legible de excepciones
    - _search_with_exponential_rl_backoff: Reintentos con backoff exponencial

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from typing import Callable, Optional

from core.models import Track, SearchResult, LoadState, TransferState
from utils.circuit_breaker import CircuitBreaker, RateLimitError, SpotifyBanException
from engine.normalizer import clean_metadata
from engine.match import _duration_to_seconds, FUZZY_REVISION_THRESHOLD, FUZZY_IDEAL


def _failure_reason_from_exc(exc: BaseException) -> str:
    """
    Extrae razón legible de una excepción para post-mortem de fallos.
    
    Proporciona mensajes de error amigables para el usuario, especialmente
    para errores HTTP de Spotify que incluyen códigos de estado específicos.
    
    Args:
        exc: Excepción capturada durante operación de API.
    
    Returns:
        String descriptivo del error, truncado a 300 caracteres.
    
    Example:
        >>> try:
        ...     # Llamada a API que falla
        ... except Exception as e:
        ...     reason = _failure_reason_from_exc(e)
        ...     # reason = "Spotify HTTP 429" o "Connection timeout"
    
    Note:
        Intenta importar SpotifyException dinámicamente para evitar
        dependencia hard de spotipy. Si no está disponible, usa el
        mensaje genérico de la excepción.
    """
    try:
        from spotipy.exceptions import SpotifyException  # pylint: disable=import-outside-toplevel
        if isinstance(exc, SpotifyException):
            hs = getattr(exc, "http_status", None)
            if hs is not None:
                return f"Spotify HTTP {hs}"
    except ImportError:
        pass
    msg = str(exc)
    return msg[:300] + ("…" if len(msg) > 300 else "")


async def _search_with_exponential_rl_backoff(
    service,
    platform: str,
    name: str,
    artist: str,
    *,
    local_duration_s: Optional[int] = None,
    local_duration_ms: int = 0,
    local_is_explicit: bool = False,
    log: Optional[Callable[[str], None]] = None,
    backoff_steps: int = 10,
) -> SearchResult:
    """
    Reintenta búsqueda con backoff exponencial ante rate limiting (HTTP 429).
    
    Implementa estrategia de reintento resiliente que duplica el tiempo de
    espera en cada intento, permitiendo que la API se recupere del throttling
    sin bombardearla con peticiones inmediatas.
    
    Args:
        service: Instancia de MusicApiService.
        platform: Plataforma destino ("Spotify", "YouTube Music", "Apple Music").
        name: Título de la canción.
        artist: Nombre del artista.
        local_duration_s: Duración en segundos para matching (opcional).
        log: Función de logging para registrar reintentos (opcional).
        backoff_steps: Número máximo de reintentos (default: 10).
    
    Returns:
        SearchResult con el track encontrado o flags de revisión.
    
    Raises:
        RateLimitError: Si se agotan todos los reintentos.
    
    Example:
        >>> result = await _search_with_exponential_rl_backoff(
        ...     service, "Spotify", "Bohemian Rhapsody", "Queen",
        ...     log=lambda msg: print(msg)
        ... )
    
    Note:
        El backoff exponencial es crítico para evitar ban permanente de APIs.
        Secuencia típica: 1s → 2s → 4s → 8s → 16s → 32s → 64s → 128s → 256s → 512s
        
        Esto da tiempo suficiente para que el rate limit se resetee sin
        desperdiciar reintentos en ventanas de throttling activo.
    """
    rl_backoff: Optional[float] = None
    for step in range(backoff_steps):
        try:
            return await service.search_with_fallback(
                platform, name, artist, local_duration_s=local_duration_s,
                local_duration_ms=local_duration_ms,
                local_is_explicit=local_is_explicit,
            )
        except RateLimitError as e:
            ra = max(1, int(e.retry_after))
            if rl_backoff is None:
                rl_backoff = float(ra)
            if log:
                log(
                    f"[WARN] 429 {platform}: esperando {int(rl_backoff)}s "
                    f"(backoff · {step + 1}/{backoff_steps})"
                )
            await asyncio.sleep(rl_backoff)
            rl_backoff *= 2.0
    if log:
        log(f"[ERROR] 429: agotados reintentos en {platform}")
    raise RateLimitError(platform, int(rl_backoff or 60))


class AppState:
    """
    ViewModel central siguiendo el patrón BLoC (Business Logic Component).
    
    Actúa como única fuente de verdad para el estado de la aplicación,
    coordinando toda la lógica de negocio y notificando cambios a la UI
    mediante el patrón Observer.
    
    Responsabilidades:
        1. Gestión de estado de carga de playlists
        2. Coordinación de transferencias entre plataformas
        3. Tracking de progreso y errores
        4. Gestión de circuit breakers
        5. Validación de sesiones de autenticación
        6. Logging estructurado de operaciones
    
    Attributes:
        service: Instancia de MusicApiService para comunicación con APIs.
        source: Plataforma de origen ("Spotify", "YouTube Music", etc).
        destination: Plataforma destino.
        playlist_id: ID de la playlist cargada.
        playlist_name: Nombre de la playlist.
        tracks: Lista completa de canciones cargadas.
        filtered: Lista filtrada de canciones (por búsqueda).
        load_state: Estado actual de carga (LoadState enum).
        transfer_state: Estado actual de transferencia (TransferState enum).
        transfer_progress: Número de canciones procesadas.
        transfer_total: Total de canciones a transferir.
        log_lines: Líneas de log para telemetría.
        failed_tracks: Canciones que fallaron en transferencia.
        cb: Diccionario de circuit breakers por plataforma.
    
    Constantes:
        PLATFORMS: Lista de plataformas soportadas.
        LOCAL_SOURCES: Set de fuentes locales (archivo, texto).
        SOURCE_OPTIONS: Todas las opciones de fuente disponibles.
    
    Methods:
        subscribe: Registra un listener para notificaciones de cambio.
        notify: Notifica a todos los listeners de un cambio de estado.
        load_playlist: Carga una playlist desde una plataforma.
        start_transfer: Inicia transferencia a plataforma destino.
        cancel_lazy_scan: Cancela escaneo lazy en progreso.
    
    Example:
        >>> state = AppState(service)
        >>> state.subscribe(lambda: print("Estado cambió"))
        >>> await state.load_playlist("spotify", "playlist_id")
        Estado cambió
    
    Note:
        Todas las mutaciones de estado deben ocurrir en el event loop de
        asyncio para garantizar thread-safety. La UI nunca debe modificar
        el estado directamente, solo leerlo y llamar métodos de AppState.
    """

    # Plataformas de streaming soportadas
    PLATFORMS = ["Apple Music", "Spotify", "YouTube Music"]
    
    # Fuentes locales (no requieren autenticación)
    LOCAL_SOURCES: frozenset = frozenset({"Archivo Local", "Pegar Texto"})
    
    # Todas las opciones de fuente disponibles en la UI
    SOURCE_OPTIONS = ["Apple Music", "Spotify", "YouTube Music", "Archivo Local", "Pegar Texto"]

    def __init__(self, service) -> None:
        """
        Inicializa el estado global de la aplicación.
        
        Args:
            service: Instancia de MusicApiService para comunicación con APIs.
        
        Note:
            El constructor inicializa todos los campos de estado con valores
            por defecto. La UI debe suscribirse inmediatamente después de
            la construcción para recibir notificaciones de cambios.
        """
        self.service = service

        # ──────────────────────────────────────────────────────────────
        # CONFIGURACIÓN DE FUENTE Y DESTINO
        # ──────────────────────────────────────────────────────────────
        
        self.source:      str = "Apple Music"
        self.destination: str = "YouTube Music"
        self.destination_confirmed: bool = True

        # ──────────────────────────────────────────────────────────────
        # ESTADO DE PLAYLIST CARGADA
        # ──────────────────────────────────────────────────────────────
        
        self.playlist_id:   str         = ""
        self.playlist_name: str         = "Cargar una playlist"
        self.tracks:        list[Track] = []
        self.filtered:      list[Track] = []
        self.load_state:    LoadState   = LoadState.IDLE
        self.load_error:    str         = ""

        # ──────────────────────────────────────────────────────────────
        # ESTADO DE TRANSFERENCIA
        # ──────────────────────────────────────────────────────────────
        
        self.transfer_state:    TransferState = TransferState.IDLE
        self.transfer_progress: int           = 0
        self.transfer_total:    int           = 0
        self.log_lines:         list[str]     = []
        self.failed_tracks:     list[Track]   = []

        # ──────────────────────────────────────────────────────────────
        # CONTADORES DE TRACKING
        # ──────────────────────────────────────────────────────────────
        
        self.count_detected:   int            = 0
        self.count_candidates: int            = 0
        self.count_processed:  int            = 0
        self.count_confirmed:  int            = 0
        self.api_rejected_tracks: list[Track] = []

        # ──────────────────────────────────────────────────────────────
        # BÚSQUEDA Y FILTRADO
        # ──────────────────────────────────────────────────────────────
        
        self.search_query: str = ""

        # ──────────────────────────────────────────────────────────────
        # CIRCUIT BREAKERS POR PLATAFORMA
        # ──────────────────────────────────────────────────────────────
        # Protección contra rate limiting de APIs
        
        self.cb: dict[str, CircuitBreaker] = {
            p: CircuitBreaker(p) for p in self.PLATFORMS
        }
        self.service._cb = self.cb

        # ──────────────────────────────────────────────────────────────
        # ESTADO DE AUTENTICACIÓN
        # ──────────────────────────────────────────────────────────────
        
        self.auth_session_ok:   dict[str, bool] = {p: True for p in self.PLATFORMS}
        self.auth_session_hint: dict[str, str]  = {p: "" for p in self.PLATFORMS}

        # ──────────────────────────────────────────────────────────────
        # TRACKING DE CANCIONES PROBLEMÁTICAS
        # ──────────────────────────────────────────────────────────────
        
        self.pending_review_tracks: list[Track] = []
        self.transfer_error_tracks: list[Track] = []

        # ──────────────────────────────────────────────────────────────
        # LAZY SCAN (ESCANEO DIFERIDO)
        # ──────────────────────────────────────────────────────────────
        
        self.lazy_scan_running: bool = False
        self.lazy_scan_done:    bool = False

        # ──────────────────────────────────────────────────────────────
        # PATRÓN OBSERVER
        # ──────────────────────────────────────────────────────────────
        
        self._listeners: list[Callable[[], None]] = []
        self._lazy_task: Optional[asyncio.Task]   = None

    # ══════════════════════════════════════════════════════════════════
    # PATRÓN OBSERVER
    # ══════════════════════════════════════════════════════════════════

    def subscribe(self, cb: Callable[[], None]) -> None:
        """
        Registra un callback para recibir notificaciones de cambio de estado.
        
        El callback será invocado cada vez que notify() sea llamado,
        típicamente después de cualquier mutación de estado.
        
        Args:
            cb: Función sin argumentos que será llamada en cada cambio.
        
        Example:
            >>> def on_change():
            ...     print("Estado actualizado")
            >>> state.subscribe(on_change)
        
        Note:
            Los callbacks deben ser síncronos y rápidos. Operaciones
            pesadas deben delegarse a tareas asyncio separadas.
        """
        self._listeners.append(cb)

    def notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception as e:  # pylint: disable=broad-exception-caught
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
            self.load_state = LoadState.ERROR
            self.load_error = str(e)
        finally:
            self.notify()

    def load_local_tracks(self, tracks: list, playlist_name: str = "Playlist Local") -> None:
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
        self.destination_confirmed = False
        self._log(f"[INFO] Ingesta local · {len(tracks)} pistas cargadas")
        self.notify()

    def reset_session(self) -> None:
        self.cancel_lazy_scan()
        self.playlist_id   = ""
        self.playlist_name = "Cargar una playlist"
        self.tracks        = []
        self.filtered      = []
        self.search_query  = ""
        self.load_state    = LoadState.IDLE
        self.load_error    = ""
        self.lazy_scan_running     = False
        self.lazy_scan_done        = False
        self.transfer_state        = TransferState.IDLE
        self.transfer_progress     = 0
        self.transfer_total        = 0
        self.failed_tracks         = []
        self.api_rejected_tracks   = []
        self.pending_review_tracks = []
        self.transfer_error_tracks = []
        self.log_lines             = []
        self.destination_confirmed = True
        self.notify()

    async def transfer_playlist(self) -> None:
        selected = [t for t in self.tracks if t.selected]
        if not selected:
            return

        self.cancel_lazy_scan()
        self.lazy_scan_running = False
        self.lazy_scan_done    = False

        self.transfer_state        = TransferState.RUNNING
        self.transfer_progress     = 0
        self.transfer_total        = len(selected)
        self.failed_tracks         = []
        self.api_rejected_tracks   = []
        self.pending_review_tracks = []
        self.transfer_error_tracks = []
        self.count_detected        = len(selected)
        self.count_candidates      = 0
        self.count_processed       = 0
        self.count_confirmed       = 0
        self._log(
            f"[INFO] Iniciando transferencia · "
            f"{self.count_detected} detectadas → {self.destination}"
        )
        self.notify()

        dest_ids: list[str]          = []
        dest_id_to_track: dict[str, Track] = {}
        completed_count = 0
        BATCH_SIZE      = 10
        batch_pending   = 0

        async def _transfer_one(track: Track) -> Optional[str]:
            nonlocal completed_count, batch_pending

            cn, ca = clean_metadata(track.name, track.artist)
            if not cn.strip():
                track.transfer_status = "error"
                track.failure_reason  = "Metadatos vacíos tras The Purge"
                self._log(f"[ERROR] Metadatos vacíos, saltando: '{track.name[:42]}'")
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

            if cache_key in self.service.search_cache:
                raw = self.service.search_cache[cache_key]
                cached = (
                    raw if isinstance(raw, SearchResult)
                    else SearchResult(raw, False) if isinstance(raw, str) and raw
                    else SearchResult(None, False)
                )
                if not cached.track_id:
                    track.transfer_status = "not_found"
                    track.failure_reason  = track.failure_reason or "Sin resultados (caché)"
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                elif cached.needs_review:
                    track.transfer_status = "revision_necesaria"
                    track.failure_reason  = "Fuzzy <40% (caché)"
                    if track not in self.pending_review_tracks:
                        self.pending_review_tracks.append(track)
                    self._log(f"[WARN]  ⚠ Revisión (caché): {track.name[:42]}")
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
                return cached.track_id if cached.track_id and not cached.needs_review else None

            track.transfer_status = "searching"
            self._log(f"[INFO]  🔍 Buscando: {track.name[:42]}")

            match     = SearchResult(None, False)
            last_exc: Optional[BaseException] = None
            for attempt in range(3):
                try:
                    match = await _search_with_exponential_rl_backoff(
                        self.service, self.destination,
                        track.name, track.artist,
                        local_duration_s=local_dur_s,
                        local_duration_ms=track.duration_ms,
                        local_is_explicit=track.is_explicit,
                        log=self._log,
                    )
                    break
                except RateLimitError:
                    raise
                except SpotifyBanException:
                    raise
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    last_exc = exc
                    wait_s = 2 ** attempt
                    if attempt < 2:
                        self._log(
                            f"[ERROR] Intento {attempt+1}/3 · "
                            f"{track.name[:30]} — reintentando en {wait_s}s"
                        )
                        await asyncio.sleep(wait_s)

            self.service.search_cache[cache_key] = match

            if match.track_id and match.needs_review:
                track.transfer_status = "revision_necesaria"
                track.failure_reason  = f"Confianza fuzzy <{FUZZY_REVISION_THRESHOLD}% (título/artista)"
                if track not in self.pending_review_tracks:
                    self.pending_review_tracks.append(track)
                self._log(f"[WARN]  ⚠ Revisión necesaria (fuzzy <{FUZZY_REVISION_THRESHOLD}%): {track.name[:42]}")
            elif match.track_id and match.low_confidence:
                if getattr(track, 'platform', '') == 'local':
                    track.transfer_status = "not_found"
                    track.failure_reason  = f"Similitud <{FUZZY_IDEAL}% (umbral local estricto)"
                    self._log(f"[WARN]  ✗ Local · fuzzy <{FUZZY_IDEAL}% rechazado: {track.name[:42]}")
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                else:
                    track.transfer_status = "found"
                    self.count_processed += 1
                    self._log(f"[INFO]  Hunter · fuzzy 70–84% (aceptado): {track.name[:42]}")
            elif match.track_id:
                track.transfer_status = "found"
                self.count_processed += 1
                self._log(f"[SUCCESS] ✓ Encontrada: {track.name[:42]}")
            else:
                track.transfer_status = "not_found"
                track.failure_reason  = _failure_reason_from_exc(last_exc) if last_exc else "Sin resultados en la API del destino"
                self._log(f"[ERROR]   ✗ No encontrada: {track.name[:42]}")
                if track not in self.failed_tracks:
                    self.failed_tracks.append(track)

            completed_count += 1
            self.transfer_progress = completed_count
            batch_pending += 1
            if batch_pending >= BATCH_SIZE:
                batch_pending = 0
                self.notify()

            return match.track_id if match.track_id and not match.needs_review else None

        try:
            init_ok = await self._ensure_auth(self.destination)
            if not init_ok:
                raise RuntimeError(f"No se pudo autenticar en {self.destination}")

            results = list(await asyncio.gather(
                *(_transfer_one(t) for t in selected),
                return_exceptions=True,
            ))

            ban = next((r for r in results if isinstance(r, SpotifyBanException)), None)
            if ban:
                self._log(
                    f"[FATAL] Bloqueo de Spotify. Baneo por {int(ban.retry_after)} segundos. "
                    "Operaci\u00f3n abortada."
                )
                self.transfer_state = TransferState.ERROR
                self.notify()
                return

            for track, result in zip(selected, results):
                if isinstance(result, RateLimitError):
                    self.cb[self.destination].trip(result.retry_after)
                    track.transfer_status = "error"
                    track.failure_reason  = f"Rate limit ({result.retry_after}s)"
                    if track not in self.failed_tracks:
                        self.failed_tracks.append(track)
                    if track not in self.transfer_error_tracks:
                        self.transfer_error_tracks.append(track)
                elif isinstance(result, Exception):
                    track.transfer_status = "error"
                    track.failure_reason  = _failure_reason_from_exc(result)
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
                names = ", ".join(t.name[:36] for t in self.pending_review_tracks[:15])
                if len(self.pending_review_tracks) > 15:
                    names += "…"
                self._log(f"[INFO]  📋 Pendientes de revisión ({len(self.pending_review_tracks)}): {names}")

            if dest_ids:
                self._log(f"[INFO]  📁 Creando playlist con {len(dest_ids)} canciones…")
                self.notify()
                ok, msg, confirmed_count, rejected_ids = await self.service.create_playlist(
                    self.destination, self.playlist_name, dest_ids
                )
                if ok:
                    self.count_confirmed   = confirmed_count
                    self.transfer_progress = confirmed_count
                    for vid in rejected_ids:
                        t = dest_id_to_track.get(vid)
                        if t:
                            t.transfer_status = "error"
                            self._log(f"[ERROR] ⚠ No insertada por API ({self.destination}): {t.name[:42]}")
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
                self._log("[WARN]  Solo coincidencias con baja confianza (revisión); no se creó playlist.")
                self.transfer_state = TransferState.DONE
            else:
                raise RuntimeError("No se encontraron coincidencias en el destino.")

        except RateLimitError as e:
            self.cb[e.platform].trip(e.retry_after)
            self._log(f"[ERROR] ⚠ Rate limit en {e.platform}: espera {e.retry_after}s")
            self.transfer_state = TransferState.ERROR
        except Exception as e:  # pylint: disable=broad-exception-caught
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
        self.destination_confirmed = val not in self.LOCAL_SOURCES
        self.notify()

    def set_destination(self, val: str) -> None:
        self.destination = val
        self.destination_confirmed = True
        self.notify()

    def _log(self, msg: str) -> None:
        self.log_lines.append(msg)
        if len(self.log_lines) > 200:
            self.log_lines = self.log_lines[-200:]

    def log(self, msg: str) -> None:
        self._log(msg)

    def cancel_lazy_scan(self) -> None:
        if self._lazy_task and not self._lazy_task.done():
            self._lazy_task.cancel()
            self._lazy_task = None

    async def _lazy_availability_scan(self, tracks: list) -> None:
        dest_ok = await self._ensure_auth(self.destination)
        if not dest_ok:
            return

        self.lazy_scan_running = True
        self.lazy_scan_done    = False
        self.transfer_total    = len(tracks)
        self.transfer_progress = 0
        self.notify()

        BATCH_SIZE = 5
        done_count = 0

        async def _check_one(track: Track) -> None:
            nonlocal done_count
            cn, ca    = clean_metadata(track.name, track.artist)
            cache_key = f"{cn.lower()}|||{ca.lower()}|||{self.destination}"
            local_dur_s = _duration_to_seconds(track.duration)

            if cache_key in self.service.search_cache:
                raw = self.service.search_cache[cache_key]
                res = (
                    raw if isinstance(raw, SearchResult)
                    else SearchResult(raw, False) if isinstance(raw, str) and raw
                    else SearchResult(None, False)
                )
                track.transfer_status = (
                    "not_found" if not res.track_id
                    else "revision_necesaria" if res.needs_review
                    else "found"
                )
            else:
                try:
                    result = await _search_with_exponential_rl_backoff(
                        self.service, self.destination,
                        track.name, track.artist,
                        local_duration_s=local_dur_s,
                        local_duration_ms=track.duration_ms,
                        local_is_explicit=track.is_explicit,
                        log=self._log,
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    result = SearchResult(None, False)
                self.service.search_cache[cache_key] = result
                track.transfer_status = (
                    "not_found" if not result.track_id
                    else "revision_necesaria" if result.needs_review
                    else "found"
                )

            done_count += 1
            self.transfer_progress = done_count
            if done_count % BATCH_SIZE == 0:
                self.notify()

        try:
            await asyncio.gather(*[_check_one(t) for t in tracks], return_exceptions=True)
        except asyncio.CancelledError:
            self.lazy_scan_running = False
            self.notify()
            return
        self.lazy_scan_running = False
        self.lazy_scan_done    = True
        self.notify()
