"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                    Paquete Core                                      ║
╚══════════════════════════════════════════════════════════════════════╝

Paquete: core
Descripción: Núcleo de la aplicación conteniendo modelos de datos y
            estado global. Define las estructuras fundamentales y la
            lógica de negocio central.

Módulos:
    - models: Dataclasses y enums (Track, SearchResult, LoadState, TransferState)
    - state: AppState - estado global de la aplicación

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from core.models import Track, SearchResult, LoadState, TransferState
from core.state import AppState

__all__ = [
    'Track',
    'SearchResult',
    'LoadState',
    'TransferState',
    'AppState',
]
