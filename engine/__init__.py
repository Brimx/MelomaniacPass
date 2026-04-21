"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                    Paquete Engine                                    ║
╚══════════════════════════════════════════════════════════════════════╝

Paquete: engine
Descripción: Motor de procesamiento puro sin I/O. Implementa lógica de
            negocio para normalización de metadatos, matching fuzzy y
            parseo de playlists locales.

Módulos:
    - normalizer: Limpieza y normalización de metadatos de canciones
    - match: Sistema Hunter Recovery de matching fuzzy
    - parsers: Parsers multi-formato para playlists locales

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from engine.normalizer import clean_metadata, build_search_query
from engine.parsers import parse_local_playlist, build_local_tracks

__all__ = [
    'clean_metadata',
    'build_search_query',
    'parse_local_playlist',
    'build_local_tracks',
]
