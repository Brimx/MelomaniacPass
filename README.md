# 🎵 MelomaniacPass v5.0

Aplicación de escritorio para transferir playlists entre **Spotify**, **YouTube Music** y **Apple Music** con matching inteligente de canciones.

```bash
python app.py
```

---

## Estructura del Proyecto

```
melomaniacpass/
├── app.py                    # Entry point (~60 líneas)
├── auth_manager.py           # Autenticación (OAuth, tokens, wizard UI)
├── cache_handler.py          # Caché de tokens Spotify
├── .env                      # Credenciales Spotify + Apple Music
├── browser.json              # Headers de sesión YouTube Music
│
├── core/
│   ├── models.py             # Dataclasses: Track, SearchResult, LoadState, TransferState
│   └── state.py              # AppState — estado global de la app
│
├── services/
│   └── api_service.py        # MusicApiService — Spotify + YTM + Apple unificado
│
├── engine/
│   ├── normalizer.py         # Limpieza y normalización de metadatos (Unicode, regex)
│   ├── match.py              # Matching fuzzy: validar_match, scoring, _yt_select_best
│   └── parsers.py            # Parsers de playlists locales (CSV, iTunes XML, M3U)
│
├── ui/
│   ├── main_ui.py            # PlaylistManagerUI — interfaz principal
│   ├── song_row.py           # SongRow, SkeletonRow
│   ├── telemetry.py          # TelemetryDrawer — panel de monitoreo
│   └── widgets.py            # Botones y componentes reutilizables
│
└── utils/
    └── circuit_breaker.py    # CircuitBreaker + RateLimitError
```

---

## Flujo de Dependencias

```
auth_manager.py + cache_handler.py
        ↓
utils/circuit_breaker.py
        ↓
engine/  (normalizer → match → parsers)
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
| `asyncio` | app.py, ui/main_ui.py, services/, ui/song_row.py | Operaciones asíncronas |
| `requests` | services/api_service.py, auth_manager.py | Llamadas HTTP a APIs |
| `spotipy` | cache_handler.py, auth_manager.py | SDK oficial de Spotify |
| `python-dotenv` | app.py, services/api_service.py, auth_manager.py | Lectura/escritura de `.env` |
| `re` + `unicodedata` | engine/normalizer.py, engine/parsers.py | Normalización de texto |
| `csv` | engine/parsers.py | Parseo de playlists CSV |
| `xml.etree.ElementTree` | engine/parsers.py | Parseo de iTunes XML |
| `json` | auth_manager.py, cache_handler.py | Lectura/escritura de `browser.json` |
| `pathlib` | auth_manager.py | Rutas de archivos |
| `threading` + `webbrowser` | auth_manager.py | Servidor OAuth local para Spotify |

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
    "Authorization": "SAPISIDHASH ...",
    "Cookie": "...",
    "Accept": "*/*",
    "Content-Type": "application/json",
    "X-Goog-AuthUser": "0",
    "x-origin": "https://music.youtube.com"
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
with open(BROWSER_JSON, 'w', encoding='utf-8') as f:
    json.dump(headers_dict, f, indent=4)

# services/api_service.py — importa la ruta desde auth_manager
from auth_manager import BROWSER_JSON

with open(BROWSER_JSON, 'r') as f:
    self.ytm_headers = json.load(f)
```

---

### Flujo de Autenticación por Plataforma

**Spotify (OAuth 2.0 Authorization Code Flow):**
```
.env (CLIENT_ID, CLIENT_SECRET)
    → auth_manager abre navegador
    → servidor HTTP local en :8080 captura el callback
    → intercambia code por access_token
    → token guardado en cache_handler.py
    → services/api_service.py lo usa en cada request
```

**YouTube Music (Headers de sesión):**
```
Usuario copia headers del navegador
    → auth_manager los guarda en browser.json
    → services/api_service.py los carga al iniciar
    → se incluyen en cada request a la API
```

**Apple Music (Bearer + User Token):**
```
Usuario obtiene tokens desde Apple Music Web
    → auth_manager los guarda en .env con set_key()
    → services/api_service.py los lee con os.getenv()
    → se incluyen en headers de cada request
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
        state.add_listener(self.on_state_change)
```

### Ejemplo: Búsqueda de canción

```
ui/main_ui.py         → state.search_spotify(query)
core/state.py         → service.search_spotify(query)
services/api_service  → engine/normalizer.build_search_query()
                      → request a Spotify API
                      → retorna list[SearchResult]
core/state.py         → actualiza self.search_results → notify()
ui/main_ui.py         → on_state_change() → actualiza UI
```

### Ejemplo: Transferencia de playlist

```
ui/main_ui.py         → state.start_transfer(platform)
core/state.py         → para cada Track:
engine/normalizer     →   clean_metadata(title, artist)
services/api_service  →   search_[platform](query)
engine/match.py       →   validar_match(original, resultado)
services/api_service  →   add_to_playlist(track_id)  ← si match válido
core/state.py         →   TransferState actualizado → notify()
ui/main_ui.py         → actualiza progreso en tiempo real
```

---

## Métricas de Refactorización

| | Antes | Después |
|---|---|---|
| Archivos | 1 monolito | 15 módulos |
| Líneas entry point | ~5000 | ~60 |
| Tamaño entry point | 218 KB | 6 KB |
| Errores de diagnóstico | — | 0 |

---

## Respaldo

El monolito original está en `v. 0.1/refactor_backup/app.py` (218 KB).
