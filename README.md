# MelomaniacPass v5.0

Aplicación de escritorio para transferir playlists entre **Spotify**, **YouTube Music** y **Apple Music** con matching inteligente de canciones.

```bash
python app.py
```

---

## Estructura del Proyecto

```
melomaniacpass/
├── app.py                    # Entry point (~200 líneas)
├── auth_manager.py           # Autenticación (OAuth, tokens, wizard UI, pre-flight)
├── cache_handler.py          # Caché de tokens Spotify (CacheFileHandler + variantes)
├── .env                      # Credenciales Spotify + Apple Music
├── browser.json              # Headers de sesión YouTube Music
│
├── core/
│   ├── models.py             # Dataclasses: Track, SearchResult, LoadState, TransferState
│   └── state.py              # AppState — ViewModel central (patrón BLoC)
│
├── services/
│   └── api_service.py        # MusicApiService — Facade unificado Spotify + YTM + Apple
│
├── engine/
│   ├── normalizer.py         # Limpieza y normalización de metadatos (Unicode, regex)
│   ├── match.py              # Sistema Hunter Recovery: validar_match, scoring, score_spotify_match
│   ├── parsers.py            # Parsers de playlists locales (CSV, M3U, XSPF, WPL, iTunes XML)
│   └── organizer.py          # Ordenamiento y segmentación de listas en memoria
│
├── ui/
│   ├── main_ui.py            # PlaylistManagerUI — interfaz principal
│   ├── song_row.py           # SongRow, SkeletonRow
│   ├── telemetry.py          # TelemetryDrawer — panel Monitor / Consola / Post-Mortem
│   └── widgets.py            # Botones y componentes reutilizables
│
└── utils/
    └── circuit_breaker.py    # CircuitBreaker + RateLimitError + SpotifyBanException
```

---

## Flujo de Dependencias

```
auth_manager.py + cache_handler.py
        ↓
utils/circuit_breaker.py
        ↓
engine/  (normalizer → match → parsers → organizer)
        ↓
core/models.py
        ↓
services/api_service.py
        ↓
core/state.py
        ↓
ui/  (widgets → song_row → telemetry → main_ui)
        ↓
app.py
```

---

## Librerías Utilizadas

| Librería | Módulos que la usan | Propósito |
|---|---|---|
| `flet` | app.py, ui/*, auth_manager.py | Framework de UI |
| `asyncio` | app.py, ui/main_ui.py, services/, core/state.py | Operaciones asíncronas |
| `requests` | services/api_service.py, auth_manager.py | Llamadas HTTP a APIs |
| `spotipy` | cache_handler.py, auth_manager.py, services/ | SDK oficial de Spotify |
| `ytmusicapi` | services/api_service.py | SDK de YouTube Music |
| `python-dotenv` | app.py, services/api_service.py, auth_manager.py | Lectura/escritura de `.env` |
| `rapidfuzz` | engine/match.py, engine/normalizer.py | Matching fuzzy de alto rendimiento |
| `re` + `unicodedata` | engine/normalizer.py, engine/parsers.py, engine/match.py | Normalización de texto |
| `csv` | engine/parsers.py | Parseo de playlists CSV |
| `xml.etree.ElementTree` | engine/parsers.py | Parseo de iTunes XML, XSPF, WPL |
| `json` | auth_manager.py, cache_handler.py | Lectura/escritura de `browser.json` |
| `pathlib` | auth_manager.py | Rutas de archivos |
| `threading` + `webbrowser` | auth_manager.py | Servidor OAuth local para Spotify |
| `collections.defaultdict` | engine/organizer.py | Agrupación de segmentos |

---

## Autenticación y Acceso a Archivos de Configuración

### `.env` — Spotify y Apple Music

```env
SPOTIFY_CLIENT_ID='...'
SPOTIFY_CLIENT_SECRET='...'
SPOTIFY_REDIRECT_URI='http://127.0.0.1:8080/callback'

APPLE_AUTH_BEARER='Bearer eyJhbGc...'
APPLE_MUSIC_USER_TOKEN='0.AsH5+9...'
```

**Quién accede y cómo:**

```python
# auth_manager.py — lectura y escritura
from pathlib import Path
from dotenv import load_dotenv, set_key

ENV_FILE = Path(__file__).parent / ".env"

load_dotenv(ENV_FILE)
client_id = os.getenv("SPOTIFY_CLIENT_ID")
set_key(ENV_FILE, "APPLE_AUTH_BEARER", nuevo_valor)  # escritura desde ConfigWizard

# services/api_service.py — solo lectura
from dotenv import load_dotenv
load_dotenv()
self.apple_bearer = os.getenv("APPLE_AUTH_BEARER")

# app.py — carga inicial
from dotenv import load_dotenv
load_dotenv()
```

---

### `browser.json` — YouTube Music

```json
{
    "Accept": "*/*",
    "Authorization": "SAPISIDHASH ...",
    "Content-Type": "application/json",
    "X-Goog-AuthUser": "0",
    "x-origin": "https://music.youtube.com",
    "Cookie": "..."
}
```

**Quién accede y cómo:**

```python
# auth_manager.py — lectura y escritura, exporta la ruta
import json
from pathlib import Path

BROWSER_JSON = Path(__file__).parent / "browser.json"

# lectura
with open(BROWSER_JSON, 'r', encoding='utf-8') as f:
    headers = json.load(f)

# escritura desde ConfigWizard
write_browser_json(authorization="SAPISIDHASH ...", cookie="...")

# services/api_service.py — importa la ruta desde auth_manager
from auth_manager import BROWSER_JSON

