"""
auth_manager.py — MelomaniacPass v5.0 — ISRC-Master Auth
════════════════════════════════════════
Centralises ALL credential I/O, pre-flight validation, and the Flet
Configuration Wizard that pops up when a 401 is detected.

Strict external-config contract
────────────────────────────────
browser.json  (YouTube Music)
    {
        "Accept": "*/*",
        "Authorization": "<SAPISIDHASH …>",
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": "https://music.youtube.com",
        "Cookie" : "<raw cookie string>"
    }

.env  (Spotify & Apple Music)
    # SPOTIFY — Authorization Code Flow (Oficial v5.0)
    SPOTIFY_CLIENT_ID="<App Client ID del Developer Dashboard>"
    SPOTIFY_CLIENT_SECRET="<App Client Secret del Developer Dashboard>"
    SPOTIFY_REDIRECT_URI="http://127.0.0.1:8888/callback"

    # APPLE MUSIC
    APPLE_AUTH_BEARER="<value>"
    APPLE_MUSIC_USER_TOKEN="<value>"

Eliminado en v5.0:
    SPOTIFY_SP_DC, SPOTIFY_CLIENT_TOKEN, SPOTIFY_MANUAL_BEARER,
    SPOTIFY_USER_AGENT, SPOTIFY_ACCEPT, SPOTIFY_ACCEPT_LANG, SPOTIFY_ORIGIN
    → Reemplazados por el flujo oficial SpotifyOAuth + CacheFileHandler.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path
from typing import Callable, Optional

import flet as ft
import requests
from dotenv import dotenv_values, load_dotenv, set_key

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
BROWSER_JSON   = BASE_DIR / "browser.json"
ENV_FILE       = BASE_DIR / ".env"

# ── Fixed keys in browser.json ─────────────────────────────────────────
BROWSER_JSON_FIXED: dict[str, str] = {
    "Accept":          "*/*",
    "Content-Type":    "application/json",
    "X-Goog-AuthUser": "0",
    "x-origin":        "https://music.youtube.com",
}

# ── Required .env variable names (exact, ordered) ──────────────────────
# v5.0 ISRC-Master: shadow auth eliminado → Authorization Code Flow oficial
ENV_KEYS_SPOTIFY = [
    "SPOTIFY_CLIENT_ID",      # Client ID del Spotify Developer Dashboard
    "SPOTIFY_CLIENT_SECRET",  # Client Secret del Spotify Developer Dashboard
    "SPOTIFY_REDIRECT_URI",   # URI registrado en el Dashboard
]
ENV_KEYS_APPLE = [
    "APPLE_AUTH_BEARER",
    "APPLE_MUSIC_USER_TOKEN",
]
ENV_KEYS_ALL = ENV_KEYS_SPOTIFY + ENV_KEYS_APPLE

# ── Design tokens (mirrored from app.py to keep wizard visually consistent) ──
_BG_DEEP      = "#FF000000"
_BG_PANEL     = "#FF080808"
_BG_SURFACE   = "#FF111118"
_ACRYLIC_BG   = "#14000000"
_ACRYLIC_BORD = "#1AFFFFFF"
_ACCENT       = "#FF4F8BFF"
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
        # Bootstrap with section comments — v5.0 OAuth layout
        ENV_FILE.write_text(
            "# SPOTIFY — Authorization Code Flow (Oficial v5.0)\n"
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

    YT_EXPIRED = "YT_EXPIRED"
    SPOTIFY_EXPIRED = "SPOTIFY_EXPIRED"
    APPLE_EXPIRED = "APPLE_EXPIRED"


class PreFlightResult:
    """Holds the outcome of a single platform pre-flight check."""

    def __init__(self, platform: str):
        self.platform = platform
        self.ok       = False
        self.error    = ""          # human-readable reason
        self.expired  = False       # True → 401 detected → open wizard
        self.code     = ""          # AuthFailureCode.* cuando falla

    def __repr__(self) -> str:
        status = "OK" if self.ok else f"FAIL({'EXPIRED' if self.expired else self.error[:30]})"
        return f"<PreFlight {self.platform}: {status}>"


def _preflight_youtube() -> PreFlightResult:
    r = PreFlightResult("YouTube Music")
    bj = read_browser_json()
    if not bj.get("Authorization") or not bj.get("Cookie"):
        r.error = "browser.json missing Authorization or Cookie"
        r.expired = True  # FORZAMOS QUE SEPA QUE EXPIRÓ
        return r
    if not bj.get("Authorization", "").startswith("SAPISIDHASH"):
        r.error = "Authorization field does not start with 'SAPISIDHASH'"
        r.expired = True
        return r
    try:
        from ytmusicapi import YTMusic  # pylint: disable=import-outside-toplevel
        ytm = YTMusic(str(BROWSER_JSON))
        # get_history() requiere auth obligatorio. Si está vencido, explota y nos da el 401.
        ytm.get_history()
        r.ok = True
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # ytmusicapi no define jerarquía de excepciones públicas documentada
        msg = str(exc).lower()
        r.code = AuthFailureCode.YT_EXPIRED
        # Ampliamos la captura para cualquier error de sesión de YouTube
        if "401" in msg or "unauthorized" in msg or "sign in" in msg or "cookie" in msg or "parse" in msg:
            r.expired = True
            r.error   = "401 — token expired or invalid"
        else:
            r.expired = True  # Ante la duda en YTM, lo mandamos al wizard
            r.error = msg
    return r


def _preflight_spotify() -> PreFlightResult:
    """
    Pre-flight de Spotify v5.0 — Authorization Code Flow oficial.

    Verifica que SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET están configurados
    y que el token cacheado en .spotify_cache es válido (o puede refrescarse).
    Si no hay cache previo y el usuario aún no ha completado el primer login,
    devuelve expired=True para que el wizard lo guíe.
    """
    r = PreFlightResult("Spotify")
    env           = read_env_values()
    client_id     = env.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = env.get("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect_uri  = env.get(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
    ).strip()

    if not client_id or not client_secret:
        r.expired = True
        r.code    = AuthFailureCode.SPOTIFY_EXPIRED
        r.error   = "SPOTIFY_CLIENT_ID o SPOTIFY_CLIENT_SECRET no configurados en .env"
        return r

    try:
        import spotipy                                        # pylint: disable=import-outside-toplevel
        from spotipy.oauth2 import SpotifyOAuth              # pylint: disable=import-outside-toplevel
        from cache_handler import CacheFileHandler            # pylint: disable=import-outside-toplevel

        cache_handler = CacheFileHandler(cache_path=".spotify_cache")
        oauth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="playlist-modify-public playlist-modify-private user-library-read",
            cache_handler=cache_handler,
            open_browser=False,
        )
        # Intenta obtener token desde cache; si no existe, get_cached_token() devuelve None
        token_info = oauth.get_cached_token()
        if not token_info:
            r.expired = True
            r.code    = AuthFailureCode.SPOTIFY_EXPIRED
            r.error   = "No hay token cacheado. Completa el primer login OAuth."
            return r

        # Refresca si está próximo a expirar (SpotifyOAuth lo hace internamente)
        if oauth.is_token_expired(token_info):
            token_info = oauth.refresh_access_token(token_info["refresh_token"])

        # Valida el token contra /v1/me
        import requests as _requests                          # pylint: disable=import-outside-toplevel
        resp = _requests.get(
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
    except Exception as exc:                                  # pylint: disable=broad-exception-caught
        # spotipy.oauth2 puede lanzar SpotifyOauthError, requests.RequestException, etc.
        msg = str(exc).lower()
        r.code = AuthFailureCode.SPOTIFY_EXPIRED
        if "401" in msg or "unauthorized" in msg or "token" in msg or "expired" in msg:
            r.expired = True
            r.error   = "401 — token expirado o scopes insuficientes"
        else:
            r.expired = True   # Ante cualquier fallo OAuth, abrimos el wizard
            r.error   = str(exc)[:200]
    return r


def _preflight_apple() -> PreFlightResult:
    r = PreFlightResult("Apple Music")
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
            r.code = AuthFailureCode.APPLE_EXPIRED
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
        "Spotify": ".env: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REDIRECT_URI (OAuth 2.0)",
        "Apple Music": ".env: APPLE_AUTH_BEARER + APPLE_MUSIC_USER_TOKEN",
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
            r = PreFlightResult(plat)
            r.error = str(res)
            out.append(r)
        else:
            out.append(res)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §3  FLET CONFIGURATION WIZARD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfigWizard:
    """
    Flet overlay dialog for updating credentials without a process restart.

    Usage:
        wizard = ConfigWizard(page, on_saved=lambda: service.reload_credentials())
        wizard.open(preflight_results)   # opens only the failing tabs
    """

    def __init__(
        self,
        page: ft.Page,
        on_saved: Optional[Callable[[], None]] = None,
    ):
        self.page     = page
        self.on_saved = on_saved
        self._dlg: Optional[ft.AlertDialog] = None
        self._tab_panels: list[ft.Container] = []
        self._tab_buttons: list[ft.Container] = []
        self._panel_holder: Optional[ft.Container] = None
        self._failed_platforms: set[str] = set()
        self._active_tab_idx: int = 0
        self._is_saving: bool = False
        # Declarados aquí para evitar W0201; se inicializan en _panel_youtube/spotify/apple
        self._yt_auth: Optional[ft.TextField] = None
        self._yt_cookie: Optional[ft.TextField] = None
        self._sp_fields: dict[str, ft.TextField] = {}
        self._am_fields: dict[str, ft.TextField] = {}

    def _show_dialog(self, dlg: ft.AlertDialog) -> None:
        """
        Flet 0.83+: Page.show_dialog() apila el overlay y envuelve on_dismiss.
        """
        self.page.show_dialog(dlg)

    def _dismiss_dialog(self, dlg: ft.AlertDialog) -> None:
        """
        Cierra el AlertDialog sin pisar `on_dismiss`.

        `Page.show_dialog()` sustituye `on_dismiss` por un wrapper interno que
        retira el control de la pila y restaura el handler original. Si asignamos
        `on_dismiss = None` o quitamos el control a mano, el cliente Flutter puede
        quedar en estado inconsistente (barrera modal, toques) y el wizard «muere».

        La forma correcta: `pop_dialog()` cuando este diálogo es el abierto más
        reciente (típico con el wizard a pantalla completa).
        """
        if dlg is None:
            return
        page = self.page
        try:
            ds = getattr(page, "_dialogs", None)
            if ds is not None and dlg in ds.controls:
                top_open = next(
                    (d for d in reversed(ds.controls) if getattr(d, "open", False)),
                    None,
                )
                if top_open is dlg:
                    page.pop_dialog()
                else:
                    dlg.open = False
                    dlg.update()
                    page.update()
            else:
                dlg.open = False
                dlg.update()
                page.update()
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Flet puede lanzar distintas exc. internas al cerrar el diálogo
            print(f"[ConfigWizard] No se pudo cerrar el diálogo: {e}")

    def _safe_dialog_update(self) -> None:
        try:
            if self._dlg is not None and getattr(self._dlg, "open", False):
                self._dlg.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # Flet puede fallar al actualizar si el control ya fue desmontado

    def _resolve_initial_tab(
        self,
        failed_platforms: set[str],
        initial_platform: Optional[str],
    ) -> int:
        if initial_platform and initial_platform in self._PLATFORM_INDEX:
            return self._PLATFORM_INDEX[initial_platform]
        if failed_platforms:
            order = ["YouTube Music", "Spotify", "Apple Music"]
            return next((self._PLATFORM_INDEX[p] for p in order if p in failed_platforms), 0)
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
            is_warn = tab_order[i] in self._failed_platforms
            col_active = _WARNING if is_warn else _TEXT_PRIMARY
            col_inactive = _WARNING if is_warn else _TEXT_MUTED
            btn.bgcolor = "#14FFFFFF" if i == idx else "transparent"
            row = btn.content
            row.controls[0].color = col_active if i == idx else col_inactive
            row.controls[1].color = col_active if i == idx else col_inactive
            row.controls[1].weight = (
                ft.FontWeight.W_600 if i == idx else ft.FontWeight.W_400
            )
        self._safe_dialog_update()
        try:
            self.page.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # page.update() puede fallar si la página ya fue cerrada

    def _on_tab_click(self, e: ft.ControlEvent) -> None:
        idx_raw = getattr(e.control, "data", "0")
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            idx = 0
        self._apply_tab_selection(idx)

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
                # _apply_save y _close_wizard pueden lanzar distintas exc. de Flet/OS
                print(f"[ConfigWizard] Error al guardar: {ex}")
            finally:
                self._is_saving = False

        asyncio.create_task(_save_and_close())

    def _make_tab_btn(
        self,
        idx: int,
        label: str,
        icon_ok: str,
        icon_warn: str,
        platform: str,
    ) -> ft.Container:
        warn = platform in self._failed_platforms
        icon = icon_warn if warn else icon_ok
        color = _WARNING if warn else (
            _TEXT_PRIMARY if idx == self._active_tab_idx else _TEXT_MUTED
        )
        return ft.Container(
            content=ft.Row([
                ft.Icon(icon, color=color, size=13),
                ft.Text(
                    label,
                    size=11,
                    color=color,
                    font_family="IBM Plex Sans",
                    weight=(
                        ft.FontWeight.W_600
                        if idx == self._active_tab_idx else ft.FontWeight.W_400
                    ),
                ),
            ], spacing=6, tight=True),
            bgcolor="#14FFFFFF" if idx == self._active_tab_idx else "transparent",
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            data=str(idx),
            ink=True,
            on_click=self._on_tab_click,
        )

    # ── Public ─────────────────────────────────────────────────────────

    # Platform name → panel index mapping
    _PLATFORM_INDEX = {
        "YouTube Music": 0,
        "Spotify":       1,
        "Apple Music":   2,
    }

    def open(
        self,
        results: Optional[list[PreFlightResult]] = None,
        initial_platform: Optional[str] = None,
    ) -> None:
        """
        Show the wizard.
        • Highlights failed platforms if *results* is supplied.
        • If *initial_platform* is given, that tab is shown first (§2 fix).
          Falls back to the first failing platform, then to tab 0.
        """
        if self._dlg is not None and getattr(self._dlg, "open", False):
            if initial_platform and initial_platform in self._PLATFORM_INDEX:
                self._apply_tab_selection(self._PLATFORM_INDEX[initial_platform])
            return

        if self._dlg is not None:
            try:
                self._dismiss_dialog(self._dlg)
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # El diálogo anterior puede estar ya cerrado o desmontado
            self._dlg = None

        self._tab_panels = []
        self._tab_buttons = []
        self._panel_holder = None
        self._failed_platforms = set()
        self._active_tab_idx = 0

        failed_platforms = set()
        if results:
            for r in results:
                if not r.ok:
                    failed_platforms.add(r.platform)
        self._failed_platforms = failed_platforms

        _initial_idx = self._resolve_initial_tab(failed_platforms, initial_platform)
        self._active_tab_idx = _initial_idx

        panel_yt = self._panel_youtube(warn="YouTube Music" in failed_platforms)
        panel_sp = self._panel_spotify(warn="Spotify" in failed_platforms)
        panel_am = self._panel_apple(warn="Apple Music" in failed_platforms)

        panels = [panel_yt, panel_sp, panel_am]
        self._tab_panels = panels
        self._panel_holder = ft.Container(
            content=panels[_initial_idx],
            expand=True,
        )

        TAB_LABELS = [
            ("YouTube Music", ft.Icons.MUSIC_VIDEO,
             ft.Icons.WARNING_AMBER_ROUNDED, "YouTube Music"),
            ("Spotify",       ft.Icons.MUSIC_NOTE,
             ft.Icons.WARNING_AMBER_ROUNDED, "Spotify"),
            ("Apple Music",   ft.Icons.APPLE,
             ft.Icons.WARNING_AMBER_ROUNDED, "Apple Music"),
        ]

        self._tab_buttons = [
            self._make_tab_btn(i, lbl, ico_ok, ico_warn, plat)
            for i, (lbl, ico_ok, ico_warn, plat) in enumerate(TAB_LABELS)
        ]
        tab_bar = ft.Row(
            controls=self._tab_buttons,
            spacing=4,
        )

        body = ft.Column(
            controls=[
                ft.Container(
                    content=tab_bar,
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
            title=ft.Row([
                ft.Icon(ft.Icons.SETTINGS, color=_ACCENT, size=18),
                ft.Text(
                    "Configuración de Credenciales",
                    size=14, weight=ft.FontWeight.W_700,
                    color=_TEXT_PRIMARY, font_family="IBM Plex Sans",
                ),
            ], spacing=8),
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
            pass  # El diálogo puede estar ya cerrado o desmontado por Flet
        finally:
            self._dlg = None

    # ── Panel builders (replace Tab builders) ──────────────────────────

    @staticmethod
    def _instructions_box(steps: list[tuple[str, str]]) -> ft.Container:
        """
        Renders a subtle instruction box above the credential fields.
        `steps` is a list of (bold_label, plain_text) tuples.
        """
        def _step(num: int, label: str, body: str) -> ft.Row:
            return ft.Row([
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
                ft.Column([
                    ft.Text(
                        label, size=11, color=_TEXT_PRIMARY,
                        font_family="IBM Plex Sans", weight=ft.FontWeight.W_700,
                    ),
                    ft.Text(
                        body, size=11, color=_TEXT_MUTED,
                        font_family="IBM Plex Sans",
                    ),
                ], spacing=1, tight=True, expand=True),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.START)

        rows = [_step(i + 1, lbl, txt) for i, (lbl, txt) in enumerate(steps)]
        return ft.Container(
            content=ft.Column(rows, spacing=8),
            bgcolor="#0AFFFFFF",
            border=ft.Border.all(0.8, "#14FFFFFF"),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        )

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
        hint = self._warn_banner(
            "Token expirado. Actualiza Authorization y Cookie desde "
            "music.youtube.com → DevTools → Network."
        ) if warn else ft.Container(height=0)

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
            content=ft.Column([
                hint,
                instructions,
                self._section("BROWSER.JSON — CAMPOS VARIABLES"),
                self._yt_auth,
                self._yt_cookie,
                self._fixed_note(
                    'Los campos fijos (Accept, Content-Type, X-Goog-AuthUser, x-origin) '
                    'se escriben automáticamente.'
                ),
            ], spacing=10, scroll=ft.ScrollMode.AUTO),
            padding=ft.Padding.all(12),
            expand=True,
        )

    def _panel_spotify(self, warn: bool = False) -> ft.Container:
        """
        Panel del wizard para Spotify — v5.0 ISRC-Master.

        Muestra los tres campos del flujo oficial Authorization Code Flow:
        SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI.
        El shadow auth (sp_dc, Client-Token, Manual-Bearer) fue eliminado en v5.0.
        """
        env = read_env_values()
        self._sp_fields: dict[str, ft.TextField] = {}
        controls: list[ft.Control] = []

        if warn:
            controls.append(self._warn_banner(
                "Token expirado o credenciales OAuth incorrectas. "
                "Verifica SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET "
                "en el Spotify Developer Dashboard."
            ))

        controls.append(self._instructions_box([
            ("Accede al Spotify Developer Dashboard",
             "Ve a developer.spotify.com/dashboard y crea una nueva app "
             "(o selecciona una existente)."),
            ("Copia Client ID y Client Secret",
             "Los encontrarás en la página principal de tu app. "
             "Pégalos en los campos de abajo."),
            ("Configura el Redirect URI en el Dashboard",
             "En 'Settings' de tu app añade exactamente: "
             "http://127.0.0.1:8888/callback — luego cópialo en SPOTIFY_REDIRECT_URI."),
            ("Primer arranque — autorización única",
             "La primera vez, MelomaniacPass mostrará en consola una URL. "
             "Ábrela en el navegador, autoriza la app y pega el redirect URL en consola."),
            ("Siguientes arranques — automático",
             "El token se guarda en .spotify_cache y se renueva automáticamente. "
             "No necesitas repetir el proceso."),
        ]))

        controls.append(self._section("SPOTIFY OFFICIAL OAuth 2.0 — .env"))

        # Campos OAuth (Client Secret se oculta; los otros son visibles)
        _oauth_fields: list[tuple[str, bool]] = [
            ("SPOTIFY_CLIENT_ID",     False),
            ("SPOTIFY_CLIENT_SECRET", True),
            ("SPOTIFY_REDIRECT_URI",  False),
        ]
        for key, is_secret in _oauth_fields:
            tf = ft.TextField(
                label=key,
                value=env.get(key, ""),
                password=is_secret,
                can_reveal_password=is_secret,
                **self._field_style(),
            )
            self._sp_fields[key] = tf
            controls.append(tf)

        controls.append(self._fixed_note(
            'SPOTIFY_REDIRECT_URI debe coincidir EXACTAMENTE con el URI registrado '
            'en el Developer Dashboard. El valor por defecto es: '
            'http://127.0.0.1:8888/callback'
        ))

        return ft.Container(
            content=ft.Column(controls, spacing=10, scroll=ft.ScrollMode.AUTO),
            padding=ft.Padding.all(12),
            expand=True,
        )

    def _panel_apple(self, warn: bool = False) -> ft.Container:
        env = read_env_values()
        self._am_fields: dict[str, ft.TextField] = {}
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
            'la app lo normaliza automáticamente.'
        ))

        return ft.Container(
            content=ft.Column(controls, spacing=10, scroll=ft.ScrollMode.AUTO),
            padding=ft.Padding.all(12),
            expand=True,
        )

    # ── Save logic ─────────────────────────────────────────────────────

    def _apply_save(self) -> None:
        # ── browser.json ──────────────────────────────────────────────
        auth_val   = getattr(self, "_yt_auth",   None)
        cookie_val = getattr(self, "_yt_cookie", None)
        if auth_val and cookie_val:
            write_browser_json(auth_val.value or "", cookie_val.value or "")

        # ── .env (Spotify) ────────────────────────────────────────────
        sp_vals = {k: tf.value or "" for k, tf in getattr(self, "_sp_fields", {}).items()}
        if sp_vals:
            write_env_values(sp_vals)

        # ── .env (Apple Music) ────────────────────────────────────────
        am_vals = {k: tf.value or "" for k, tf in getattr(self, "_am_fields", {}).items()}
        if am_vals:
            write_env_values(am_vals)

    # ── UI helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _field_style() -> dict:
        return {
            "bgcolor": "#08FFFFFF",
            "border_color": "#18FFFFFF",
            "focused_border_color": _ACCENT,
            "label_style": ft.TextStyle(color=_TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            "text_style": ft.TextStyle(color=_TEXT_PRIMARY, size=11, font_family="IBM Plex Sans"),
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
            content=ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=_WARNING, size=14),
                ft.Text(text, size=10, color=_WARNING, font_family="IBM Plex Sans", expand=True),
            ], spacing=6),
            bgcolor="#120C0000",
            border=ft.Border.all(0.8, "#30FFA500"),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §4  AUTH MANAGER (service-level)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AuthManager:
    """
    High-level coordinator used by app.py.

    1. Called once at startup via run_startup_check() → check_all_sessions().
    2. If any platform is expired / misconfigured, opens the ConfigWizard.
    3. After wizard save, calls reload_credentials() which hot-reloads
       .env and reinitialises the affected MusicApiService connections
       without restarting the process.
    """

    def __init__(self, page: ft.Page, service, state):
        self.page = page
        self.service = service
        # Acepta AppState o, por compatibilidad, state._log (método enlazado → __self__)
        if hasattr(state, "notify"):
            self.state = state
            self.state_log_fn = state._log
        elif callable(state) and getattr(state, "__self__", None) is not None and hasattr(
            state.__self__, "notify",
        ):
            self.state = state.__self__
            self.state_log_fn = state
        else:
            raise TypeError(
                "AuthManager(page, service, state): el tercer argumento debe ser "
                "el objeto AppState (p. ej. state), no state._log ni otro valor."
            )
        self._wizard      = ConfigWizard(page, on_saved=self._on_wizard_saved)
        self._last_results: list[PreFlightResult] = []

    def get_spotify_web_token(self) -> Optional[str]:
        """
        Stub de compatibilidad — DEPRECADO en v5.0.

        Con el flujo Authorization Code Flow oficial (SpotifyOAuth + CacheFileHandler),
        spotipy gestiona el access_token internamente. Este método ya no es necesario
        en el pipeline de MusicApiService._sync_init_spotify.

        Mantenido para evitar AttributeError en código que aún lo llame.
        """
        self.state_log_fn(
            "[WARN] get_spotify_web_token() está deprecado en v5.0 ISRC-Master. "
            "Usa SpotifyOAuth + CacheFileHandler (MusicApiService._sync_init_spotify)."
        )
        return None

    async def check_all_sessions(self) -> list[PreFlightResult]:
        """
        Validación triple asíncrona: YouTube (biblioteca), Spotify (/v1/me),
        Apple (storefront + catálogo con media-user-token).
        """
        return await run_preflight()

    def ingest_preflight_results(self, results: list[PreFlightResult]) -> None:
        """Actualiza caché, estado de sesión en AppState y notifica a la UI."""
        self._last_results = results
        self._sync_auth_ui_state(results)
        self.state.notify()

    async def refresh_session_icons(self) -> list[PreFlightResult]:
        """
        Revalidación activa (semáforo UI): vuelve a ejecutar check_all_sessions
        y actualiza iconos / tooltips sin reiniciar la app.
        """
        results = await self.check_all_sessions()
        self.ingest_preflight_results(results)
        return results

    def _sync_auth_ui_state(self, results: list[PreFlightResult]) -> None:
        for r in results:
            self.state.auth_session_ok[r.platform] = r.ok
            self.state.auth_session_hint[r.platform] = (
                "" if r.ok else auth_failure_tooltip(r)
            )

    async def run_startup_check(self) -> list[PreFlightResult]:
        """
        Parallel pre-flight + auth init.  Returns results list.
        If any platform is expired, opens the wizard immediately.
        """
        self.state_log_fn("[INFO] Pre-flight: verificando credenciales…")

        results = await self.check_all_sessions()
        self._last_results = results
        self._sync_auth_ui_state(results)

        lines = []
        need_wizard = False
        for r in results:
            if r.ok:
                lines.append(f"✓ {r.platform}")
                self.state_log_fn(f"[INFO]  ✓ {r.platform}: OK")
            elif r.expired:
                lines.append(f"⚠ {r.platform} (expirado)")
                self.state_log_fn(f"[ERROR] ⚠ {r.platform}: token expirado — abriendo wizard")
                need_wizard = True
            else:
                lines.append(f"– {r.platform}")
                self.state_log_fn(f"[ERROR] – {r.platform}: {r.error}")

        if need_wizard:
            # Determine first failing platform for auto-navigation (§2)
            _order = ["YouTube Music", "Spotify", "Apple Music"]
            _failing = next(
                (r.platform for p in _order for r in results if r.platform == p and not r.ok),
                None,
            )
            async def _open_after_render() -> None:
                await asyncio.sleep(random.uniform(2.0, 5.0))
                self._wizard.open(results, initial_platform=_failing)
            asyncio.create_task(_open_after_render())

        # Always try to init services for platforms that passed pre-flight
        await self._init_passing_services(results)
        return results

    async def _init_passing_services(self, results: list[PreFlightResult]) -> None:
        """Init only platforms that passed pre-flight (avoids blocking on broken creds)."""
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

    def open_wizard(self, platform: Optional[str] = None) -> None:
        """
        Abre el wizard desde la UI. Si *platform* es un nombre conocido
        (YouTube Music / Spotify / Apple Music), ConfigWizard selecciona esa
        pestaña vía _PLATFORM_INDEX antes de mostrar el diálogo.
        """
        def _go() -> None:
            try:
                self._wizard.open(self._last_results or None, initial_platform=platform)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                # ConfigWizard.open() puede lanzar distintas exc. internas de Flet
                self.state_log_fn(f"[ERROR] Wizard: {ex}")

        try:
            asyncio.get_running_loop().call_soon(_go)
        except RuntimeError:
            _go()

    def _on_wizard_saved(self) -> None:
        """Called by ConfigWizard after the user clicks 'Guardar y Aplicar'."""
        asyncio.create_task(self.reload_credentials())

    async def reload_credentials(self) -> None:
        """
        Hot-reload credentials from disk and reinitialise all services.
        No process restart required.
        """
        self.state_log_fn("[INFO] Recargando credenciales…")
        load_dotenv(str(ENV_FILE), override=True)

        results = await asyncio.gather(
            self.service.init_spotify(),
            self.service.init_youtube(),
            self.service.init_apple(),
            return_exceptions=True,
        )
        platforms = ["Spotify", "YouTube Music", "Apple Music"]
        for plat, res in zip(platforms, results):
            if res is True:
                self.state_log_fn(f"[SUCCESS] ✓ {plat}: reconectado")
            else:
                self.state_log_fn(f"[ERROR]   – {plat}: {res}")
        chk = await self.check_all_sessions()
        self._last_results = chk
        self._sync_auth_ui_state(chk)
        self.state.notify()
