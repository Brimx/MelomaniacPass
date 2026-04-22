"""
auth_manager.py — MelomaniacPass v5.0 — ISRC-Master Auth
════════════════════════════════════════════════════════
Centralises ALL credential I/O, pre-flight validation, and the Flet
Configuration Wizard.

Credential contract by platform
────────────────────────────────
• YouTube Music  → editable via ConfigWizard (browser.json)
• Apple Music    → editable via ConfigWizard (.env)
• Spotify        → read-only in UI; credentials set directly in .env;
                   authentication via PKCE (Proof Key for Code Exchange)
                   — no CLIENT_SECRET required.
                   (browser redirect to http://127.0.0.1:8080/callback)

browser.json  (YouTube Music)
    {
        "Accept": "*/*",
        "Authorization": "<SAPISIDHASH …>",
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": "https://music.youtube.com",
        "Cookie": "<raw cookie string>"
    }

.env  (Spotify & Apple Music)
    # SPOTIFY — PKCE (Public Client, sin CLIENT_SECRET)
    SPOTIFY_CLIENT_ID="<App Client ID from Developer Dashboard>"
    SPOTIFY_REDIRECT_URI="http://127.0.0.1:8080/callback"

    # APPLE MUSIC
    APPLE_AUTH_BEARER="<value>"
    APPLE_MUSIC_USER_TOKEN="<value>"
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import random
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import flet as ft
import requests
from dotenv import dotenv_values, load_dotenv, set_key

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
BROWSER_JSON = BASE_DIR / "browser.json"
ENV_FILE     = BASE_DIR / ".env"

# ── Fixed keys in browser.json ─────────────────────────────────────────
BROWSER_JSON_FIXED: dict[str, str] = {
    "Accept":          "*/*",
    "Content-Type":    "application/json",
    "X-Goog-AuthUser": "0",
    "x-origin":        "https://music.youtube.com",
}

# ── Required .env variable names (exact, ordered) ──────────────────────
ENV_KEYS_SPOTIFY = [
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_REDIRECT_URI",
]
ENV_KEYS_APPLE = [
    "APPLE_AUTH_BEARER",
    "APPLE_MUSIC_USER_TOKEN",
]
ENV_KEYS_ALL = ENV_KEYS_SPOTIFY + ENV_KEYS_APPLE

# ── Spotify OAuth settings ──────────────────────────────────────────────
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8080/callback"
SPOTIFY_CALLBACK_PORT = 8080
SPOTIFY_OAUTH_TIMEOUT = 180  # seconds to wait for browser callback

# ── Design tokens (mirrored from app.py) ───────────────────────────────
_BG_DEEP      = "#FF000000"
_BG_PANEL     = "#FF080808"
_BG_SURFACE   = "#FF111118"
_BG_INPUT     = "#FF16161F"
_CHIP_BG      = "#FF1A1A22"
_BORDER_LIGHT = "#FF3D4455"
_ACCENT       = "#FF4F8BFF"
_ACCENT_HALO  = "#FF2A3F5C"
_SUCCESS      = "#FF00D084"
_WARNING      = "#FFFFA500"
_ERROR_COL    = "#FFFF4444"
_TEXT_PRIMARY = "#FFF2F6FF"
_TEXT_MUTED   = "#FF7A8499"
_TEXT_DIM     = "#FF3D4455"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §1  LOW-LEVEL CREDENTIAL I/O
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def read_browser_json() -> dict:
    """Return the parsed browser.json, or {} if missing/invalid."""
    if not BROWSER_JSON.exists():
        return {}
    try:
        return json.loads(BROWSER_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_browser_json(authorization: str, cookie: str) -> None:
    """
    Write a spec-compliant browser.json, injecting only Authorization
    and Cookie while keeping all fixed fields in exact order.
    """
    data = {
        "Accept":          BROWSER_JSON_FIXED["Accept"],
        "Authorization":   authorization.strip(),
        "Content-Type":    BROWSER_JSON_FIXED["Content-Type"],
        "X-Goog-AuthUser": BROWSER_JSON_FIXED["X-Goog-AuthUser"],
        "x-origin":        BROWSER_JSON_FIXED["x-origin"],
        "Cookie":          cookie.strip(),
    }
    BROWSER_JSON.write_text(
        json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8"
    )


def read_env_values() -> dict[str, str]:
    """Return current .env values (empty string if key absent)."""
    raw = dotenv_values(str(ENV_FILE)) if ENV_FILE.exists() else {}
    return {k: raw.get(k, "") for k in ENV_KEYS_ALL}


def write_env_values(values: dict[str, str]) -> None:
    """
    Upsert keys in .env, maintaining the required comment headers and order.
    Creates the file if it does not exist.
    """
    if not ENV_FILE.exists():
        ENV_FILE.write_text(
            "# SPOTIFY — PKCE (Public Client, sin CLIENT_SECRET)\n"
            + "\n".join(f'{k}=""' for k in ENV_KEYS_SPOTIFY)
            + "\n\n# APPLE MUSIC\n"
            + "\n".join(f'{k}=""' for k in ENV_KEYS_APPLE)
            + "\n",
            encoding="utf-8",
        )
    for key, val in values.items():
        if key in ENV_KEYS_ALL:
            set_key(str(ENV_FILE), key, val, quote_mode="always")
    # Hot-reload into the running process's os.environ
    load_dotenv(str(ENV_FILE), override=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §2  PRE-FLIGHT VALIDATORS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AuthFailureCode:
    """Códigos de sesión para UI / diagnóstico (Global Auth Check)."""
    YT_EXPIRED      = "YT_EXPIRED"
    SPOTIFY_EXPIRED = "SPOTIFY_EXPIRED"
    APPLE_EXPIRED   = "APPLE_EXPIRED"


class PreFlightResult:
    """Holds the outcome of a single platform pre-flight check."""

    def __init__(self, platform: str):
        self.platform = platform
        self.ok       = False
        self.error    = ""
        self.expired  = False
        self.code     = ""

    def __repr__(self) -> str:
        status = "OK" if self.ok else f"FAIL({'EXPIRED' if self.expired else self.error[:30]})"
        return f"<PreFlight {self.platform}: {status}>"


def _preflight_youtube() -> PreFlightResult:
    r  = PreFlightResult("YouTube Music")
    bj = read_browser_json()
    if not bj.get("Authorization") or not bj.get("Cookie"):
        r.error   = "browser.json: falta Authorization o Cookie"
        r.expired = True
        return r
    if not bj.get("Authorization", "").startswith("SAPISIDHASH"):
        r.error   = "Authorization no comienza con 'SAPISIDHASH'"
        r.expired = True
        return r
    try:
        from ytmusicapi import YTMusic  # pylint: disable=import-outside-toplevel
        ytm = YTMusic(str(BROWSER_JSON))
        ytm.get_history()
        r.ok = True
    except Exception as exc:  # pylint: disable=broad-exception-caught
        msg       = str(exc).lower()
        r.code    = AuthFailureCode.YT_EXPIRED
        r.expired = True
        r.error   = (
            "401 — token expirado o inválido"
            if any(k in msg for k in ("401", "unauthorized", "sign in", "cookie", "parse"))
            else str(exc)[:200]
        )
    return r


def _preflight_spotify() -> PreFlightResult:
    """
    Pre-flight de Spotify v5.0 — PKCE (Public Client).

    Verifica que CLIENT_ID está configurado y que el token cacheado
    (.spotify_cache) es válido o puede refrescarse.
    Si no hay cache, expired=True → el usuario inicia el flujo desde el Wizard.
    """
    r            = PreFlightResult("Spotify")
    env          = read_env_values()
    client_id    = env.get("SPOTIFY_CLIENT_ID", "").strip()
    redirect_uri = (
        env.get("SPOTIFY_REDIRECT_URI", SPOTIFY_REDIRECT_URI).strip()
        or SPOTIFY_REDIRECT_URI
    )

    if not client_id:
        r.expired = True
        r.code    = AuthFailureCode.SPOTIFY_EXPIRED
        r.error   = "SPOTIFY_CLIENT_ID no configurado en .env"
        return r

    try:
        from spotipy.oauth2 import SpotifyPKCE           # pylint: disable=import-outside-toplevel
        from cache_handler import CacheFileHandler        # pylint: disable=import-outside-toplevel

        cache_handler = CacheFileHandler(cache_path=str(BASE_DIR / ".spotify_cache"))
        oauth = SpotifyPKCE(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope="playlist-modify-public playlist-modify-private user-library-read",
            cache_handler=cache_handler,
            open_browser=False,
        )
        token_info = oauth.get_cached_token()
        if not token_info:
            r.expired = True
            r.code    = AuthFailureCode.SPOTIFY_EXPIRED
            r.error   = "Sin token cacheado — es necesario autenticarse con Spotify"
            return r

        if oauth.is_token_expired(token_info):
            token_info = oauth.refresh_access_token(token_info["refresh_token"])

        resp = requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token_info['access_token']}"},
            timeout=8,
        )
        if resp.status_code == 401:
            r.expired = True
            r.code    = AuthFailureCode.SPOTIFY_EXPIRED
            r.error   = "401 — token expirado o scopes insuficientes"
            return r
        if resp.status_code == 200:
            r.ok = True
        else:
            r.error = f"Spotify /v1/me devolvió HTTP {resp.status_code}"

    except Exception as exc:  # pylint: disable=broad-exception-caught
        msg       = str(exc).lower()
        r.code    = AuthFailureCode.SPOTIFY_EXPIRED
        r.expired = True
        r.error   = (
            "401 — token expirado o scopes insuficientes"
            if any(k in msg for k in ("401", "unauthorized", "token", "expired"))
            else str(exc)[:200]
        )
    return r


def _preflight_apple() -> PreFlightResult:
    r      = PreFlightResult("Apple Music")
    env    = read_env_values()
    bearer = env.get("APPLE_AUTH_BEARER", "").strip()
    utok   = env.get("APPLE_MUSIC_USER_TOKEN", "").strip()

    if not bearer or not utok:
        r.error = "APPLE_AUTH_BEARER or APPLE_MUSIC_USER_TOKEN missing"
        return r

    full_bearer = bearer if bearer.startswith("Bearer ") else f"Bearer {bearer}"
    hdrs = {
        "Authorization":            full_bearer,
        "media-user-token":         utok,
        "x-apple-music-user-token": utok,
        "Origin":  "https://music.apple.com",
        "Referer": "https://music.apple.com/",
        "Accept":  "application/json",
    }
    try:
        resp = requests.get(
            "https://amp-api.music.apple.com/v1/me/storefront",
            headers=hdrs, timeout=8,
        )
        if resp.status_code == 401:
            r.expired = True
            r.code    = AuthFailureCode.APPLE_EXPIRED
            r.error   = "401 — Apple Music token expired"
            return r
        if resp.status_code != 200:
            r.error = f"Unexpected HTTP {resp.status_code}"
            return r
        sf  = resp.json().get("data", [{}])[0].get("id", "us")
        cat = requests.get(
            f"https://api.music.apple.com/v1/catalog/{sf}/search",
            params={"term": "a", "types": "songs", "limit": 1},
            headers=hdrs,
            timeout=8,
        )
        if cat.status_code == 401:
            r.expired = True
            r.code    = AuthFailureCode.APPLE_EXPIRED
            r.error   = "401 — catálogo rechazó media-user-token"
            return r
        if cat.status_code == 200:
            r.ok = True
        else:
            r.code  = AuthFailureCode.APPLE_EXPIRED
            r.error = f"Catálogo HTTP {cat.status_code}"
    except requests.RequestException as exc:
        r.error = str(exc)
    return r


def auth_failure_tooltip(r: PreFlightResult) -> str:
    """Texto para Tooltip en la barra superior (sesión caída)."""
    if r.ok:
        return ""
    hints = {
        "YouTube Music": "browser.json: Cookie + Authorization (SAPISIDHASH)",
        "Spotify":       ".env: SPOTIFY_CLIENT_ID + SPOTIFY_REDIRECT_URI (PKCE)",
        "Apple Music":   ".env: APPLE_AUTH_BEARER + APPLE_MUSIC_USER_TOKEN",
    }
    tag = f"[{r.code}] " if r.code else ""
    return f"{tag}{hints.get(r.platform, r.platform)} · {r.error}"[:500]


async def run_preflight() -> list[PreFlightResult]:
    """
    Run all three pre-flight checks in parallel using asyncio.gather().
    Returns [yt_result, sp_result, am_result].
    """
    results = await asyncio.gather(
        asyncio.to_thread(_preflight_youtube),
        asyncio.to_thread(_preflight_spotify),
        asyncio.to_thread(_preflight_apple),
        return_exceptions=True,
    )
    out: list[PreFlightResult] = []
    platforms = ["YouTube Music", "Spotify", "Apple Music"]
    for plat, res in zip(platforms, results):
        if isinstance(res, Exception):
            r       = PreFlightResult(plat)
            r.error = str(res)
            out.append(r)
        else:
            out.append(res)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §3  SPOTIFY OAUTH — LOCAL CALLBACK SERVER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_HTML_SUCCESS = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<title>MelomaniacPass — Login exitoso</title>"
    "<style>body{font-family:sans-serif;background:#000;color:#f2f6ff;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
    "div{text-align:center} h2{color:#00d084} p{color:#7a8499}</style></head>"
    "<body><div>"
    "<h2>&#10003; Login con Spotify exitoso</h2>"
    "<p>Puedes cerrar esta ventana y volver a MelomaniacPass.</p>"
    "</div></body></html>"
).encode()

_HTML_ERROR = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<title>MelomaniacPass — Error de autenticación</title>"
    "<style>body{font-family:sans-serif;background:#000;color:#f2f6ff;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
    "div{text-align:center} h2{color:#ff4444} p{color:#7a8499}</style></head>"
    "<body><div>"
    "<h2>&#10007; Error de autenticación</h2>"
    "<p>Cierra esta ventana y vuelve a intentarlo desde MelomaniacPass.</p>"
    "</div></body></html>"
).encode()


class _OAuthCallbackServer:
    """
    Minimal single-shot HTTP server that captures the Spotify OAuth callback.

    Listens on 127.0.0.1:8080, waits for GET /callback?code=… and signals
    completion via a threading.Event so the caller can await asynchronously.
    """

    def __init__(self) -> None:
        self.auth_code: Optional[str] = None
        self.error:     Optional[str] = None
        self._done   = threading.Event()
        self._server: Optional[http.server.HTTPServer] = None

    def start(self) -> None:
        """Start the background listener thread."""
        server_ref = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # pylint: disable=invalid-name
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                if "code" in params:
                    server_ref.auth_code = params["code"][0]
                    body = _HTML_SUCCESS
                elif "error" in params:
                    server_ref.error = params.get("error", ["unknown"])[0]
                    body = _HTML_ERROR
                else:
                    self.send_response(200)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                server_ref._done.set()

            def log_message(self, *_args):
                pass  # silence access log

        try:
            self._server = http.server.HTTPServer(
                ("127.0.0.1", SPOTIFY_CALLBACK_PORT), _Handler
            )
            self._server.timeout = 1.0
        except OSError as exc:
            raise RuntimeError(
                f"No se pudo abrir el puerto {SPOTIFY_CALLBACK_PORT} para el callback "
                f"OAuth. Asegúrate de que ningún otro proceso lo esté usando. Detalle: {exc}"
            ) from exc

        thread = threading.Thread(
            target=self._serve, daemon=True, name="spotify-oauth-cb"
        )
        thread.start()

    def wait(self, timeout: float = SPOTIFY_OAUTH_TIMEOUT) -> bool:
        """Block until callback received or timeout. Returns True on success."""
        return self._done.wait(timeout=timeout)

    def stop(self) -> None:
        self._done.set()
        if self._server:
            try:
                self._server.server_close()
            except OSError:
                pass

    def _serve(self) -> None:
        assert self._server is not None
        while not self._done.is_set():
            self._server.handle_request()
        try:
            self._server.server_close()
        except OSError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §4  FLET CONFIGURATION WIZARD
#     Tab 0 — YouTube Music  : fully editable  (browser.json)
#     Tab 1 — Spotify        : read-only status + OAuth connect button
#     Tab 2 — Apple Music    : fully editable  (.env)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfigWizard:
    """
    Flet overlay dialog for platform credential management.

    YouTube Music and Apple Music panels are fully editable as before.
    The Spotify panel is read-only: it shows the current auth status and
    provides a single "Conectar con Spotify" button that opens the system
    browser to complete the OAuth Authorization Code Flow.

    "Guardar y Aplicar" only writes YouTube Music (browser.json) and
    Apple Music (.env). Spotify credentials are never written from here.
    """

    # Platform name → panel index
    _PLATFORM_INDEX = {
        "YouTube Music": 0,
        "Spotify":       1,
        "Apple Music":   2,
    }

    def __init__(
        self,
        page: ft.Page,
        auth_manager: "AuthManager",
        on_saved: Optional[Callable[[], None]] = None,
    ) -> None:
        self.page          = page
        self._auth_manager = auth_manager
        self.on_saved      = on_saved

        self._dlg: Optional[ft.AlertDialog]    = None
        self._tab_panels:  list[ft.Container]  = []
        self._tab_buttons: list[ft.Container]  = []
        self._panel_holder: Optional[ft.Container] = None
        self._failed_platforms: set[str]       = set()
        self._active_tab_idx: int              = 0
        self._is_saving: bool                  = False

        # YouTube Music field refs (editable)
        self._yt_auth:   Optional[ft.TextField] = None
        self._yt_cookie: Optional[ft.TextField] = None

        # Apple Music field refs (editable)
        self._am_fields: dict[str, ft.TextField] = {}

        # Spotify panel control refs (read-only status)
        self._sp_status_icon:  Optional[ft.Icon]         = None
        self._sp_status_label: Optional[ft.Text]         = None
        self._sp_connect_btn:  Optional[ft.TextButton]   = None
        self._sp_spinner:      Optional[ft.ProgressRing] = None

    # ── Dialog lifecycle ───────────────────────────────────────────────

    def _show_dialog(self, dlg: ft.AlertDialog) -> None:
        self.page.show_dialog(dlg)

    def _dismiss_dialog(self, dlg: ft.AlertDialog) -> None:
        if dlg is None:
            return
        try:
            ds = getattr(self.page, "_dialogs", None)
            if ds is not None and dlg in ds.controls:
                top_open = next(
                    (d for d in reversed(ds.controls) if getattr(d, "open", False)),
                    None,
                )
                if top_open is dlg:
                    self.page.pop_dialog()
                else:
                    dlg.open = False
                    dlg.update()
                    self.page.update()
            else:
                dlg.open = False
                dlg.update()
                self.page.update()
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"[ConfigWizard] No se pudo cerrar el diálogo: {e}")

    def _safe_dialog_update(self) -> None:
        try:
            if self._dlg is not None and getattr(self._dlg, "open", False):
                self._dlg.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    # ── Tab management ─────────────────────────────────────────────────

    def _resolve_initial_tab(
        self, failed_platforms: set[str], initial_platform: Optional[str]
    ) -> int:
        if initial_platform and initial_platform in self._PLATFORM_INDEX:
            return self._PLATFORM_INDEX[initial_platform]
        if failed_platforms:
            order = ["YouTube Music", "Spotify", "Apple Music"]
            return next(
                (self._PLATFORM_INDEX[p] for p in order if p in failed_platforms), 0
            )
        return 0

    def _apply_tab_selection(self, idx: int) -> None:
        if not self._tab_panels or not self._tab_buttons:
            return
        idx = max(0, min(idx, len(self._tab_panels) - 1))
        self._active_tab_idx = idx
        if self._panel_holder is not None:
            self._panel_holder.content = self._tab_panels[idx]
        tab_order = ["YouTube Music", "Spotify", "Apple Music"]
        for i, btn in enumerate(self._tab_buttons):
            is_warn      = tab_order[i] in self._failed_platforms
            col_active   = _WARNING if is_warn else _TEXT_PRIMARY
            col_inactive = _WARNING if is_warn else _TEXT_MUTED
            btn.bgcolor  = "#14FFFFFF" if i == idx else "transparent"
            row = btn.content
            row.controls[0].color  = col_active   if i == idx else col_inactive
            row.controls[1].color  = col_active   if i == idx else col_inactive
            row.controls[1].weight = (
                ft.FontWeight.W_600 if i == idx else ft.FontWeight.W_400
            )
        self._safe_dialog_update()
        try:
            self.page.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _on_tab_click(self, e: ft.ControlEvent) -> None:
        try:
            idx = int(getattr(e.control, "data", "0"))
        except (TypeError, ValueError):
            idx = 0
        self._apply_tab_selection(idx)

    def _make_tab_btn(
        self,
        idx: int,
        label: str,
        icon_ok: str,
        icon_warn: str,
        platform: str,
    ) -> ft.Container:
        warn  = platform in self._failed_platforms
        icon  = icon_warn if warn else icon_ok
        color = _WARNING if warn else (
            _TEXT_PRIMARY if idx == self._active_tab_idx else _TEXT_MUTED
        )
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(icon, color=color, size=13),
                    ft.Text(
                        label, size=11, color=color,
                        font_family="IBM Plex Sans",
                        weight=(
                            ft.FontWeight.W_600
                            if idx == self._active_tab_idx else ft.FontWeight.W_400
                        ),
                    ),
                ],
                spacing=6, tight=True,
            ),
            bgcolor="#14FFFFFF" if idx == self._active_tab_idx else "transparent",
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            data=str(idx),
            ink=True,
            on_click=self._on_tab_click,
        )

    # ── Action handlers ────────────────────────────────────────────────

    def _on_close_click(self, _e: ft.ControlEvent) -> None:
        self._close_wizard()

    def _on_save_click(self, _e: ft.ControlEvent) -> None:
        if self._is_saving:
            return
        self._is_saving = True

        async def _save_and_close() -> None:
            try:
                await asyncio.to_thread(self._apply_save)
                self._close_wizard()
                if self.on_saved:
                    self.on_saved()
            except Exception as ex:  # pylint: disable=broad-exception-caught
                print(f"[ConfigWizard] Error al guardar: {ex}")
            finally:
                self._is_saving = False

        asyncio.create_task(_save_and_close())

    # ── Public API ─────────────────────────────────────────────────────

    def open(
        self,
        results: Optional[list[PreFlightResult]] = None,
        initial_platform: Optional[str] = None,
    ) -> None:
        """
        Show the wizard.
        • Highlights failed platforms if *results* is supplied.
        • If *initial_platform* is given, that tab is shown first.
        """
        if self._dlg is not None and getattr(self._dlg, "open", False):
            if initial_platform and initial_platform in self._PLATFORM_INDEX:
                self._apply_tab_selection(self._PLATFORM_INDEX[initial_platform])
            return

        if self._dlg is not None:
            try:
                self._dismiss_dialog(self._dlg)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            self._dlg = None

        self._tab_panels       = []
        self._tab_buttons      = []
        self._panel_holder     = None
        self._failed_platforms = set()
        self._active_tab_idx   = 0

        if results:
            for r in results:
                if not r.ok:
                    self._failed_platforms.add(r.platform)

        _initial_idx = self._resolve_initial_tab(self._failed_platforms, initial_platform)
        self._active_tab_idx = _initial_idx

        sp_result = next(
            (r for r in (results or []) if r.platform == "Spotify"), None
        )

        panels = [
            self._panel_youtube(warn="YouTube Music" in self._failed_platforms),
            self._panel_spotify(result=sp_result),
            self._panel_apple(warn="Apple Music" in self._failed_platforms),
        ]
        self._tab_panels   = panels
        self._panel_holder = ft.Container(content=panels[_initial_idx], expand=True)

        TAB_LABELS = [
            ("YouTube Music", ft.Icons.MUSIC_VIDEO,  ft.Icons.WARNING_AMBER_ROUNDED, "YouTube Music"),
            ("Spotify",       ft.Icons.MUSIC_NOTE,   ft.Icons.WARNING_AMBER_ROUNDED, "Spotify"),
            ("Apple Music",   ft.Icons.APPLE,        ft.Icons.WARNING_AMBER_ROUNDED, "Apple Music"),
        ]
        self._tab_buttons = [
            self._make_tab_btn(i, lbl, ico_ok, ico_warn, plat)
            for i, (lbl, ico_ok, ico_warn, plat) in enumerate(TAB_LABELS)
        ]

        body = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Row(controls=self._tab_buttons, spacing=4),
                    bgcolor="#08FFFFFF",
                    border_radius=10,
                    padding=ft.Padding.all(4),
                    border=ft.Border.all(0.8, "#14FFFFFF"),
                ),
                ft.Container(
                    content=self._panel_holder,
                    expand=True,
                    bgcolor=_BG_SURFACE,
                    border_radius=8,
                    padding=ft.Padding.all(0),
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                ),
            ],
            spacing=8,
            expand=True,
        )

        self._dlg = ft.AlertDialog(
            modal=True,
            scrollable=False,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            title=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.SETTINGS, color=_ACCENT, size=18),
                    ft.Text(
                        "Configuración de Credenciales",
                        size=14, weight=ft.FontWeight.W_700,
                        color=_TEXT_PRIMARY, font_family="IBM Plex Sans",
                    ),
                ],
                spacing=8,
            ),
            content=ft.Container(
                content=body,
                width=620,
                height=480,
                bgcolor=_BG_SURFACE,
                border_radius=10,
                padding=ft.Padding.all(8),
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ),
            actions=[
                ft.TextButton(
                    "Guardar y Aplicar",
                    icon=ft.Icons.SAVE_OUTLINED,
                    on_click=self._on_save_click,
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: _ACCENT}),
                ),
                ft.TextButton(
                    "Cerrar",
                    on_click=self._on_close_click,
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: _TEXT_MUTED}),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=_BG_PANEL,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        self._apply_tab_selection(_initial_idx)
        self._show_dialog(self._dlg)

    def _close_wizard(self) -> None:
        try:
            if self._dlg is not None:
                self._dismiss_dialog(self._dlg)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        finally:
            self._dlg = None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Panel builders
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _instructions_box(steps: list[tuple[str, str]]) -> ft.Container:
        """Renders a numbered instruction box above the credential fields."""
        def _step(num: int, label: str, body: str) -> ft.Row:
            return ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Text(
                            str(num), size=10, color=_ACCENT,
                            font_family="IBM Plex Sans", weight=ft.FontWeight.W_700,
                        ),
                        bgcolor="#18FFFFFF",
                        border_radius=20,
                        width=20, height=20,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(
                                label, size=11, color=_TEXT_PRIMARY,
                                font_family="IBM Plex Sans", weight=ft.FontWeight.W_700,
                            ),
                            ft.Text(
                                body, size=11, color=_TEXT_MUTED,
                                font_family="IBM Plex Sans",
                            ),
                        ],
                        spacing=1, tight=True, expand=True,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )

        rows = [_step(i + 1, lbl, txt) for i, (lbl, txt) in enumerate(steps)]
        return ft.Container(
            content=ft.Column(rows, spacing=8),
            bgcolor="#0AFFFFFF",
            border=ft.Border.all(0.8, "#14FFFFFF"),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        )

    # ── Tab 0: YouTube Music (editable) ───────────────────────────────

    def _panel_youtube(self, warn: bool = False) -> ft.Container:
        bj = read_browser_json()
        self._yt_auth = ft.TextField(
            label="Authorization (SAPISIDHASH …)",
            value=bj.get("Authorization", ""),
            multiline=True, min_lines=2, max_lines=3,
            **self._field_style(),
        )
        self._yt_cookie = ft.TextField(
            label="Cookie",
            value=bj.get("Cookie", ""),
            multiline=True, min_lines=4, max_lines=6,
            **self._field_style(),
        )
        hint = (
            self._warn_banner(
                "Token expirado. Actualiza Authorization y Cookie desde "
                "music.youtube.com → DevTools → Network."
            ) if warn else ft.Container(height=0)
        )
        instructions = self._instructions_box([
            ("Abre YouTube Music y pulsa F12",
             "Ve a la pestaña Network en DevTools."),
            ("Filtra por \"browse\"",
             "Escribe browse en la barra de filtro de Network."),
            ("Selecciona el POST de mayor peso",
             "Busca una solicitud con método POST (habitualmente browsing o browse)."),
            ("Extrae Authorization",
             "En Headers → Request Headers copia el valor completo de Authorization "
             "(empieza con SAPISIDHASH …)."),
            ("Extrae Cookie",
             "En la misma solicitud copia el valor completo del header Cookie."),
        ])
        return ft.Container(
            content=ft.Column(
                controls=[
                    hint,
                    instructions,
                    self._section("BROWSER.JSON — CAMPOS VARIABLES"),
                    self._yt_auth,
                    self._yt_cookie,
                    self._fixed_note(
                        "Los campos fijos (Accept, Content-Type, X-Goog-AuthUser, x-origin) "
                        "se escriben automáticamente."
                    ),
                ],
                spacing=10,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=ft.Padding.all(12),
            expand=True,
        )

    # ── Tab 1: Spotify (read-only status + OAuth connect) ─────────────

    def _panel_spotify(self, result: Optional[PreFlightResult] = None) -> ft.Container:
        """
        Read-only Spotify panel.

        Shows the current authentication status and a single
        "Conectar con Spotify" button that starts the official OAuth
        Authorization Code Flow via the system browser.

        No credential TextFields — CLIENT_ID / REDIRECT_URI
        must be configured directly in .env before pressing Connect.
        """
        # Status badge
        if result is None or (not result.ok and not result.expired):
            ico_name  = ft.Icons.HELP_OUTLINE
            ico_color = _TEXT_DIM
            lbl_text  = "Estado desconocido"
            lbl_color = _TEXT_DIM
        elif result.ok:
            ico_name  = ft.Icons.CHECK_CIRCLE_OUTLINE
            ico_color = _SUCCESS
            lbl_text  = "Conectado y autenticado"
            lbl_color = _SUCCESS
        else:
            ico_name  = ft.Icons.LOCK_CLOCK_OUTLINED
            ico_color = _WARNING
            lbl_text  = "Sin token — requiere autenticación"
            lbl_color = _WARNING

        self._sp_status_icon  = ft.Icon(ico_name, color=ico_color, size=20)
        self._sp_status_label = ft.Text(
            lbl_text, size=13, color=lbl_color,
            font_family="IBM Plex Sans", weight=ft.FontWeight.W_600,
        )
        status_box = ft.Container(
            content=ft.Row(
                controls=[self._sp_status_icon, self._sp_status_label],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor="#0AFFFFFF",
            border=ft.Border.all(0.8, "#14FFFFFF"),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=14, vertical=12),
        )

        # Error detail row (only when there is a specific message)
        extra: list[ft.Control] = []
        if result and not result.ok and result.error:
            extra.append(
                ft.Container(
                    content=ft.Text(
                        result.error[:160] + ("…" if len(result.error) > 160 else ""),
                        size=10, color=_TEXT_DIM, font_family="IBM Plex Sans",
                    ),
                    bgcolor="#06FFFFFF",
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                )
            )

        # Spinner + connect button
        self._sp_spinner = ft.ProgressRing(
            width=16, height=16, stroke_width=2,
            color=_ACCENT, visible=False,
        )
        needs_connect = (result is None) or (not result.ok)
        self._sp_connect_btn = ft.TextButton(
            "Conectar con Spotify",
            icon=ft.Icons.OPEN_IN_BROWSER_OUTLINED,
            on_click=self._on_spotify_connect,
            disabled=not needs_connect,
            style=ft.ButtonStyle(
                color={ft.ControlState.DEFAULT: _ACCENT},
                bgcolor={
                    ft.ControlState.DEFAULT:  "#0A4F8BFF",
                    ft.ControlState.HOVERED:  "#1A4F8BFF",
                    ft.ControlState.DISABLED: "transparent",
                },
                shape=ft.RoundedRectangleBorder(radius=10),
                padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            ),
        )
        connect_row = ft.Row(
            controls=[self._sp_spinner, self._sp_connect_btn],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        how_to = self._instructions_box([
            ("Configura .env antes de conectar",
             f"SPOTIFY_CLIENT_ID y "
             f"SPOTIFY_REDIRECT_URI={SPOTIFY_REDIRECT_URI} deben estar en .env."),
            ("Pulsa «Conectar con Spotify»",
             "Se abrirá el navegador con la página de autorización de Spotify."),
            ("Acepta los permisos en el navegador",
             "MelomaniacPass recibirá el token automáticamente en el callback."),
            ("Siguientes arranques — automático",
             "El token se guarda en .spotify_cache y se renueva solo. "
             "No necesitas repetir el proceso."),
        ])

        read_only_note = self._fixed_note(
            "Las credenciales de Spotify (CLIENT_ID) se configuran "
            "directamente en el archivo .env y no son editables desde esta interfaz."
        )

        return ft.Container(
            content=ft.Column(
                controls=[status_box, *extra, how_to, read_only_note, connect_row],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=ft.Padding.all(12),
            expand=True,
        )

    def _on_spotify_connect(self, _e: ft.ControlEvent) -> None:
        asyncio.create_task(self._do_spotify_oauth())

    async def _do_spotify_oauth(self) -> None:
        self._set_spotify_loading(True)
        try:
            ok = await self._auth_manager.start_spotify_oauth_flow()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            ok = False
            self._auth_manager.state_log_fn(f"[ERROR] OAuth Spotify: {exc}")
        finally:
            self._set_spotify_loading(False)

        self._update_spotify_status(
            ok=ok,
            error="No se completó la autenticación" if not ok else "",
        )
        if ok:
            # Refresh global pre-flight so the header icons update
            results = await self._auth_manager.check_all_sessions()
            self._auth_manager.ingest_preflight_results(results)

    def _set_spotify_loading(self, loading: bool) -> None:
        if self._sp_spinner is not None:
            self._sp_spinner.visible = loading
        if self._sp_connect_btn is not None:
            self._sp_connect_btn.disabled = loading
        self._safe_dialog_update()

    def _update_spotify_status(self, ok: bool, error: str = "") -> None:
        if self._sp_status_icon is not None:
            self._sp_status_icon.name  = (
                ft.Icons.CHECK_CIRCLE_OUTLINE if ok else ft.Icons.CANCEL_OUTLINED
            )
            self._sp_status_icon.color = _SUCCESS if ok else _ERROR_COL
        if self._sp_status_label is not None:
            self._sp_status_label.value = (
                "Conectado y autenticado" if ok else (error or "Error de autenticación")
            )
            self._sp_status_label.color = _SUCCESS if ok else _ERROR_COL
        if self._sp_connect_btn is not None:
            self._sp_connect_btn.disabled = ok  # disable once successfully connected
        self._safe_dialog_update()

    # ── Tab 2: Apple Music (editable) ─────────────────────────────────

    def _panel_apple(self, warn: bool = False) -> ft.Container:
        env = read_env_values()
        self._am_fields = {}
        controls: list[ft.Control] = []

        if warn:
            controls.append(self._warn_banner(
                "Token expirado. Actualiza APPLE_AUTH_BEARER y APPLE_MUSIC_USER_TOKEN."
            ))

        controls.append(self._instructions_box([
            ("Abre Apple Music Web y pulsa F12",
             "Ve a music.apple.com y abre DevTools."),
            ("Filtra en Network por \"catalog\"",
             "Escribe catalog en la barra de filtro de la pestaña Network."),
            ("Selecciona el GET de mayor peso",
             "Abre la solicitud GET más pesada y ve a Headers → Request Headers."),
            ("Extrae Authorization (Bearer)",
             "Copia el valor completo de Authorization (Bearer eyJ…) "
             "y pégalo en APPLE_AUTH_BEARER."),
            ("Extrae Media-User-Token",
             "Copia el valor del header media-user-token (o x-apple-music-user-token) "
             "y pégalo en APPLE_MUSIC_USER_TOKEN."),
        ]))

        controls.append(self._section("APPLE MUSIC — .env"))
        for key in ENV_KEYS_APPLE:
            tf = ft.TextField(
                label=key,
                value=env.get(key, ""),
                password=True,
                can_reveal_password=True,
                **self._field_style(),
            )
            self._am_fields[key] = tf
            controls.append(tf)

        controls.append(self._fixed_note(
            'APPLE_AUTH_BEARER puede tener o no el prefijo "Bearer "; '
            "la app lo normaliza automáticamente."
        ))
        return ft.Container(
            content=ft.Column(controls, spacing=10, scroll=ft.ScrollMode.AUTO),
            padding=ft.Padding.all(12),
            expand=True,
        )

    # ── Save logic (YouTube Music + Apple Music only) ──────────────────

    def _apply_save(self) -> None:
        # browser.json — YouTube Music
        if self._yt_auth and self._yt_cookie:
            write_browser_json(
                self._yt_auth.value   or "",
                self._yt_cookie.value or "",
            )
        # .env — Apple Music only (Spotify credentials are never written from here)
        am_vals = {k: tf.value or "" for k, tf in self._am_fields.items()}
        if am_vals:
            write_env_values(am_vals)

    # ── UI helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _field_style() -> dict:
        return {
            "bgcolor":              "#08FFFFFF",
            "border_color":        "#18FFFFFF",
            "focused_border_color": _ACCENT,
            "label_style":  ft.TextStyle(color=_TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            "text_style":   ft.TextStyle(color=_TEXT_PRIMARY, size=11, font_family="IBM Plex Sans"),
            "border_radius": 8,
        }

    @staticmethod
    def _section(text: str) -> ft.Text:
        return ft.Text(
            text, size=8, color=_TEXT_DIM,
            font_family="IBM Plex Sans", weight=ft.FontWeight.W_700,
            style=ft.TextStyle(letter_spacing=1.2),
        )

    @staticmethod
    def _fixed_note(text: str) -> ft.Container:
        return ft.Container(
            content=ft.Text(text, size=9, color=_TEXT_DIM, font_family="IBM Plex Sans"),
            bgcolor="#06FFFFFF",
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )

    @staticmethod
    def _warn_banner(text: str) -> ft.Container:
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=_WARNING, size=14),
                    ft.Text(
                        text, size=10, color=_WARNING,
                        font_family="IBM Plex Sans", expand=True,
                    ),
                ],
                spacing=6,
            ),
            bgcolor="#120C0000",
            border=ft.Border.all(0.8, "#30FFA500"),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §5  AUTH MANAGER  (service-level coordinator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AuthManager:
    """
    High-level auth coordinator used by app.py.

    1. run_startup_check()        — parallel pre-flight on all three platforms.
    2. start_spotify_oauth_flow() — OAuth browser flow for Spotify.
    3. open_wizard(platform)      — opens ConfigWizard (routes to correct tab).
    4. refresh_session_icons()    — revalidates and updates UI icons.
    5. reload_credentials()       — hot-reloads .env / browser.json and re-inits services.
    """

    def __init__(self, page: ft.Page, service, state) -> None:
        self.page    = page
        self.service = service

        # Accept AppState or a bound _log method (compatibility)
        if hasattr(state, "notify"):
            self.state        = state
            self.state_log_fn = state._log
        elif callable(state) and getattr(state, "__self__", None) is not None \
                and hasattr(state.__self__, "notify"):
            self.state        = state.__self__
            self.state_log_fn = state
        else:
            raise TypeError(
                "AuthManager(page, service, state): el tercer argumento debe ser "
                "el objeto AppState (p. ej. state), no state._log ni otro valor."
            )

        self._wizard = ConfigWizard(
            page, auth_manager=self, on_saved=self._on_wizard_saved
        )
        self._last_results: list[PreFlightResult] = []
        self._reload_task:  Optional[asyncio.Task] = None  # tracked for hard_cleanup

    # ── Spotify OAuth ──────────────────────────────────────────────────

    async def start_spotify_oauth_flow(self) -> bool:
        """
        Full PKCE Authorization Flow for Spotify.

        1. Reads CLIENT_ID / REDIRECT_URI from .env.
        2. Opens the system browser to Spotify's authorization page.
        3. Starts _OAuthCallbackServer on 127.0.0.1:8080.
        4. Awaits callback (max SPOTIFY_OAUTH_TIMEOUT seconds).
        5. Exchanges code for token → saved in .spotify_cache.
        6. Returns True on success.
        """
        env          = read_env_values()
        client_id    = env.get("SPOTIFY_CLIENT_ID", "").strip()
        redirect_uri = (
            env.get("SPOTIFY_REDIRECT_URI", SPOTIFY_REDIRECT_URI).strip()
            or SPOTIFY_REDIRECT_URI
        )

        if not client_id:
            self.state_log_fn(
                "[ERROR] PKCE Spotify: SPOTIFY_CLIENT_ID no configurado en .env"
            )
            return False

        try:
            from spotipy.oauth2 import SpotifyPKCE           # pylint: disable=import-outside-toplevel
            from cache_handler import CacheFileHandler        # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            self.state_log_fn(f"[ERROR] PKCE Spotify — import: {exc}")
            return False

        cache_path    = str(BASE_DIR / ".spotify_cache")
        cache_handler = CacheFileHandler(cache_path=cache_path)
        oauth = SpotifyPKCE(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope="playlist-modify-public playlist-modify-private user-library-read",
            cache_handler=cache_handler,
            open_browser=False,
        )

        # Skip the flow if a valid token is already cached
        try:
            existing = oauth.get_cached_token()
            if existing and not oauth.is_token_expired(existing):
                self.state_log_fn("[INFO] OAuth Spotify: token válido ya en caché")
                return True
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # Start local callback server
        cb_server = _OAuthCallbackServer()
        try:
            cb_server.start()
        except RuntimeError as exc:
            self.state_log_fn(f"[ERROR] OAuth Spotify — servidor de callback: {exc}")
            return False

        # Open system browser
        auth_url = oauth.get_authorize_url()
        self.state_log_fn(
            "[INFO] Abriendo navegador para autenticación Spotify…\n"
            f"       Si no se abre visita: {auth_url}"
        )
        await asyncio.to_thread(webbrowser.open, auth_url)

        # Await callback
        self.state_log_fn(
            f"[INFO] Esperando callback en {redirect_uri} "
            f"(máx. {SPOTIFY_OAUTH_TIMEOUT}s)…"
        )
        received = await asyncio.to_thread(cb_server.wait, float(SPOTIFY_OAUTH_TIMEOUT))
        cb_server.stop()

        if not received or cb_server.auth_code is None:
            reason = cb_server.error or "timeout — no se recibió el código de autorización"
            self.state_log_fn(f"[ERROR] OAuth Spotify: {reason}")
            return False

        # Exchange code for token
        try:
            token_info = await asyncio.to_thread(
                oauth.get_access_token, cb_server.auth_code, check_cache=False
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.state_log_fn(f"[ERROR] OAuth Spotify — intercambio de token: {exc}")
            return False

        if not token_info:
            self.state_log_fn("[ERROR] OAuth Spotify: get_access_token devolvió vacío")
            return False

        self.state_log_fn(
            "[SUCCESS] Token de Spotify obtenido y guardado en .spotify_cache"
        )
        await self.service.init_spotify()
        return True

    # ── Pre-flight / session management ───────────────────────────────

    async def check_all_sessions(self) -> list[PreFlightResult]:
        """Parallel pre-flight on all three platforms."""
        return await run_preflight()

    def ingest_preflight_results(self, results: list[PreFlightResult]) -> None:
        """Cache results, update AppState auth flags and notify the UI."""
        self._last_results = results
        self._sync_auth_ui_state(results)
        self.state.notify()

    async def refresh_session_icons(self) -> list[PreFlightResult]:
        """Re-run check_all_sessions and push results to the UI icons."""
        results = await self.check_all_sessions()
        self.ingest_preflight_results(results)
        return results

    def _sync_auth_ui_state(self, results: list[PreFlightResult]) -> None:
        for r in results:
            self.state.auth_session_ok[r.platform]   = r.ok
            self.state.auth_session_hint[r.platform] = (
                "" if r.ok else auth_failure_tooltip(r)
            )

    async def run_startup_check(self) -> list[PreFlightResult]:
        """
        Parallel pre-flight + conditional service init.

        Spotify: if there is no valid cached token, the state is marked as
        "requires authentication" but the browser is NOT opened automatically.
        The user must click "Conectar con Spotify" in the wizard to start the
        OAuth flow.
        Other expired platforms open the wizard on their respective tab.
        """
        self.state_log_fn("[INFO] Pre-flight: verificando credenciales…")
        results = await self.check_all_sessions()
        self._last_results = results
        self._sync_auth_ui_state(results)

        need_wizard_for: list[str] = []

        for r in results:
            if r.ok:
                self.state_log_fn(f"[INFO]  ✓ {r.platform}: OK")
            elif r.expired:
                if r.platform == "Spotify" and r.code == AuthFailureCode.SPOTIFY_EXPIRED:
                    # Lazy auth: do NOT open the browser automatically.
                    # Just log so the status icon shows "Desconectado".
                    self.state_log_fn(
                        "[INFO]  ↳ Spotify: sin token cacheado — "
                        "usa el Wizard → Spotify → «Conectar con Spotify» para autenticarte."
                    )
                else:
                    need_wizard_for.append(r.platform)
                    self.state_log_fn(
                        f"[ERROR] ⚠ {r.platform}: credenciales expiradas — "
                        "actualiza las credenciales en la configuración"
                    )
            else:
                self.state_log_fn(f"[WARN]  – {r.platform}: {r.error}")

        await self._init_passing_services(results)

        if need_wizard_for:
            # Open wizard on the first failing non-Spotify platform
            first_fail = need_wizard_for[0]

            async def _open_wizard_deferred() -> None:
                await asyncio.sleep(random.uniform(2.0, 4.0))
                self.open_wizard(first_fail)

            asyncio.create_task(_open_wizard_deferred())

        return results

    async def _init_passing_services(self, results: list[PreFlightResult]) -> None:
        tasks = []
        for r in results:
            if r.ok:
                if r.platform == "Spotify":
                    tasks.append(self.service.init_spotify())
                elif r.platform == "YouTube Music":
                    tasks.append(self.service.init_youtube())
                elif r.platform == "Apple Music":
                    tasks.append(self.service.init_apple())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Wizard ─────────────────────────────────────────────────────────

    def open_wizard(self, platform: Optional[str] = None) -> None:
        """
        Open the ConfigWizard.
        If *platform* is given, the corresponding tab is shown first.
        """
        def _go() -> None:
            try:
                self._wizard.open(self._last_results or None, initial_platform=platform)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                self.state_log_fn(f"[ERROR] Wizard: {ex}")

        try:
            asyncio.get_running_loop().call_soon(_go)
        except RuntimeError:
            _go()

    def _on_wizard_saved(self) -> None:
        """Called by ConfigWizard after the user clicks 'Guardar y Aplicar'."""
        self._reload_task = asyncio.create_task(self.reload_credentials())

    # ── Hot-reload ─────────────────────────────────────────────────────

    async def reload_credentials(self) -> None:
        """
        Hot-reload credentials from disk and reinitialise all services.
        No process restart required.
        """
        self.state_log_fn("[INFO] Recargando credenciales…")
        load_dotenv(str(ENV_FILE), override=True)

        init_results = await asyncio.gather(
            self.service.init_spotify(),
            self.service.init_youtube(),
            self.service.init_apple(),
            return_exceptions=True,
        )
        for plat, res in zip(["Spotify", "YouTube Music", "Apple Music"], init_results):
            if res is True:
                self.state_log_fn(f"[SUCCESS] ✓ {plat}: reconectado")
            else:
                self.state_log_fn(f"[ERROR]   – {plat}: {res}")

        chk = await self.check_all_sessions()
        self._last_results = chk
        self._sync_auth_ui_state(chk)
        self.state.notify()

    # ── Deprecated stub ────────────────────────────────────────────────

    def get_spotify_web_token(self) -> Optional[str]:
        """DEPRECATED in v5.0 — use SpotifyPKCE + CacheFileHandler."""
        self.state_log_fn(
            "[WARN] get_spotify_web_token() está deprecado en v5.0. "
            "Usa SpotifyPKCE + CacheFileHandler (MusicApiService._sync_init_spotify)."
        )
        return None
