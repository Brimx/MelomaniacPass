"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                    Paquete Services                                  ║
╚══════════════════════════════════════════════════════════════════════╝

Paquete: services
Descripción: Fachadas sobre APIs externas de plataformas de streaming.
            Abstrae la comunicación con Spotify, YouTube Music y Apple Music
            proporcionando una interfaz unificada.

Módulos:
    - api_service: MusicApiService - servicio unificado para todas las plataformas

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from services.api_service import MusicApiService

__all__ = [
    'MusicApiService',
]
