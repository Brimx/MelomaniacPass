"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║              Interfaz Principal de Usuario                           ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: ui/main_ui.py
Descripción: Componente principal de la interfaz de usuario. Implementa
            PlaylistManagerUI, la vista principal que orquesta todos los
            componentes visuales y maneja la interacción del usuario.

Estrategia de Diseño - Arquitectura Reactiva:
    PlaylistManagerUI es una clase UI pura que no conoce detalles de APIs.
    Implementa el patrón Observer suscribiéndose a cambios en AppState:
    
    1. Separación de Responsabilidades:
       - UI: Solo renderizado y eventos de usuario
       - State: Lógica de negocio y coordinación
       - Services: Comunicación con APIs
    
    2. Flujo Unidireccional de Datos:
       Usuario → UI → State → Services → State → UI
       
    3. Actualización Reactiva:
       - State.notify() → _on_state_changed() → actualiza UI
       - Circuit breakers → _on_circuit_change() → deshabilita controles
    
    4. Gestión de Recursos:
       - Caché de filas (SongRow) para evitar recreación
       - Skeleton screens durante carga
       - Tareas asyncio cancelables para búsquedas
    
    5. Feedback Visual Multi-Nivel:
       - Skeleton loading (reduce percepción de latencia)
       - Progress bars (transferencias)
       - Snackbars (notificaciones)
       - Diálogos (errores críticos)
       - Circuit breaker countdown (rate limiting)

Componentes Principales:
    - Sidebar: Navegación y controles de plataforma
    - Content: Lista de canciones y controles de transferencia
    - Telemetry: Panel de monitoreo y logs
    - File picker: Selector de archivos locales
    - Dialogs: Modales para errores y confirmaciones

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import flet as ft

from core.models import Track, LoadState, TransferState
from core.state import AppState
from engine.parsers import parse_local_playlist, build_local_tracks
from ui.song_row import SongRow, SkeletonRow, ITEM_H
from ui.telemetry import TelemetryDrawer
from ui.widgets import _primary_btn, _ghost_btn, _section_label, _status_icon

# ══════════════════════════════════════════════════════════════════════
# TOKENS DE DISEÑO
# ══════════════════════════════════════════════════════════════════════
# Sistema de colores completo para la interfaz principal

BG_DEEP      = "#FF000000"  # Negro absoluto para fondos profundos
BG_PANEL     = "#FF080808"  # Fondo de paneles principales
BG_SURFACE   = "#FF111118"  # Superficies elevadas
BG_HOVER     = "#FF1E1E28"  # Fondo de hover
BG_INPUT     = "#FF16161F"  # Fondo de campos de entrada
SIDEBAR_BG   = "#FF0E0E15"  # Fondo de sidebar
BG_LIST      = "#FF161622"  # Fondo de listas
CHIP_BG      = "#FF1A1A22"  # Fondo de chips y badges
BORDER_LIGHT = "#FF3D4455"  # Bordes visibles
BORDER_MUTED = "#FF2A3040"  # Bordes sutiles
ACCENT       = "#FF4F8BFF"  # Color de acento principal
ACCENT_DIM   = "#FF2D5FCC"  # Acento atenuado
ACCENT_HALO  = "#FF2A3F5C"  # Halo de sombra de acento
SUCCESS      = "#FF00D084"  # Verde para éxito
WARNING      = "#FFFFA500"  # Naranja para advertencias
ERROR_COL    = "#FFFF4444"  # Rojo para errores
TEXT_PRIMARY = "#FFF2F6FF"  # Texto principal
TEXT_MUTED   = "#FF7A8499"  # Texto secundario
TEXT_DIM     = "#FF3D4455"  # Texto terciario
SKELETON_DARK = "#FF0E1016" # Color de skeleton placeholders

# Detección de disponibilidad de Spotipy
try:
    import spotipy
    HAS_SPOTIFY = True
except ImportError:
    HAS_SPOTIFY = False


