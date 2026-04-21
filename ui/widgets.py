"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                  Widgets UI Reutilizables                            ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: ui/widgets.py
Descripción: Biblioteca de componentes UI reutilizables para la interfaz
            de MelomaniacPass. Proporciona botones, labels e iconos con
            estilos consistentes siguiendo el sistema de diseño OLED.

Componentes:
    - _section_label: Labels de sección con tipografía uppercase y tracking
    - _primary_btn: Botón primario con fondo de acento y elevación
    - _ghost_btn: Botón secundario con borde y fondo transparente
    - _status_icon: Iconos de estado para tracking de transferencias

Sistema de Diseño:
    Implementa un sistema de tokens de diseño consistente con:
    - Paleta de colores optimizada para OLED (negros profundos)
    - Tipografía IBM Plex Sans con pesos y tamaños específicos
    - Animaciones sutiles (120ms) para feedback táctil
    - Estados interactivos (default, hover, pressed, disabled)
    - Elevación y sombras para jerarquía visual

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import flet as ft

# ══════════════════════════════════════════════════════════════════════
# TOKENS DE DISEÑO
# ══════════════════════════════════════════════════════════════════════
# Sistema de colores optimizado para pantallas OLED con alto contraste
# y reducción de fatiga visual en sesiones prolongadas.

TEXT_PRIMARY = "#FFF2F6FF"  # Texto principal de alta legibilidad
TEXT_MUTED   = "#FF7A8499"  # Texto secundario con opacidad reducida
TEXT_DIM     = "#FF3D4455"  # Texto terciario para labels y metadatos
ACCENT       = "#FF4F8BFF"  # Color de acento para elementos interactivos
ACCENT_DIM   = "#FF2D5FCC"  # Acento atenuado para estado pressed
ACCENT_HALO  = "#FF2A3F5C"  # Halo de sombra para elevación de acento
BG_HOVER     = "#FF1E1E28"  # Fondo de hover para elementos interactivos
SUCCESS      = "#FF00D084"  # Verde para estados exitosos
ERROR_COL    = "#FFFF4444"  # Rojo para estados de error
WARNING      = "#FFFFA500"  # Naranja para advertencias y revisiones


def _section_label(text: str) -> ft.Text:
    """
    Crea un label de sección con tipografía uppercase y letter-spacing.
    
    Utilizado para encabezados de secciones y categorías en la UI.
    Implementa tipografía condensada con tracking amplio (1.4px) para
    máxima legibilidad en tamaños pequeños.
    
    Args:
        text: Texto del label (se renderiza en uppercase automáticamente).
    
    Returns:
        Componente ft.Text configurado con estilos de sección.
    
    Example:
        >>> label = _section_label("PLAYLISTS")
        >>> # Renderiza: "PLAYLISTS" en gris dim con tracking amplio
    
    Note:
        El letter-spacing de 1.4px es crítico para legibilidad en
        tamaños de fuente pequeños (9pt). Sin tracking, las letras
        uppercase se perciben aglomeradas.
    """
    return ft.Text(
        text, size=9, color=TEXT_DIM,
        font_family="IBM Plex Sans",
        weight=ft.FontWeight.W_700,
        style=ft.TextStyle(letter_spacing=1.4),
        opacity=1.0,
    )


def _primary_btn(text: str, icon: str, on_click, width=None, height=None) -> ft.Button:
    """
    Crea un botón primario con fondo de acento y elevación en hover.
    
    Botón de acción principal con estados interactivos completos:
    - Default: Fondo azul acento sin elevación
    - Hover: Fondo azul claro con elevación 6 y sombra de halo
    - Pressed: Fondo azul oscuro sin elevación
    
    Args:
        text: Texto del botón.
        icon: Nombre del icono de Flet (ej: ft.Icons.PLAY_ARROW).
        on_click: Callback ejecutado al hacer clic.
        width: Ancho opcional del botón en píxeles.
        height: Alto opcional del botón en píxeles.
    
    Returns:
        Componente ft.Button configurado con estilos primarios.
    
    Example:
        >>> btn = _primary_btn(
        ...     "Transferir",
        ...     ft.Icons.UPLOAD,
        ...     on_click=lambda e: print("Transferir")
        ... )
    
    Note:
        La elevación en hover (6px) con sombra de halo proporciona feedback
        táctil visual que indica interactividad. La animación de 120ms
        es suficientemente rápida para sentirse responsiva sin ser abrupta.
    """
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
    """
    Crea un botón secundario con borde y fondo transparente.
    
    Botón de acción secundaria con énfasis visual reducido:
    - Default: Borde gris oscuro, texto muted
    - Hover: Borde azul acento, texto primary
    - Disabled: Opacidad reducida, no interactivo
    
    Args:
        text: Texto del botón.
        icon: Nombre del icono de Flet.
        on_click: Callback ejecutado al hacer clic.
        width: Ancho opcional del botón en píxeles.
        height: Alto opcional del botón en píxeles.
        disabled: Si True, el botón se renderiza deshabilitado.
    
    Returns:
        Componente ft.OutlinedButton configurado con estilos ghost.
    
    Example:
        >>> btn = _ghost_btn(
        ...     "Cancelar",
        ...     ft.Icons.CLOSE,
        ...     on_click=lambda e: print("Cancelar"),
        ...     disabled=False
        ... )
    
    Note:
        Los botones ghost son ideales para acciones secundarias o
        destructivas que no deben competir visualmente con la acción
        primaria. El cambio de borde a acento en hover proporciona
        feedback claro sin ser intrusivo.
    """
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


def _status_icon(status: str) -> ft.Control:
    """
    Retorna un icono de estado para tracking visual de transferencias.
    
    Mapea estados de transferencia a iconos y colores semánticos:
    - found: Check verde (canción encontrada en plataforma destino)
    - not_found: X roja (canción no encontrada)
    - searching: Loop azul (búsqueda en progreso)
    - transferred: Cloud verde (transferencia completada)
    - error: Error rojo (fallo en transferencia)
    - pending: Radio button gris (pendiente de procesar)
    - local_pending: Folder naranja (cargado desde archivo local)
    - revision_necesaria: Flag naranja (requiere revisión manual)
    
    Args:
        status: String identificando el estado de la canción.
    
    Returns:
        Componente ft.Icon con icono y color apropiados.
    
    Example:
        >>> icon = _status_icon("found")
        >>> # Retorna: Icono de check verde
        >>> icon = _status_icon("revision_necesaria")
        >>> # Retorna: Icono de flag naranja
    
    Note:
        Los colores semánticos son críticos para escaneo visual rápido
        en listas largas de canciones. Verde = éxito, Rojo = error,
        Naranja = atención requerida, Azul = en progreso, Gris = pendiente.
        
        El tamaño de 15px está optimizado para alineación vertical con
        texto de 13-14px en filas de canciones.
    """
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
