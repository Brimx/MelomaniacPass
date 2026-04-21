"""
auth_manager.py — MelomaniacPass v5.0 — ISRC-Master Auth
════════════════════════════════════════════════════════
Centralises ALL pre-flight validation and platform status display.

Credential contract (read-only — edit directly in .env / browser.json):
────────────────────────────────────────────────────────────────────────
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
    # SPOTIFY — Authorization Code Flow (Official v5.0)
    SPOTIFY_CLIENT_ID="<App Client ID from Developer Dashboard>"
    SPOTIFY_CLIENT_SECRET="<App Client Secret from Developer Dashboard>"
    SPOTIFY_REDIRECT_URI="http://127.0.0.1:8080/callback"

    # APPLE MUSIC
    APPLE_AUTH_BEARER="<value>"
    APPLE_MUSIC_USER_TOKEN="<value>"

The UI exposed by this module is READ-ONLY: it shows connection status for
each platform but provides no credential editing. All credentials must be
set in .env or browser.json before launching the application.

The only interactive action available in the UI is:
  • "Conectar con Spotify" — starts the official OAuth Authorization Code
    Flow: opens the system browser, runs a local callback server on
    http://127.0.0.1:8080/callback and exchanges the code for a token
    persisted in .spotify_cache.
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

# ── Required .env variable names ───────────────────────────────────────
ENV_KEYS_SPOTIFY = [
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI",
]
ENV_KEYS_APPLE = [
    "APPLE_AUTH_BEARER",
    "APPLE_MUSIC_USER_TOKEN",
]
ENV_KEYS_ALL = ENV_KEYS_SPOTIFY + ENV_KEYS_APPLE

# ── Spotify OAuth redirect URI (must match Developer Dashboard) ─────────
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8080/callback"
SPOTIFY_CALLBACK_PORT = 8080
SPOTIFY_OAUTH_TIMEOUT = 180  # seconds to wait for browser callback

# ── Design tokens (mirror app.py for visual consistency) ───────────────
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
# §1  LOW-LEVEL CREDENTIAL I/O  (read-only — no UI editing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def read_browser_json() -> dict:
    """Return the parsed browser.json, or {} if missing/invalid."""
    if not BROWSER_JSON.exists():
        return {}
    try:
        return json.loads(BROWSER_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def read_env_values() -> dict[str, str]:
    """Return current .env values (empty string if key absent)."""
    raw = dotenv_values(str(ENV_FILE)) if ENV_FILE.exists() else {}
    return {k: raw.get(k, "") for k in ENV_KEYS_ALL}


def _bootstrap_env_if_missing() -> None:
    """Create a blank .env with section comments if it does not exist yet."""
    if ENV_FILE.exists():
        return
    ENV_FILE.write_text(
        "# SPOTIFY — Authorization Code Flow (Official v5.0)\n"
        + "\n".join(f'{k}=""' for k in ENV_KEYS_SPOTIFY)
        + "\n\n# APPLE MUSIC\n"
        + "\n".join(f'{k}=""' for k in ENV_KEYS_APPLE)
        + "\n",
        encoding="utf-8",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §2  PRE-FLIGHT VALIDATORS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AuthFailureCode:
    """Códigos de sesión para UI / diagnóstico."""
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
        msg    = str(exc).lower()
        r.code = AuthFailureCode.YT_EXPIRED
        r.expired = True
        r.error   = (
            "401 — token expirado o inválido"
            if any(k in msg for k in ("401", "unauthorized", "sign in", "cookie", "parse"))
            else str(exc)[:200]
        )
    return r


def _preflight_spotify() -> PreFlightResult:
    """
    Pre-flight de Spotify v5.0 — Authorization Code Flow oficial.

    Verifica que CLIENT_ID / CLIENT_SECRET están configurados y que el
    token cacheado (.spotify_cache) es válido o puede refrescarse.
    Si no hay cache, expired=True → AuthManager inicia el flujo OAuth.
    """
    r             = PreFlightResult("Spotify")
    env           = read_env_values()
    client_id     = env.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = env.get("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect_uri  = env.get("SPOTIFY_REDIRECT_URI", SPOTIFY_REDIRECT_URI).strip() \
                    or SPOTIFY_REDIRECT_URI

    if not client_id or not client_secret:
        r.expired = True
        r.code    = AuthFailureCode.SPOTIFY_EXPIRED
        r.error   = "SPOTIFY_CLIENT_ID o SPOTIFY_CLIENT_SECRET no configurados en .env"
        return r

    try:
        import spotipy                                   # pylint: disable=import-outside-toplevel
        from spotipy.oauth2 import SpotifyOAuth          # pylint: disable=import-outside-toplevel
        from cache_handler import CacheFileHandler        # pylint: disable=import-outside-toplevel

        cache_handler = CacheFileHandler(cache_path=str(BASE_DIR / ".spotify_cache"))
        oauth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
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
        msg    = str(exc).lower()
        r.code = AuthFailureCode.SPOTIFY_EXPIRED
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
        r.error = "APPLE_AUTH_BEARER o APPLE_MUSIC_USER_TOKEN no configurados en .env"
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
            r.error   = "401 — Apple Music token expirado"
            return r
        if resp.status_code != 200:
            r.error = f"HTTP inesperado {resp.status_code}"
            return r
        sf = resp.json().get("data", [{}])[0].get("id", "us")
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
    """Texto corto para Tooltip (sesión caída)."""
    if r.ok:
        return ""
    hints = {
        "YouTube Music": "browser.json: Cookie + Authorization (SAPISIDHASH)",
        "Spotify":       ".env: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REDIRECT_URI",
        "Apple Music":   ".env: APPLE_AUTH_BEARER + APPLE_MUSIC_USER_TOKEN",
    }
    tag = f"[{r.code}] " if r.code else ""
    return f"{tag}{hints.get(r.platform, r.platform)} · {r.error}"[:500]


async def run_preflight() -> list[PreFlightResult]:
    """Run all three pre-flight checks in parallel. Returns [yt, sp, am]."""
    results = await asyncio.gather(
        asyncio.to_thread(_preflight_youtube),
        asyncio.to_thread(_preflight_spotify),
        asyncio.to_thread(_preflight_apple),
        return_exceptions=True,
    )
    out: list[PreFlightResult] = []
    for plat, res in zip(["YouTube Music", "Spotify", "Apple Music"], results):
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
    "<h2>✓ Login con Spotify exitoso</h2>"
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
    "<h2>✗ Error de autenticación</h2>"
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
        self._done  = threading.Event()
        self._server: Optional[http.server.HTTPServer] = None

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background listener thread."""
        server_ref = self
        # Build request handler class closing over server_ref
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
                    # Unknown path — respond OK but keep waiting
                    self.send_response(200)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                server_ref._done.set()

            def log_message(self, *_args):  # silence access log
                pass

        try:
            self._server = http.server.HTTPServer(("127.0.0.1", SPOTIFY_CALLBACK_PORT), _Handler)
            self._server.timeout = 1.0  # poll interval so we can check _done
        except OSError as exc:
            raise RuntimeError(
                f"No se pudo abrir el puerto {SPOTIFY_CALLBACK_PORT} para el callback OAuth. "
                f"Asegúrate de que ningún otro proceso lo esté usando. Detalle: {exc}"
            ) from exc

        thread = threading.Thread(target=self._serve, daemon=True, name="spotify-oauth-cb")
        thread.start()

    def wait(self, timeout: float = SPOTIFY_OAUTH_TIMEOUT) -> bool:
        """Block (non-event-loop) until callback received or timeout. Returns True on success."""
        return self._done.wait(timeout=timeout)

    def stop(self) -> None:
        """Signal the serve loop to exit and close the socket."""
        self._done.set()
        if self._server:
            try:
                self._server.server_close()
            except OSError:
                pass

    # ── Internal ─────────────────────────────────────────────────────────

    def _serve(self) -> None:
        assert self._server is not None
        while not self._done.is_set():
            self._server.handle_request()
        try:
            self._server.server_close()
        except OSError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §4  FLET STATUS PANEL  (read-only — no credential editing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class StatusPanel:
    """
    Read-only platform connection status dialog.

    Displays the current pre-flight result for each platform with a
    color-coded badge.  The only interactive action is "Conectar con
    Spotify" which starts the official OAuth Authorization Code Flow.

    No credential fields are shown or editable here — all credentials
    must be configured directly in .env or browser.json.
    """

    _PLATFORM_ICONS = {
        "YouTube Music": ft.Icons.VIDEO_LIBRARY_OUTLINED,
        "Spotify":       ft.Icons.MUSIC_NOTE,
        "Apple Music":   ft.Icons.APPLE,
    }

    def __init__(self, page: ft.Page, auth_manager: "AuthManager") -> None:
        self.page         = page
        self._am          = auth_manager
        self._dlg: Optional[ft.AlertDialog] = None

        # Per-platform live-update refs
        self._status_icons:   dict[str, ft.Icon]      = {}
        self._status_labels:  dict[str, ft.Text]       = {}
        self._sp_connect_btn: Optional[ft.TextButton]  = None
        self._sp_spinner:     Optional[ft.ProgressRing] = None
        self._sp_status_row:  Optional[ft.Row]          = None
        self._checking_ring:  Optional[ft.ProgressRing] = None
        self._refresh_btn:    Optional[ft.TextButton]   = None

    # ── Public ────────────────────────────────────────────────────────

    def open(
        self,
        results: Optional[list[PreFlightResult]] = None,
        initial_platform: Optional[str] = None,  # kept for API compatibility, unused
    ) -> None:
        """Show (or re-focus) the status panel dialog."""
        if self._dlg is not None and getattr(self._dlg, "open", False):
            # Already open — just refresh the displayed data
            if results:
                self._apply_results(results)
            return

        # Build dialog
        self._dlg = self._build_dialog(results or [])
        self.page.show_dialog(self._dlg)

    def close(self) -> None:
        if self._dlg is not None:
            try:
                self.page.pop_dialog()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            self._dlg = None

    def refresh_results(self, results: list[PreFlightResult]) -> None:
        """Push new pre-flight results into the already-open panel."""
        if self._dlg is None or not getattr(self._dlg, "open", False):
            return
        self._apply_results(results)
        self._stop_checking()

    # ── Build ─────────────────────────────────────────────────────────

    def _build_dialog(self, results: list[PreFlightResult]) -> ft.AlertDialog:
        # Map results for easy lookup
        result_map = {r.platform: r for r in results}
        platforms  = ["YouTube Music", "Spotify", "Apple Music"]

        platform_rows: list[ft.Control] = []
        for plat in platforms:
            r = result_map.get(plat)
            row = self._build_platform_row(plat, r)
            platform_rows.append(row)
            platform_rows.append(
                ft.Divider(height=1, color=_BORDER_LIGHT, thickness=0.5)
            )
        # Remove trailing divider
        if platform_rows:
            platform_rows.pop()

        # Spinner for re-check animation
        self._checking_ring = ft.ProgressRing(
            width=14, height=14, stroke_width=2,
            color=_ACCENT, visible=False,
        )

        # Refresh button
        self._refresh_btn = ft.TextButton(
            "Verificar conexiones",
            icon=ft.Icons.REFRESH_OUTLINED,
            on_click=self._on_refresh,
            style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: _ACCENT}),
        )

        footer_note = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.INFO_OUTLINE, color=_TEXT_DIM, size=13),
                    ft.Text(
                        "Las credenciales se configuran directamente en .env y browser.json",
                        size=10, color=_TEXT_DIM, font_family="IBM Plex Sans",
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=_CHIP_BG,
            border=ft.Border.all(0.8, _BORDER_LIGHT),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        )

        body = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=platform_rows,
                        spacing=0,
                    ),
                    bgcolor=_CHIP_BG,
                    border=ft.Border.all(0.8, _BORDER_LIGHT),
                    border_radius=10,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                ),
                footer_note,
            ],
            spacing=12,
        )

        dlg = ft.AlertDialog(
            modal=True,
            scrollable=False,
            title=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.WIFI_TETHERING, color=_ACCENT, size=18),
                    ft.Text(
                        "Estado de Plataformas",
                        size=14, weight=ft.FontWeight.W_700,
                        color=_TEXT_PRIMARY, font_family="IBM Plex Sans",
                    ),
                    ft.Container(expand=True),
                    self._checking_ring,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Container(
                content=body,
                width=480,
                bgcolor=_BG_SURFACE,
                border_radius=10,
                padding=ft.Padding.all(16),
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ),
            actions=[
                self._refresh_btn,
                ft.TextButton(
                    "Cerrar",
                    on_click=lambda _: self.close(),
                    style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: _TEXT_MUTED}),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=_BG_PANEL,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        return dlg

    def _build_platform_row(
        self, platform: str, result: Optional[PreFlightResult]
    ) -> ft.Container:
        """Build one platform status row. Stores control refs for live updates."""
        ok      = result.ok      if result else False
        expired = result.expired if result else False
        error   = result.error   if result else ""

        # Status icon
        if result is None:
            ico_name  = ft.Icons.HELP_OUTLINE
            ico_color = _TEXT_DIM
            label_val = "Sin verificar"
            label_col = _TEXT_DIM
        elif ok:
            ico_name  = ft.Icons.CHECK_CIRCLE_OUTLINE
            ico_color = _SUCCESS
            label_val = "Conectado"
            label_col = _SUCCESS
        elif expired:
            ico_name  = ft.Icons.LOCK_CLOCK_OUTLINED
            ico_color = _WARNING
            label_val = "Sin autenticar"
            label_col = _WARNING
        else:
            ico_name  = ft.Icons.CANCEL_OUTLINED
            ico_color = _ERROR_COL
            label_val = "Sin conexión"
            label_col = _ERROR_COL

        status_icon  = ft.Icon(ico_name, color=ico_color, size=16)
        status_label = ft.Text(
            label_val, size=11, color=label_col,
            font_family="IBM Plex Sans", weight=ft.FontWeight.W_600,
        )
        self._status_icons[platform]  = status_icon
        self._status_labels[platform] = status_label

        # Platform identity column
        plat_icon = ft.Container(
            content=ft.Icon(self._PLATFORM_ICONS[platform], color=_ACCENT, size=18),
            bgcolor=_ACCENT_HALO,
            border_radius=8,
            padding=ft.Padding.all(6),
            width=34, height=34,
        )
        plat_name = ft.Text(
            platform, size=13, color=_TEXT_PRIMARY,
            font_family="IBM Plex Sans", weight=ft.FontWeight.W_600,
        )
        if error and not ok:
            plat_name_col = ft.Column(
                controls=[
                    plat_name,
                    ft.Text(
                        error[:72] + ("…" if len(error) > 72 else ""),
                        size=9, color=_TEXT_DIM, font_family="IBM Plex Sans",
                    ),
                ],
                spacing=1, tight=True, expand=True,
            )
        else:
            plat_name_col = ft.Column(
                controls=[plat_name],
                spacing=0, tight=True, expand=True,
            )

        status_badge = ft.Row(
            controls=[status_icon, status_label],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Spotify-specific: connect button + spinner
        right_controls: list[ft.Control] = [status_badge]
        if platform == "Spotify":
            needs_oauth = (result is None) or (not ok)
            self._sp_spinner = ft.ProgressRing(
                width=14, height=14, stroke_width=2,
                color=_ACCENT, visible=False,
            )
            self._sp_connect_btn = ft.TextButton(
                "Conectar",
                icon=ft.Icons.OPEN_IN_BROWSER_OUTLINED,
                on_click=self._on_spotify_connect,
                visible=needs_oauth,
                style=ft.ButtonStyle(
                    color={ft.ControlState.DEFAULT: _ACCENT},
                    padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                ),
            )
            self._sp_status_row = ft.Row(
                controls=[
                    self._sp_spinner,
                    self._sp_connect_btn,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            right_controls.append(self._sp_status_row)

        row = ft.Container(
            content=ft.Row(
                controls=[
                    plat_icon,
                    plat_name_col,
                    ft.Container(expand=True),
                    ft.Row(
                        controls=right_controls,
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(vertical=10),
        )
        return row

    # ── Live updates ──────────────────────────────────────────────────

    def _apply_results(self, results: list[PreFlightResult]) -> None:
        """Update status badges in-place without rebuilding the dialog."""
        for r in results:
            icon_ctrl  = self._status_icons.get(r.platform)
            label_ctrl = self._status_labels.get(r.platform)
            if icon_ctrl is None or label_ctrl is None:
                continue
            if r.ok:
                icon_ctrl.name   = ft.Icons.CHECK_CIRCLE_OUTLINE
                icon_ctrl.color  = _SUCCESS
                label_ctrl.value = "Conectado"
                label_ctrl.color = _SUCCESS
            elif r.expired:
                icon_ctrl.name   = ft.Icons.LOCK_CLOCK_OUTLINED
                icon_ctrl.color  = _WARNING
                label_ctrl.value = "Sin autenticar"
                label_ctrl.color = _WARNING
            else:
                icon_ctrl.name   = ft.Icons.CANCEL_OUTLINED
                icon_ctrl.color  = _ERROR_COL
                label_ctrl.value = "Sin conexión"
                label_ctrl.color = _ERROR_COL

            # Spotify connect button visibility
            if r.platform == "Spotify" and self._sp_connect_btn is not None:
                self._sp_connect_btn.visible = not r.ok

        try:
            if self._dlg and getattr(self._dlg, "open", False):
                self._dlg.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _set_spotify_loading(self, loading: bool) -> None:
        if self._sp_spinner is not None:
            self._sp_spinner.visible = loading
        if self._sp_connect_btn is not None:
            self._sp_connect_btn.disabled = loading
            self._sp_connect_btn.visible  = not loading
        try:
            if self._dlg and getattr(self._dlg, "open", False):
                self._dlg.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _start_checking(self) -> None:
        if self._checking_ring is not None:
            self._checking_ring.visible = True
        if self._refresh_btn is not None:
            self._refresh_btn.disabled = True
        try:
            if self._dlg and getattr(self._dlg, "open", False):
                self._dlg.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _stop_checking(self) -> None:
        if self._checking_ring is not None:
            self._checking_ring.visible = False
        if self._refresh_btn is not None:
            self._refresh_btn.disabled = False
        try:
            if self._dlg and getattr(self._dlg, "open", False):
                self._dlg.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    # ── Event handlers ─────────────────────────────────────────────────

    def _on_refresh(self, _e: ft.ControlEvent) -> None:
        asyncio.create_task(self._do_refresh())

    async def _do_refresh(self) -> None:
        self._start_checking()
        results = await self._am.check_all_sessions()
        self._am.ingest_preflight_results(results)
        self.refresh_results(results)

    def _on_spotify_connect(self, _e: ft.ControlEvent) -> None:
        asyncio.create_task(self._do_spotify_oauth())

    async def _do_spotify_oauth(self) -> None:
        """Triggers the browser-based Spotify OAuth flow from within the panel."""
        self._set_spotify_loading(True)
        self._am.state_log_fn("[INFO] Iniciando autenticación OAuth de Spotify…")
        try:
            ok = await self._am.start_spotify_oauth_flow()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            ok = False
            self._am.state_log_fn(f"[ERROR] OAuth Spotify: {exc}")
        finally:
            self._set_spotify_loading(False)

        if ok:
            # Re-run pre-flight to update status badge
            await self._do_refresh()
            self._am.state_log_fn("[SUCCESS] ✓ Spotify autenticado correctamente")
        else:
            # Show error without connecting — badge stays amber
            self._am.state_log_fn("[ERROR] Autenticación con Spotify no completada")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §5  AUTH MANAGER  (service-level coordinator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AuthManager:
    """
    High-level auth coordinator used by app.py.

    Responsibilities
    ────────────────
    1. run_startup_check() — parallel pre-flight on all three platforms.
    2. If Spotify has no cached token, start_spotify_oauth_flow() is called:
       opens the system browser, runs a local HTTP server on port 8080,
       exchanges the authorization code for a token and saves it to
       .spotify_cache so subsequent runs are fully automatic.
    3. open_wizard() — opens a READ-ONLY status panel (no credential editing).
    4. refresh_session_icons() — revalidates sessions and updates the UI icons.
    5. reload_credentials() — hot-reloads .env and reinitialises services.

    Public API is intentionally kept compatible with app.py so no changes
    are required there.
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
                "el objeto AppState, no state._log ni otro valor."
            )

        self._status_panel  = StatusPanel(page, self)
        self._last_results: list[PreFlightResult] = []
        self._reload_task:  Optional[asyncio.Task] = None  # tracked for hard_cleanup
        self._oauth_task:   Optional[asyncio.Task] = None

        _bootstrap_env_if_missing()

    # ── Spotify OAuth ──────────────────────────────────────────────────

    async def start_spotify_oauth_flow(self) -> bool:
        """
        Full Authorization Code Flow for Spotify.

        1. Builds SpotifyOAuth (open_browser=False — we control the browser).
        2. Calls get_authorize_url() → opens system browser.
        3. Starts _OAuthCallbackServer on 127.0.0.1:8080.
        4. Awaits callback with SPOTIFY_OAUTH_TIMEOUT seconds.
        5. Exchanges code for token (stored in .spotify_cache).
        6. Returns True on success.
        """
        env           = read_env_values()
        client_id     = env.get("SPOTIFY_CLIENT_ID", "").strip()
        client_secret = env.get("SPOTIFY_CLIENT_SECRET", "").strip()
        redirect_uri  = env.get("SPOTIFY_REDIRECT_URI", SPOTIFY_REDIRECT_URI).strip() \
                        or SPOTIFY_REDIRECT_URI

        if not client_id or not client_secret:
            self.state_log_fn(
                "[ERROR] OAuth Spotify: SPOTIFY_CLIENT_ID o SPOTIFY_CLIENT_SECRET "
                "no configurados en .env"
            )
            return False

        try:
            from spotipy.oauth2 import SpotifyOAuth          # pylint: disable=import-outside-toplevel
            from cache_handler import CacheFileHandler        # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            self.state_log_fn(f"[ERROR] OAuth Spotify — import: {exc}")
            return False

        cache_path    = str(BASE_DIR / ".spotify_cache")
        cache_handler = CacheFileHandler(cache_path=cache_path)
        oauth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="playlist-modify-public playlist-modify-private user-library-read",
            cache_handler=cache_handler,
            open_browser=False,   # we handle browser opening ourselves
            show_dialog=False,    # don't force re-auth if a valid session exists
        )

        # If a valid token is already cached, skip the flow
        try:
            existing = oauth.get_cached_token()
            if existing and not oauth.is_token_expired(existing):
                self.state_log_fn("[INFO] OAuth Spotify: token válido ya en caché")
                return True
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # ── Start callback server ──────────────────────────────────────
        cb_server = _OAuthCallbackServer()
        try:
            cb_server.start()
        except RuntimeError as exc:
            self.state_log_fn(f"[ERROR] OAuth Spotify — servidor de callback: {exc}")
            return False

        # ── Open browser ───────────────────────────────────────────────
        auth_url = oauth.get_authorize_url()
        self.state_log_fn(
            f"[INFO] Abriendo navegador para autenticación Spotify…\n"
            f"       Si no se abre automáticamente visita: {auth_url}"
        )
        await asyncio.to_thread(webbrowser.open, auth_url)

        # ── Await callback (non-blocking via asyncio.to_thread) ────────
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

        # ── Exchange code for token ────────────────────────────────────
        try:
            token_info = await asyncio.to_thread(
                oauth.get_access_token, cb_server.auth_code, False, False
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.state_log_fn(f"[ERROR] OAuth Spotify — intercambio de token: {exc}")
            return False

        if not token_info:
            self.state_log_fn("[ERROR] OAuth Spotify: get_access_token devolvió vacío")
            return False

        self.state_log_fn("[SUCCESS] ✓ Token de Spotify obtenido y guardado en .spotify_cache")

        # Reinitialise the service so _sp is ready for immediate use
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

        If Spotify has no cached token, starts the OAuth flow automatically.
        Other expired platforms log a warning (credentials must be fixed in .env).
        """
        self.state_log_fn("[INFO] Pre-flight: verificando credenciales…")
        results = await self.check_all_sessions()
        self._last_results = results
        self._sync_auth_ui_state(results)

        needs_oauth = False
        for r in results:
            if r.ok:
                self.state_log_fn(f"[INFO]  ✓ {r.platform}: OK")
            elif r.expired:
                if r.platform == "Spotify" and r.code == AuthFailureCode.SPOTIFY_EXPIRED:
                    needs_oauth = True
                    self.state_log_fn(
                        "[INFO]  ↳ Spotify: sin token — se iniciará el flujo OAuth"
                    )
                else:
                    self.state_log_fn(
                        f"[ERROR] ⚠ {r.platform}: credenciales expiradas — "
                        f"actualiza .env o browser.json"
                    )
            else:
                self.state_log_fn(f"[WARN]  – {r.platform}: {r.error}")

        # Init services that passed pre-flight immediately
        await self._init_passing_services(results)

        if needs_oauth:
            # Delay slightly so the UI renders before the browser opens
            async def _deferred_oauth() -> None:
                await asyncio.sleep(random.uniform(1.5, 2.5))
                ok = await self.start_spotify_oauth_flow()
                if ok:
                    chk = await self.check_all_sessions()
                    self.ingest_preflight_results(chk)
                else:
                    # Show status panel so the user can trigger it manually
                    await asyncio.sleep(0.5)
                    self._status_panel.open(self._last_results, initial_platform="Spotify")

            self._oauth_task = asyncio.create_task(_deferred_oauth())

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

    # ── Status panel ──────────────────────────────────────────────────

    def open_wizard(self, platform: Optional[str] = None) -> None:
        """
        Open the read-only platform status panel.
        The *platform* argument is accepted for API compatibility but is not
        used to select a tab (there are no tabs in the new panel).
        """
        def _go() -> None:
            try:
                self._status_panel.open(self._last_results or None)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                self.state_log_fn(f"[ERROR] StatusPanel: {ex}")

        try:
            asyncio.get_running_loop().call_soon(_go)
        except RuntimeError:
            _go()

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

        # Push to panel if it is open
        self._status_panel.refresh_results(chk)

    # ── Deprecated stub (kept for any caller that still references it) ─

    def get_spotify_web_token(self) -> Optional[str]:
        """DEPRECATED in v5.0 — use SpotifyOAuth + CacheFileHandler."""
        self.state_log_fn(
            "[WARN] get_spotify_web_token() está deprecado en v5.0. "
            "Usa SpotifyOAuth + CacheFileHandler (MusicApiService._sync_init_spotify)."
        )
        return None