self._ytm = YTMusic(auth=str(BROWSER_JSON), requests_session=self._yt_http_session)
```

---

### Flujo de Autenticación por Plataforma

**Spotify (OAuth 2.0 Authorization Code Flow):**
```
.env (CLIENT_ID, CLIENT_SECRET)
    → auth_manager genera URL de autorización
    → usuario abre navegador y autoriza
    → pega la URL de redirección en el diálogo de la app
    → service.handle_spotify_redirect() intercambia code por access_token
    → token guardado en .spotify_cache via CacheFileHandler
    → services/api_service.py lo usa en cada request via spotipy
```

**YouTube Music (Headers de sesión):**
```
Usuario copia headers del navegador (F12 → Network)
    → ConfigWizard los guarda en browser.json via write_browser_json()
    → services/api_service.py los carga al iniciar con YTMusic(auth=...)
    → se incluyen en cada request a la API
```

**Apple Music (Bearer + User Token):**
```
Usuario obtiene tokens desde Apple Music Web
    → ConfigWizard los guarda en .env con set_key()
    → services/api_service.py los lee con os.getenv()
    → se incluyen en headers de cada request
```

**Pre-flight checks (auth_manager.py):**
```
Al iniciar la app → run_preflight() ejecuta en paralelo:
    _preflight_youtube()  → valida browser.json + llamada real a YTMusic
    _preflight_spotify()  → valida .spotify_cache + GET /v1/me
    _preflight_apple()    → valida .env + GET /v1/me/storefront
    → resultados → AuthManager.ingest_preflight_results()
    → iconos de estado en la UI actualizados
```

---

## Comunicación entre Módulos

### Inicialización en `app.py`

```python
circuit_breakers = {p: CircuitBreaker(p) for p in AppState.PLATFORMS}
service      = MusicApiService(circuit_breakers)
state        = AppState(service)
ui           = PlaylistManagerUI(page, state)
auth_manager = AuthManager(page, service, state)

# Referencias bidireccionales
service.auth_manager = auth_manager
ui.auth_manager      = auth_manager
```

### Patrón Observer (estado → UI)

```python
# core/state.py
class AppState:
    def notify(self):
        for callback in self._listeners:
            callback()

# ui/main_ui.py
class PlaylistManagerUI:
    def __init__(self, page, state: AppState):
        state.subscribe(self._on_state_changed)
        for platform, cb in state.cb.items():
            cb.subscribe(lambda is_open, rem, p=platform: self._on_circuit_change(p, is_open, rem))
```

### Ejemplo: Búsqueda de canción

```
ui/main_ui.py         → state.load_playlist(playlist_id)
core/state.py         → service.fetch_playlist(source, id, progress_cb)
services/api_service  → request a plataforma origen
                      → retorna (name, list[Track])
core/state.py         → self.tracks = tracks → notify()
ui/main_ui.py         → _on_state_changed() → actualiza lista
```

### Ejemplo: Transferencia de playlist

```
ui/main_ui.py         → state.transfer_playlist()
core/state.py         → para cada Track seleccionado (concurrente):
engine/normalizer     →   clean_metadata(title, artist)
services/api_service  →   search_with_fallback(platform, name, artist)
engine/match.py       →   validar_match() + score_spotify_match()
core/state.py         →   SearchResult → track_id o needs_review
services/api_service  →   create_playlist(platform, name, ids)
core/state.py         →   TransferState.DONE → notify()
ui/main_ui.py         → _on_state_changed() → muestra post-mortem
```

### Ejemplo: Organizar / Dividir lista

```
ui/main_ui.py         → state.organize_sort(["artist"], reverse=False)
core/state.py         → engine/organizer.sort_tracks(tracks, keys, reverse)
                      → self.tracks = sorted_tracks → notify()

ui/main_ui.py         → state.organize_split("artist")
core/state.py         → engine/organizer.split_tracks(tracks, "artist")
                      → self.segments = {"Queen": [...], "Beatles": [...]}
                      → notify()
```

---

## Módulos Clave — Resumen de Responsabilidades

### `engine/organizer.py` (nuevo en v5.0)
Transformaciones de datos en memoria sin I/O:
- `sort_tracks(tracks, keys, reverse)` — ordena por artista, álbum, título, duración o plataforma
- `split_tracks(tracks, key)` — segmenta la lista maestra en grupos por atributo

### `engine/match.py` — Sistema Hunter Recovery
- `validar_match()` — validación multi-capa L0→L3 para YouTube Music
- `_fuzzy_scores_triple()` — scores desglosados: combinado, título, artista
- `score_spotify_match()` — scoring base-100 para Spotify (reemplaza `popularity` eliminado en 2026)
- `_yt_select_best()` — selección del mejor resultado con tie-breaker por duración

### `utils/circuit_breaker.py`
- `CircuitBreaker` — patrón circuit breaker con auto-reset y notificaciones a UI
- `RateLimitError` — excepción para HTTP 429 con tiempo de espera
- `SpotifyBanException` — excepción fatal para ban activo de Spotify (aborta transferencia)

### `services/api_service.py` — SpotifyRateLimiter
Además del `CircuitBreaker` global, el servicio implementa un `SpotifyRateLimiter` interno con 3 niveles:
1. Token bucket (nivel 1) — limita requests por segundo
2. Sliding window (nivel 2) — limita requests por minuto
3. Kill switch (nivel 3) — lanza `SpotifyBanException` si Spotify devuelve 429 a pesar de los niveles anteriores

---

## Métricas de Refactorización

| | Antes | Después |
|---|---|---|
| Archivos | 1 monolito | 16 módulos |
| Líneas entry point | ~5000 | ~200 |
| Tamaño entry point | 218 KB | ~8 KB |
| Módulos de engine | 0 | 4 (normalizer, match, parsers, organizer) |

---

## Respaldo

El monolito original está en `v. 0.1/refactor_backup/app.py` (218 KB).
