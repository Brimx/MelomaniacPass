"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                    Paquete UI                                        ║
╚══════════════════════════════════════════════════════════════════════╝

Paquete: ui
Descripción: Componentes de interfaz de usuario construidos con Flet.
            Implementa el sistema de diseño OLED con widgets reutilizables
            y componentes especializados.

Módulos:
    - widgets: Componentes UI reutilizables (botones, labels, iconos)
    - song_row: Componentes de fila de canción (SongRow, SkeletonRow)
    - telemetry: Panel de telemetría y monitoreo
    - main_ui: Interfaz principal de la aplicación (PlaylistManagerUI)

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from ui.widgets import _primary_btn, _ghost_btn, _section_label, _status_icon
from ui.song_row import SongRow, SkeletonRow, ITEM_H
from ui.main_ui import PlaylistManagerUI

__all__ = [
    '_primary_btn',
    '_ghost_btn',
    '_section_label',
    '_status_icon',
    'SongRow',
    'SkeletonRow',
    'ITEM_H',
    'PlaylistManagerUI',
]