class PlaylistManagerUI:
    """
    Interfaz principal de usuario para gestión de playlists.
    
    Clase UI pura que implementa el patrón Observer para reaccionar a
    cambios en AppState. No contiene lógica de negocio ni conocimiento
    de APIs, delegando toda la coordinación al estado.
    
    Attributes:
        page: Instancia de página Flet.
        state: Instancia de AppState (estado global).
        auth_manager: Referencia a AuthManager (inyectada externamente).
        root: Container raíz de la UI.
    
    Componentes Internos:
        _sidebar: Panel lateral con navegación y controles
        _content: Área principal con lista de canciones
        _telemetry: Panel de telemetría y logs
        _file_picker: Selector de archivos del sistema
        _row_cache: Caché de SongRow para evitar recreación
        _skeleton_tasks: Tareas de animación de skeleton screens
    
    Flujo de Datos:
        1. Usuario interactúa con UI (click, input, etc)
        2. UI llama métodos de AppState
        3. AppState ejecuta lógica y actualiza su estado interno
        4. AppState.notify() dispara _on_state_changed()
        5. UI se actualiza reflejando el nuevo estado
    
    Example:
        >>> state = AppState(service)
        >>> ui = PlaylistManagerUI(page, state)
        >>> ui.auth_manager = auth_manager  # Inyección de dependencia
        >>> page.add(ui.root)
    
    Note:
        La separación estricta entre UI y lógica de negocio permite:
        - Testing independiente de componentes
        - Reutilización de lógica en diferentes UIs
        - Cambios en UI sin afectar lógica de negocio
        - Múltiples vistas del mismo estado (ej: modo compacto)
    """

    # Número de filas skeleton mostradas durante carga
    SKELETON_COUNT = 14

    def __init__(self, page: ft.Page, state: AppState):
        """
        Inicializa la interfaz principal con todos sus componentes.
        
        Args:
            page: Instancia de página Flet.
            state: Instancia de AppState para suscripción reactiva.
        
        Note:
            El constructor solo inicializa estructuras de datos y construye
            la jerarquía de componentes. La suscripción a eventos y la
            configuración de callbacks se realiza al final para garantizar
            que todos los componentes estén listos.
        """
        self.page  = page
        self.state = state

        # ──────────────────────────────────────────────────────────────
        # ESTADO INTERNO DE UI
        # ──────────────────────────────────────────────────────────────
        
        self._search_task:            Optional[asyncio.Task] = None
        self._skeleton_tasks:         list[asyncio.Task]     = []
        self._row_cache:              dict[str, SongRow]     = {}
        self._failed_dialog_shown:    bool  = False
        self._transfer_start:         float = 0.0
        self._completion_snack_shown: bool  = False
        self._pm_cleared_for_load:    bool  = False
        self.auth_manager                   = None
        self._auth_poll_task: Optional[asyncio.Task] = None

        # ──────────────────────────────────────────────────────────────
        # FILE PICKER
        # ──────────────────────────────────────────────────────────────
        # Selector de archivos del sistema operativo
        
        self._file_picker = ft.FilePicker()
        page.services.append(self._file_picker)

        # ──────────────────────────────────────────────────────────────
        # CAMPO DE TEXTO PARA PEGAR LISTAS
        # ──────────────────────────────────────────────────────────────
        
        self._paste_field = ft.TextField(
            multiline=True, min_lines=10, max_lines=10,
            hint_text="Pega aquí tu lista  (ej: Título - Artista, una por línea)",
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT,
            focused_border_color=ACCENT, border_radius=10, expand=True,
        )

        # ──────────────────────────────────────────────────────────────
        # CONSTRUCCIÓN DE COMPONENTES PRINCIPALES
        # ──────────────────────────────────────────────────────────────
        
        self._build_sidebar()
        self._build_content()

        # ──────────────────────────────────────────────────────────────
        # ENSAMBLAJE DE LAYOUT RAÍZ
        # ──────────────────────────────────────────────────────────────
        # Layout principal: [Sidebar | Content]
        
        self.root = ft.Container(
            content=ft.Row(
                controls=[self._sidebar, self._content],
                spacing=0, expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            bgcolor=BG_LIST, expand=True,
        )

        # ──────────────────────────────────────────────────────────────
        # SUSCRIPCIÓN A EVENTOS
        # ──────────────────────────────────────────────────────────────
        # Conecta la UI al sistema reactivo de estado y circuit breakers
        
        state.subscribe(self._on_state_changed)
        for platform, cb in state.cb.items():
            cb.subscribe(lambda is_open, rem, p=platform: self._on_circuit_change(p, is_open, rem))
        page.on_resize = lambda _: (self._telemetry.sync_mode(), self.page.update())

        # ──────────────────────────────────────────────────────────────
        # SINCRONIZACIÓN INICIAL
        # ──────────────────────────────────────────────────────────────
        # Tarea asíncrona para sincronizar modo de telemetría tras render
        
        async def _initial_sync():
            await asyncio.sleep(0.15)
            self._telemetry.sync_mode()
            self.page.update()

        page.run_task(_initial_sync)

    # ── Auth helpers ───────────────────────────────────────────────────

    async def _refresh_auth_live(self) -> None:
        am = getattr(self, "auth_manager", None)
        if am:
            await am.refresh_session_icons()

    async def _on_auth_probe(self, platform: str) -> None:
        am = getattr(self, "auth_manager", None)
        if not am:
            return
        self.state.log(f"[INFO] ⏳ Revalidando sesión de {platform}...")
        self.page.update()
        results = await am.check_all_sessions()
        am.ingest_preflight_results(results)
        for r in results:
            if r.platform != platform:
                continue
            if not r.ok:
                self.state.log(f"[ERROR] ⚠ {platform} falló la validación. Abriendo wizard.")
                am.open_wizard(platform)
            else:
                self.state.log(f"[SUCCESS] ✓ {platform} validada correctamente.")
                self._snack(f"Sesión de {platform} válida y activa.")
            break

    # ── Spotify OAuth ──────────────────────────────────────────────────

    async def _on_connect_spotify(self, _e: ft.ControlEvent) -> None:
        svc      = self.state.service
        auth_url = svc.get_spotify_auth_url()
        if not auth_url:
            self._snack("No se puede generar la URL de Spotify. Verifica .env", error=True)
            return
        await self.page.launch_url(auth_url)
        self._open_spotify_oauth_dialog()

    def _open_spotify_oauth_dialog(self) -> None:
        _redirect_field = ft.TextField(
            label="URL de redirección",
            hint_text="http://127.0.0.1:8888/callback?code=…",
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            border_radius=10, multiline=False, expand=True,
        )
        _status_text = ft.Text("", size=10, color=TEXT_MUTED, font_family="IBM Plex Sans", visible=False)

        async def _do_exchange(_e: ft.ControlEvent) -> None:
            code_or_url = (_redirect_field.value or "").strip()
            if not code_or_url:
                _status_text.value   = "Pega la URL completa o solo el código."
                _status_text.color   = WARNING
                _status_text.visible = True
                _status_text.update()
                return
            _status_text.value   = "Intercambiando token…"
            _status_text.color   = TEXT_MUTED
            _status_text.visible = True
            _status_text.update()
            ok = await self.state.service.handle_spotify_redirect(code_or_url)
            if ok:
                self.state.auth_session_ok["Spotify"]   = True
                self.state.auth_session_hint["Spotify"] = "Conectado"
                self._sync_spotify_connect_ui(connected=True)
                self.state.notify()
                dlg.open = False
                self.page.update()
                self._snack("✓ Spotify conectado correctamente")
            else:
                _status_text.value = "No se pudo completar la autorización. Comprueba la URL."
                _status_text.color = ERROR_COL
                _status_text.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Autorizar Spotify", size=15, weight=ft.FontWeight.W_600,
                          color=TEXT_PRIMARY, font_family="IBM Plex Sans"),
            content=ft.Container(
                width=400,
                content=ft.Column(controls=[
                    ft.Text("El navegador se ha abierto con la página de Spotify.\n"
                            "Autoriza la aplicación y copia la URL completa de la barra de dirección:",
                            size=12, color=TEXT_MUTED, font_family="IBM Plex Sans"),
                    ft.Container(height=8),
                    _redirect_field, _status_text,
                ], spacing=4, tight=True),
            ),
            actions=[
                ft.TextButton("Cancelar", style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED}),
                              on_click=lambda _: [setattr(dlg, 'open', False), self.page.update()]),
                ft.TextButton("Autorizar", style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT}),
                              on_click=lambda e: asyncio.create_task(_do_exchange(e))),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_SURFACE,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def _sync_spotify_connect_ui(self, connected: bool) -> None:
        dot_color  = SUCCESS     if connected else TEXT_DIM
        label_val  = "Conectado" if connected else "Desconectado"
        label_col  = SUCCESS     if connected else TEXT_MUTED
        try:
            self._sp_status_dot.color    = dot_color
            self._sp_status_label.value  = label_val
            self._sp_status_label.color  = label_col
            self._sp_connect_btn.visible = not connected
            self._sp_connect_row.update()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _on_open_wizard(self, _e: ft.ControlEvent) -> None:
        am = getattr(self, "auth_manager", None)
        if not am:
            self.state.log("[ERROR] AuthManager no disponible (wizard).")
            return
        am.open_wizard()

    def _close_postmortem_dialog(self) -> None:
        """Limpia el estado de post-mortem sin cerrar diálogos (manejado por telemetry)."""
        s = self.state
        s.pending_review_tracks.clear()
        s.failed_tracks.clear()
        s.api_rejected_tracks.clear()
        s.transfer_error_tracks.clear()
        self._failed_dialog_shown = False


    # ── BUILD SIDEBAR ──────────────────────────────────────────────────

    def _build_sidebar(self) -> None:
        s = self.state

        self.btn_wizard = ft.IconButton(
            icon=ft.Icons.SETTINGS_OUTLINED, icon_color=TEXT_DIM, icon_size=16,
            tooltip="Configurar credenciales", on_click=self._on_open_wizard,
            style=ft.ButtonStyle(overlay_color={ft.ControlState.HOVERED: BG_HOVER},
                                 shape=ft.RoundedRectangleBorder(radius=8)),
        )
        logo = ft.Column([
            ft.Row([
                ft.Container(
                    content=ft.Icon(ft.Icons.HEADPHONES, color=ACCENT, size=22),
                    bgcolor=ACCENT_HALO, border_radius=8, padding=ft.Padding.all(6),
                ),
                ft.Column([
                    ft.Text(spans=[
                        ft.TextSpan("Melomaniac", ft.TextStyle(size=16, weight=ft.FontWeight.W_300,
                                                               color=TEXT_PRIMARY, font_family="IBM Plex Sans")),
                        ft.TextSpan("Pass",       ft.TextStyle(size=16, weight=ft.FontWeight.W_700,
                                                               color=TEXT_PRIMARY, font_family="IBM Plex Sans")),
                    ], opacity=1.0),
                    ft.Text("v5.0", size=9, color=TEXT_DIM, font_family="IBM Plex Sans",
                            style=ft.TextStyle(letter_spacing=0.8), opacity=1.0),
                ], spacing=0, tight=True, expand=True),
                self.btn_wizard,
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=0)

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
                asyncio.create_task(self._do_local_pick())
            elif val == "Pegar Texto":
                self._open_paste_dialog()
            else:
                asyncio.create_task(self._refresh_auth_live())

        def _on_dst_select(e) -> None:
            s.set_destination(e.control.value)
            asyncio.create_task(self._refresh_auth_live())

        self._src_dd = ft.Dropdown(
            label="Origen", value=s.source,
            options=[ft.dropdown.Option(key=p, text=p) for p in AppState.SOURCE_OPTIONS],
            on_select=_on_src_select, **_dd_style,
        )
        self._dst_dd = ft.Dropdown(
            label="Destino", value=s.destination,
            options=[ft.dropdown.Option(key=p, text=p) for p in AppState.PLATFORMS],
            on_select=_on_dst_select, **_dd_style,
        )
        self._status_badge      = ft.Text("", size=10, color=SUCCESS, font_family="IBM Plex Sans", opacity=1.0)
        self._dest_session_warn = ft.Text("", size=9, color=ERROR_COL, font_family="IBM Plex Sans", visible=False)

        platform_section = ft.Column([
            _section_label("PLATAFORMAS"),
            ft.Row([self._src_dd, self._dst_dd], spacing=8),
            self._status_badge,
            self._dest_session_warn,
        ], spacing=8)

        _sp_connected_init = HAS_SPOTIFY and bool(s.service._sp)
        self._sp_status_dot   = ft.Icon(ft.Icons.CIRCLE, size=8,
                                        color=SUCCESS if _sp_connected_init else TEXT_DIM)
        self._sp_status_label = ft.Text(
            "Conectado" if _sp_connected_init else "Desconectado",
            size=10, color=SUCCESS if _sp_connected_init else TEXT_MUTED, font_family="IBM Plex Sans",
        )
        self._sp_connect_btn = ft.TextButton(
            "Conectar", icon=ft.Icons.OPEN_IN_BROWSER_OUTLINED,
            on_click=self._on_connect_spotify,
            visible=not _sp_connected_init,
            style=ft.ButtonStyle(
                color={ft.ControlState.DEFAULT: ACCENT, ft.ControlState.HOVERED: TEXT_PRIMARY},
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=10, font_family="IBM Plex Sans"),
            ),
        )
        self._sp_connect_row = ft.Container(
            content=ft.Row(controls=[
                self._sp_status_dot, self._sp_status_label,
                ft.Container(expand=True), self._sp_connect_btn,
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=BG_INPUT,
            border=ft.Border.all(0.8, SUCCESS if _sp_connected_init else BORDER_LIGHT),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            visible=(s.source == "Spotify" or s.destination == "Spotify"),
        )

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
        self._playlist_section = ft.Column([
            _section_label("PLAYLIST"), self._id_field,
        ], spacing=8, visible=(s.source not in AppState.LOCAL_SOURCES))
        self._playlist_divider = ft.Divider(
            height=1, color=BORDER_MUTED, thickness=0.5,
            visible=(s.source not in AppState.LOCAL_SOURCES),
        )

        _BTN_W, _BTN_H = 129, 44
        self._load_btn     = _primary_btn("Cargar",      ft.Icons.DOWNLOAD,   self._on_load,     width=_BTN_W, height=_BTN_H)
        self._transfer_btn = _ghost_btn(  "Transferir",  ft.Icons.SWAP_HORIZ, self._on_transfer, width=_BTN_W, height=_BTN_H)
        self._organize_btn = _ghost_btn(  "Organizar",   ft.Icons.SORT,       self._on_organize, width=_BTN_W, height=_BTN_H, disabled=True)
        self._split_btn    = _ghost_btn(  "Dividir",     ft.Icons.CALL_SPLIT, self._on_split,    width=_BTN_W, height=_BTN_H, disabled=True)

        actions = ft.Column([
            ft.Row([self._load_btn,  self._transfer_btn], spacing=6),
            ft.Row([self._organize_btn, self._split_btn], spacing=6),
        ], spacing=6)

        self._rl_banner = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.TIMER_OUTLINED, color=WARNING, size=14),
                ft.Text("", size=10, color=WARNING, font_family="IBM Plex Sans", opacity=1.0),
            ], spacing=6),
            bgcolor=BG_PANEL, border=ft.Border.all(0.8, WARNING),
            border_radius=8, padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            visible=False,
        )

        self._progress_bar = ft.ProgressBar(value=0, bgcolor=BG_SURFACE, color=ACCENT, border_radius=4)
        self._progress_row = ft.Container(
            content=ft.Column([
                self._progress_bar,
                ft.Row([ft.Text("", size=10, color=TEXT_MUTED, font_family="IBM Plex Sans", opacity=1.0)],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ], spacing=4),
            visible=False,
            border=ft.Border.all(0.8, BORDER_LIGHT), border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )

        self._telemetry = TelemetryDrawer(self.page, sidebar_width=300)

        fixed_top = ft.Column(controls=[
            logo,
            ft.Divider(height=1, color=BORDER_MUTED, thickness=0.5),
            platform_section,
            self._sp_connect_row,
            ft.Divider(height=1, color=BORDER_MUTED, thickness=0.5),
            self._playlist_section,
            self._playlist_divider,
            _section_label("ACCIONES"),
            actions,
        ], spacing=12)

        scrollable_bottom = ft.Column(controls=[
            self._rl_banner,
            self._telemetry.container,
        ], spacing=12, scroll=ft.ScrollMode.ADAPTIVE, expand=True)

        sidebar_col   = ft.Column(controls=[fixed_top, scrollable_bottom], spacing=12, expand=True)
        sidebar_stack = ft.Stack(controls=[sidebar_col, self._telemetry.handle], expand=True)

        self._sidebar = ft.Container(
            width=300, padding=ft.Padding.all(18),
            bgcolor=SIDEBAR_BG, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            border_radius=ft.BorderRadius.only(top_right=14, bottom_right=14),
            border=ft.Border.only(right=ft.BorderSide(1, BORDER_LIGHT)),
            content=sidebar_stack,
        )

    # ── MÉTODOS DE ORGANIZACIÓN Y DIVISIÓN ────────────────────────────

    def _on_organize(self, _e: ft.ControlEvent) -> None:
        """Abre el diálogo para organizar la lista de canciones."""
        _dd_field = ft.Dropdown(
            options=[
                ft.dropdown.Option(key="artist", text="Artista"),
                ft.dropdown.Option(key="album", text="Álbum"),
                ft.dropdown.Option(key="name", text="Título"),
                ft.dropdown.Option(key="duration_ms", text="Duración"),
                ft.dropdown.Option(key="platform", text="Plataforma")
            ],
            value="artist", label="Ordenar por", width=200,
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
        )
        _switch_rev = ft.Switch(label="Descendente", value=False, active_color=ACCENT)

        def _apply(_e):
            self.state.organize_sort([_dd_field.value], _switch_rev.value)
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Organizar lista", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=ft.Column([_dd_field, _switch_rev], tight=True, spacing=15),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda _: [setattr(dlg, 'open', False), self.page.update()],
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED})),
                ft.TextButton("Aplicar", on_click=_apply,
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT}))
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_SURFACE, shape=ft.RoundedRectangleBorder(radius=10)
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def _on_split(self, _e: ft.ControlEvent) -> None:
        """Abre el diálogo para dividir la lista maestra en segmentos."""
        _dd_field = ft.Dropdown(
            options=[
                ft.dropdown.Option(key="artist", text="Artista"),
                ft.dropdown.Option(key="album", text="Álbum"),
                ft.dropdown.Option(key="platform", text="Plataforma")
            ],
            value="artist", label="Agrupar por", width=200,
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
        )

        def _apply(_e):
            self.state.organize_split(_dd_field.value)
            dlg.open = False
            self.page.update()
            
        def _clear(_e):
            self.state.clear_split()
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Dividir lista", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=ft.Column([
                ft.Text("Agrupa tu playlist en segmentos independientes.", size=12, color=TEXT_MUTED),
                _dd_field
            ], tight=True, spacing=15),
            actions=[
                ft.TextButton("Limpiar División", on_click=_clear,
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: WARNING}),
                              visible=bool(self.state.segments)),
                ft.TextButton("Cancelar", on_click=lambda _: [setattr(dlg, 'open', False), self.page.update()],
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED})),
                ft.TextButton("Agrupar", on_click=_apply,
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT}))
            ],
            actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            bgcolor=BG_SURFACE, shape=ft.RoundedRectangleBorder(radius=10)
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()


    # ── BUILD CONTENT ──────────────────────────────────────────────────

    def _build_content(self) -> None:
        self._playlist_title = ft.Text(
            "Cargar una playlist", size=22, weight=ft.FontWeight.W_700,
            color=TEXT_PRIMARY, font_family="IBM Plex Sans", opacity=1.0,
        )
        self._track_count = ft.Text("", size=12, color=TEXT_MUTED, font_family="IBM Plex Sans", opacity=1.0)
        self._search_field = ft.TextField(
            hint_text="Buscar título, artista…", prefix_icon=ft.Icons.SEARCH,
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT,
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            border_radius=10, focused_border_color=ACCENT,
            width=240, height=38,
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            on_change=self._on_search_change,
        )
        self._segment_dd = ft.Dropdown(
            width=160, height=38,
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT, focused_border_color=ACCENT,
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12, font_family="IBM Plex Sans"),
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=0),
            on_select=lambda e: self.state.set_active_segment(e.control.value),
            visible=False,
            hint_text="Segmento..."
        )

        _ib_style = dict(icon_size=17, style=ft.ButtonStyle(
            padding=4, bgcolor={ft.ControlState.DEFAULT: ft.Colors.TRANSPARENT}))
        self._auth_yt = ft.IconButton(icon=ft.Icons.VIDEO_LIBRARY_OUTLINED, icon_color=TEXT_DIM,
                                      tooltip="YouTube Music · clic = validar sesión ahora",
                                      on_click=lambda _: asyncio.create_task(self._on_auth_probe("YouTube Music")),
                                      **_ib_style)
        self._auth_sp = ft.IconButton(icon=ft.Icons.MUSIC_NOTE, icon_color=TEXT_DIM,
                                      tooltip="Spotify · clic = validar sesión ahora",
                                      on_click=lambda _: asyncio.create_task(self._on_auth_probe("Spotify")),
                                      **_ib_style)
        self._auth_am = ft.IconButton(icon=ft.Icons.APPLE, icon_color=TEXT_DIM,
                                      tooltip="Apple Music · clic = validar sesión ahora",
                                      on_click=lambda _: asyncio.create_task(self._on_auth_probe("Apple Music")),
                                      **_ib_style)
        self._auth_strip = ft.Row(controls=[self._auth_yt, self._auth_sp, self._auth_am],
                                  spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self._select_all_chk = ft.Checkbox(
            label="Todo",
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11, font_family="IBM Plex Sans"),
            fill_color={ft.ControlState.SELECTED: ACCENT},
            check_color=TEXT_PRIMARY,
            border_side=ft.BorderSide(1.5, TEXT_DIM),
            on_change=lambda _: self.state.toggle_select_all(),
        )

        self._content_progress_bar = ft.ProgressBar(value=0, bgcolor=BG_SURFACE, color=ACCENT, border_radius=4)
        self._content_prog_label   = ft.Text("", size=10, color=TEXT_MUTED, font_family="IBM Plex Sans", opacity=0.6)
        self._content_eta_label    = ft.Text("", size=10, color=TEXT_DIM,   font_family="IBM Plex Sans", opacity=0.45)
        self._content_progress = ft.Container(
            content=ft.Column([
                self._content_progress_bar,
                ft.Row([self._content_prog_label, ft.Container(expand=True), self._content_eta_label], spacing=0),
            ], spacing=4),
            visible=False,
            border=ft.Border.all(0.8, BORDER_LIGHT), border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )

        header_bar = ft.Row(controls=[
            ft.Column([self._playlist_title, self._track_count], spacing=2),
            ft.Container(expand=True),
            self._segment_dd, self._auth_strip, self._search_field, self._select_all_chk,
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER)

        def _col_header(text, width=None, expand=False, center=False):
            align = ft.Alignment.CENTER if center else ft.Alignment.CENTER_LEFT
            ctrl  = ft.Text(text, size=9, color=TEXT_DIM, weight=ft.FontWeight.W_700,
                            font_family="IBM Plex Sans", style=ft.TextStyle(letter_spacing=0.8),
                            text_align=ft.TextAlign.CENTER if center else ft.TextAlign.LEFT, opacity=1.0)
            return ft.Container(content=ctrl, width=width, expand=expand, alignment=align)

        col_headers = ft.Container(
            content=ft.Row(controls=[
                _col_header("#",               width=32, center=True),
                _col_header("PORTADA",         width=55, center=True),
                _col_header("TÍTULO / ARTISTA", expand=True),
                _col_header("DUR.",            width=48, center=True),
                _col_header("",                width=26, center=True),
                _col_header("SEL.",            width=32, center=True),
            ], spacing=16, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=16, vertical=8),
            bgcolor=CHIP_BG, border_radius=12,
            border=ft.Border.all(0.8, BORDER_LIGHT),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

        self._list_view = ft.ListView(item_extent=ITEM_H, spacing=0, expand=True,
                                      padding=ft.Padding.only(bottom=20))
        _sf = dict(left=0, top=0, right=0, bottom=0)
        self._list_view_wrap     = ft.Container(content=self._list_view, bgcolor=BG_LIST, visible=False, **_sf)
        self._skeletons          = [SkeletonRow(i) for i in range(self.SKELETON_COUNT)]
        self._skeleton_view      = ft.ListView(item_extent=ITEM_H, spacing=0, expand=True,
                                               controls=self._skeletons, visible=True)
        self._skeleton_view_wrap = ft.Container(content=self._skeleton_view, bgcolor=BG_LIST, visible=False, **_sf)

        self._empty_hint_text = ft.Text(
            "Introduce el ID en el panel izquierdo y pulsa «Cargar».",
            size=12, color=TEXT_DIM, font_family="IBM Plex Sans", opacity=1.0, text_align=ft.TextAlign.CENTER,
        )
        self._empty_state = ft.Container(
            bgcolor=BG_LIST,
            content=ft.Column(controls=[
                ft.Container(content=ft.Icon(ft.Icons.LIBRARY_MUSIC, size=52, color=TEXT_DIM),
                             bgcolor=CHIP_BG, border=ft.Border.all(0.8, BORDER_LIGHT),
                             border_radius=20, padding=ft.Padding.all(20)),
                ft.Text("Carga una playlist", size=20, color=TEXT_PRIMARY, font_family="IBM Plex Sans",
                        weight=ft.FontWeight.W_700, opacity=1.0),
                ft.Text("Sin playlist cargada", size=14, color=TEXT_MUTED, font_family="IBM Plex Sans",
                        weight=ft.FontWeight.W_500, opacity=1.0),
                self._empty_hint_text,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
               alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            alignment=ft.Alignment.CENTER, visible=True, **_sf,
        )

        self._error_text  = ft.Text("", size=13, color=ERROR_COL, font_family="IBM Plex Sans", opacity=1.0)
        self._error_state = ft.Container(
            bgcolor=BG_LIST,
            content=ft.Column([ft.Icon(ft.Icons.ERROR_OUTLINE, size=48, color=ERROR_COL), self._error_text],
                              horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            alignment=ft.Alignment.CENTER, visible=False, **_sf,
        )

        list_area = ft.Stack(controls=[
            self._list_view_wrap, self._skeleton_view_wrap, self._empty_state, self._error_state,
        ], expand=True)

        self._content = ft.Container(
            expand=True, bgcolor=BG_LIST, padding=ft.Padding.all(24),
            content=ft.Column(controls=[self._content_progress, header_bar, col_headers, list_area],
                              spacing=10, expand=True),
        )


    # ── STATE REACTIONS ────────────────────────────────────────────────

    def _on_state_changed(self) -> None:
        s = self.state
        is_local_src = s.source in AppState.LOCAL_SOURCES

        if self._playlist_section.visible != (not is_local_src):
            self._playlist_section.visible = not is_local_src
            self._playlist_divider.visible = not is_local_src
            self._playlist_section.update()
            self._playlist_divider.update()

        _hint_map = {
            "Archivo Local": "Pulsa «Cargar» para abrir el explorador de archivos.",
            "Pegar Texto":   "Pulsa «Cargar» para pegar tu lista de canciones.",
        }
        new_hint = _hint_map.get(s.source, "Introduce el ID en el panel izquierdo y pulsa «Cargar».")
        if self._empty_hint_text.value != new_hint:
            self._empty_hint_text.value = new_hint
            self._empty_hint_text.update()

        dest_needs_confirm = is_local_src and not s.destination_confirmed
        new_dst_border = WARNING if dest_needs_confirm else BORDER_LIGHT
        if self._dst_dd.border_color != new_dst_border:
            self._dst_dd.border_color         = new_dst_border
            self._dst_dd.focused_border_color = ACCENT if not dest_needs_confirm else WARNING
            self._dst_dd.update()

        self._playlist_title.value = s.playlist_name
        n     = len(s.display_tracks)
        total = len(s.tracks)
        self._track_count.value = (
            f"{n} canciones" if not s.search_query else f"{n} de {total} coincidencias"
        )

        for plat, ic in (("YouTube Music", self._auth_yt), ("Spotify", self._auth_sp), ("Apple Music", self._auth_am)):
            ok = s.auth_session_ok.get(plat, True)
            ic.icon_color = SUCCESS if ok else ERROR_COL
            hint = s.auth_session_hint.get(plat) or ""
            base = f"{plat}: clic para revalidar ahora"
            ic.tooltip = f"{base} · {hint}" if hint else f"{base} · {'OK' if ok else 'fallo'}"

        dest_ok = s.auth_session_ok.get(s.destination, True)
        self._dest_session_warn.visible = not dest_ok
        self._dest_session_warn.value   = "" if dest_ok else f"Sesión expirada en {s.destination}"

        sp_in_use = (s.source == "Spotify" or s.destination == "Spotify")
        if self._sp_connect_row.visible != sp_in_use:
            self._sp_connect_row.visible = sp_in_use
            self._sp_connect_row.update()
        if sp_in_use:
            sp_ok = bool(s.service._sp) and s.auth_session_ok.get("Spotify", False)
            self._sync_spotify_connect_ui(connected=sp_ok)
            new_border_col = SUCCESS if sp_ok else BORDER_LIGHT
            if getattr(self._sp_connect_row, "_border_col_cache", None) != new_border_col:
                self._sp_connect_row._border_col_cache = new_border_col  # pylint: disable=protected-access
                self._sp_connect_row.border = ft.Border.all(0.8, new_border_col)
                self._sp_connect_row.update()

        if s.source == s.destination:
            self._status_badge.value = "⚠ Origen y destino iguales"
            self._status_badge.color = WARNING
        else:
            self._status_badge.value = f"✓ {s.source} → {s.destination}"
            self._status_badge.color = SUCCESS

        self._select_all_chk.value = s.select_all

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

        has_tracks = len(s.tracks) > 0
        self._organize_btn.disabled = not has_tracks
        self._split_btn.disabled    = not has_tracks
        
        if s.segments:
            # Recrea opciones solo si cambiaron para no perder el foco
            current_options = [opt.key for opt in self._segment_dd.options] if self._segment_dd.options else []
            new_options = list(s.segments.keys())
            if current_options != new_options:
                self._segment_dd.options = [ft.dropdown.Option(k) for k in new_options]
            self._segment_dd.value = s.active_segment_key
            self._segment_dd.visible = True
        else:
            self._segment_dd.visible = False

        is_transferring  = s.transfer_state == TransferState.RUNNING
        is_transfer_done = s.transfer_state == TransferState.DONE
        is_transfer_err  = s.transfer_state == TransferState.ERROR
        xfer_active      = is_transferring or is_transfer_done or is_transfer_err
        is_scan_run      = getattr(s, "lazy_scan_running", False)
        is_scan_done     = getattr(s, "lazy_scan_done", False)
        idle_xfer        = s.transfer_state == TransferState.IDLE
        show_progress    = xfer_active or is_scan_run or (is_scan_done and idle_xfer)

        if is_transferring:
            if self._transfer_start == 0.0:
                self._transfer_start         = time.monotonic()
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
                    ok_n   = sum(1 for t in s.tracks if getattr(t, "transfer_status", "") == "found")
                    fail_n = sum(1 for t in s.tracks if getattr(t, "transfer_status", "") in ("not_found", "error"))
                    self._content_prog_label.value  = f"Búsqueda finalizada: {ok_n} Éxitos / {fail_n} Fallos"
                    self._content_prog_label.color  = _accent_ok
                    self._content_eta_label.value   = ""
                    self._content_progress.border   = ft.Border.all(0.9, _accent_ok)
                else:
                    self._content_prog_label.value  = f"Búsqueda en destino… {int(frac * 100)}%"
                    self._content_prog_label.color  = TEXT_MUTED
                    self._content_eta_label.value   = ""
                    self._content_progress.border   = ft.Border.all(0.8, BORDER_LIGHT)
            elif xfer_active:
                frac = (
                    s.count_confirmed / s.count_detected
                    if s.transfer_state == TransferState.DONE and s.count_detected
                    else s.transfer_progress / s.transfer_total
                )
                self._content_progress_bar.value = min(1.0, frac)
                fallidas   = len(s.failed_tracks)
                rechazadas = len(s.api_rejected_tracks)
                ejec       = len(s.transfer_error_tracks)
                porcentaje = int(frac * 100)
                if s.transfer_state == TransferState.DONE:
                    self._content_prog_label.value = f"Completado · {porcentaje}%"
                    self._content_prog_label.color = _accent_ok
                    self._content_eta_label.value  = ""
                    self._content_progress.border  = ft.Border.all(0.9, _accent_ok)
                    if not self._completion_snack_shown:
                        self._completion_snack_shown = True
                        fail_n = fallidas + rechazadas + ejec
                        _snack = ft.SnackBar(
                            content=ft.Text(
                                f"Transferencia completada: {s.count_confirmed} exitosas, {fail_n} errores",
                                color=ft.Colors.WHITE, font_family="IBM Plex Sans", size=12, opacity=1.0,
                            ),
                            action="Ver Detalles" if fail_n > 0 else None,
                            on_action=(lambda _: self._telemetry.show_postmortem()) if fail_n > 0 else None,
                            bgcolor=BG_PANEL, duration=6000,
                            behavior=ft.SnackBarBehavior.FLOATING, width=440,
                            show_close_icon=True, close_icon_color=ACCENT,
                        )
                        self.page.overlay.append(_snack)
                        _snack.open = True
                elif s.transfer_state == TransferState.ERROR:
                    self._content_prog_label.value = f"{porcentaje}%  ·  error · {fallidas + rechazadas} incidencias"
                    self._content_prog_label.color = WARNING
                    self._content_eta_label.value  = ""
                    self._content_progress.border  = ft.Border.all(0.8, WARNING)
                else:
                    eta_text = ""
                    if self._transfer_start > 0 and s.transfer_progress > 0:
                        elapsed   = time.monotonic() - self._transfer_start
                        remaining = s.transfer_total - s.transfer_progress
                        eta_s     = (elapsed / s.transfer_progress) * remaining
                        if 0 < eta_s < 3600:
                            eta_text = (f"~{int(eta_s)}s restantes" if eta_s < 60
                                        else f"~{int(eta_s // 60)}m {int(eta_s % 60)}s restantes")
                    self._content_prog_label.value = (
                        f"{porcentaje}%  ·  {s.count_processed} ok  /  {fallidas + rechazadas} errores"
                    )
                    self._content_prog_label.color = TEXT_MUTED
                    self._content_eta_label.value  = eta_text
                    self._content_progress.border  = ft.Border.all(0.8, BORDER_LIGHT)

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
                destination=s.destination, confirmed=s.count_confirmed, detected=s.count_detected,
            )
        if is_loading and not self._pm_cleared_for_load:
            self._pm_cleared_for_load = True
            self._telemetry.clear_postmortem()
        elif not is_loading:
            self._pm_cleared_for_load = False
        self._telemetry.sync_mode()

        net_blocked  = any(cb.is_open for cb in s.cb.values())
        rule4_blocked = is_local_src and not s.destination_confirmed
        self._load_btn.disabled     = net_blocked or is_loading
        self._transfer_btn.disabled = net_blocked or is_transferring or not is_ready or not dest_ok or rule4_blocked
        if rule4_blocked:
            self._transfer_btn.tooltip = "⚠ Elige un destino antes de transferir"
        elif not dest_ok:
            self._transfer_btn.tooltip = f"Sesión expirada en {s.destination}"
        else:
            self._transfer_btn.tooltip = "Transferir selección al destino"

        self.page.update()

    def _sync_list_view(self, tracks: list[Track]) -> None:
        lv           = self._list_view
        existing_ids = {c.track.id for c in lv.controls if hasattr(c, "track")}
        incoming_ids = {t.id for t in tracks}
        if existing_ids != incoming_ids:
            lv.controls.clear()
            self._row_cache.clear()
            for i, track in enumerate(tracks, 1):
                row = SongRow(track, i, self.state.toggle_track)
                self._row_cache[track.id] = row
                lv.controls.append(row)
        else:
            track_map = {t.id: t for t in tracks}
            for tid, row in self._row_cache.items():
                current = track_map.get(tid)
                if current:
                    row.refresh(current)


    # ── EVENT HANDLERS ─────────────────────────────────────────────────

    def _on_load(self, _) -> None:
        src = self.state.source
        if src in AppState.LOCAL_SOURCES and not self.state.destination_confirmed:
            self._snack("⚠ Selecciona primero una plataforma de Destino", error=True)
            self._dst_dd.border_color         = WARNING
            self._dst_dd.focused_border_color = WARNING
            self._dst_dd.update()
            return
        if src == "Archivo Local":
            asyncio.create_task(self._do_local_pick())
        elif src == "Pegar Texto":
            self._open_paste_dialog()
        else:
            asyncio.create_task(self._do_cloud_load())

    async def _do_cloud_load(self, _=None) -> None:
        pid = self._id_field.value.strip()
        if not pid:
            self._snack("Introduce un ID de playlist")
            return
        self._completion_snack_shown = False
        await self.state.load_playlist(pid)

    def _open_paste_dialog(self) -> None:
        self._paste_field.value = ""

        def _close_paste():
            paste_dlg.open = False
            self.page.update()

        def _process(_):
            text = self._paste_field.value or ""
            _close_paste()
            if not text.strip():
                self._snack("El campo de texto está vacío", error=True)
                return
            import datetime as _dt
            default_ts = _dt.datetime.now().strftime("%H:%M")
            self._ask_playlist_name_then_ingest(text=text, filename="",
                                                suggested_name=f"Local_Import_{default_ts}")

        paste_dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Pegar Texto", color=TEXT_PRIMARY, font_family="IBM Plex Sans",
                          size=14, weight=ft.FontWeight.W_700),
            content=ft.Container(content=self._paste_field, width=480, height=220),
            actions=[
                ft.TextButton("Procesar", icon=ft.Icons.PLAY_ARROW_OUTLINED, on_click=_process,
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT})),
                ft.TextButton("Cancelar", on_click=lambda _: _close_paste(),
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED})),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_PANEL, shape=ft.RoundedRectangleBorder(radius=14),
        )
        self.page.dialog = paste_dlg
        paste_dlg.open = True
        self.page.update()

    def _ask_playlist_name_then_ingest(self, text: str, filename: str, suggested_name: str) -> None:
        import datetime as _dt
        name_field = ft.TextField(
            value=suggested_name, hint_text=f"Ej. {suggested_name}",
            label="Nombre de la Playlist",
            hint_style=ft.TextStyle(color=TEXT_DIM, size=11),
            label_style=ft.TextStyle(color=TEXT_MUTED, size=10, font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=13, font_family="IBM Plex Sans"),
            bgcolor=BG_INPUT, border_color=BORDER_LIGHT, focused_border_color=ACCENT,
            border_radius=10, autofocus=True, on_submit=lambda _: _confirm(None),
        )

        def _close():
            name_dlg.open = False
            self.page.update()

        def _confirm(_):
            raw        = (name_field.value or "").strip()
            final_name = raw if raw else (suggested_name or f"Local_Import_{_dt.datetime.now().strftime('%H:%M')}")
            _close()
            self._ingest_text(text, label=final_name, filename=filename)

        name_dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.DRIVE_FILE_RENAME_OUTLINE, color=ACCENT, size=18),
                ft.Text("Nombra esta playlist", size=14, weight=ft.FontWeight.W_700,
                        color=TEXT_PRIMARY, font_family="IBM Plex Sans"),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column([
                    ft.Text("Asigna un nombre antes de importar. Si lo dejas vacío se usará el nombre sugerido.",
                            size=11, color=TEXT_MUTED, font_family="IBM Plex Sans"),
                    name_field,
                ], spacing=10, tight=True),
                width=400, padding=ft.Padding.only(top=6),
            ),
            actions=[
                ft.TextButton("Importar", icon=ft.Icons.CHECK_OUTLINED, on_click=_confirm,
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: ACCENT})),
                ft.TextButton("Cancelar", on_click=lambda _: _close(),
                              style=ft.ButtonStyle(color={ft.ControlState.DEFAULT: TEXT_MUTED})),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=BG_PANEL, shape=ft.RoundedRectangleBorder(radius=14),
        )
        self.page.dialog = name_dlg
        name_dlg.open = True
        self.page.update()

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
            self.state.log(f"[ERROR] No se pudo leer '{f.name}': {exc}")
            self._snack(f"Error leyendo archivo: {exc}", error=True)
            return
        base_name = os.path.splitext(os.path.basename(f.name))[0] or "Playlist Local"
        self._ask_playlist_name_then_ingest(text=text, filename=f.name, suggested_name=base_name)

    def _ingest_text(self, text: str, label: str = "", filename: str = "") -> None:
        try:
            pairs = parse_local_playlist(text, filename=filename or label)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.state.log(f"[ERROR] Parser ingesta: {exc}")
            self._snack(f"Error en el parser: {exc}", error=True)
            return
        if not pairs:
            self.state.log(f"[WARN] No se encontraron pistas en '{label or 'texto'}'")
            self._snack("No se reconocieron pistas en el archivo", error=True)
            return
        tracks = build_local_tracks(pairs)
        name   = label.strip() if label and label.strip() else "Playlist Local"
        self._completion_snack_shown = False
        self._pm_cleared_for_load    = True
        self._telemetry.clear_postmortem()
        self.state.load_local_tracks(tracks, playlist_name=name)
        self._snack(f"{len(tracks)} canciones importadas de '{name}'")
        self.state.log(f"[INFO] Ingesta completa · {len(tracks)} pistas desde '{label}'")

    async def _on_transfer(self, _) -> None:
        if self.state.source == self.state.destination:
            self._snack("Origen y destino no pueden ser iguales", error=True)
            return
        if self.state.source in AppState.LOCAL_SOURCES and not self.state.destination_confirmed:
            self._snack("⚠ Selecciona una plataforma de destino antes de transferir", error=True)
            self._dst_dd.border_color         = WARNING
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

    # ── Skeleton pulse ─────────────────────────────────────────────────

    def _ensure_skeletons_pulsing(self) -> None:
        if self._skeleton_tasks:
            return
        for sk in self._skeletons:
            self._skeleton_tasks.append(asyncio.create_task(sk.start_pulse()))

    def _stop_skeleton_pulse(self) -> None:
        for task in self._skeleton_tasks:
            task.cancel()
        self._skeleton_tasks.clear()
        for sk in self._skeletons:
            sk.stop_pulse()

    def stop(self) -> None:
        self._stop_skeleton_pulse()
        if self._search_task and not self._search_task.done():
            self._search_task.cancel()

    def start_auth_poll(self, task: asyncio.Task) -> None:
        self._auth_poll_task = task

    # ── Circuit breaker reactions ──────────────────────────────────────

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
                break
            await asyncio.sleep(1)

    # ── Helpers ────────────────────────────────────────────────────────

    def _snack(self, msg: str, error: bool = False) -> None:
        snack = ft.SnackBar(
            content=ft.Text(msg, color=ft.Colors.WHITE, font_family="IBM Plex Sans", size=12, opacity=1.0),
            bgcolor=ERROR_COL if error else BG_PANEL,
            duration=3000, behavior=ft.SnackBarBehavior.FLOATING,
            width=380, show_close_icon=True, close_icon_color=ACCENT,
        )
        self.page.overlay.append(snack)
        snack.open = True
        self.page.update()
