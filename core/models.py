"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                    Modelos de Datos del Core                         ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: core/models.py
Descripción: Define las estructuras de datos fundamentales del sistema.
            Contiene dataclasses y enums para representar canciones, resultados
            de búsqueda, y estados de carga/transferencia.

Componentes:
    - Track: Representación universal de una canción en cualquier plataforma
    - SearchResult: Resultado de búsqueda con metadatos de confianza ISRC-Master
    - LoadState: Estados del ciclo de carga de playlists
    - TransferState: Estados del proceso de transferencia entre plataformas

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


@dataclass
class Track:
    """
    Representación universal de una canción en el ecosistema MelomaniacPass.
    
    Abstrae las diferencias entre plataformas (Spotify, YouTube Music, Apple Music)
    proporcionando una interfaz común para manipular metadatos de canciones.
    
    Attributes:
        id: Identificador único de la canción en la plataforma de origen.
        name: Título de la canción.
        artist: Nombre del artista principal.
        album: Nombre del álbum al que pertenece.
        duration: Duración en formato legible (ej: "3:45").
        img_url: URL de la imagen de portada del álbum.
        platform: Plataforma de origen ("spotify", "youtube", "apple").
        selected: Indica si la canción está seleccionada para transferencia.
        transfer_status: Estado actual en el proceso de transferencia.
                        Valores: "pending", "searching", "found", "not_found",
                                "transferred", "error", "revision_necesaria"
        failure_reason: Descripción del error en caso de fallo (post-mortem).
                       Ejemplos: "Zero Results", "HTTP 429", "Timeout"
    
    Note:
        El campo transfer_status sigue un flujo de estados que permite
        tracking granular del proceso de transferencia y facilita debugging
        en caso de fallos.
    """
    id: str
    name: str
    artist: str
    album: str
    duration: str
    img_url: str
    platform: str
    selected: bool = True
    transfer_status: str = "pending"
    failure_reason: str = ""
    duration_ms: int = 0
    is_explicit: bool = False


@dataclass
class SearchResult:
    """
    Resultado de búsqueda universal con sistema ISRC-Master V5.0.
    
    Encapsula el resultado de una búsqueda en plataformas de streaming,
    incluyendo metadatos de confianza del matching fuzzy y código ISRC
    para identificación precisa de grabaciones.
    
    Attributes:
        track_id: ID de la canción encontrada en la plataforma destino.
                 None indica que no se encontró match válido.
        needs_review: Flag que indica si el match requiere revisión manual.
                     True cuando el score fuzzy es <40% tras Hunter Recovery.
        low_confidence: Flag para matches con confianza media (70-84%).
                       Válidos pero registrados para análisis interno.
        isrc: Código ISRC (ISO 3901) extraído de Spotify external_ids.
             Permite matching preciso entre plataformas cuando está disponible.
    
    Note:
        El sistema de confianza implementa tres niveles:
        - Alta (≥85%): Match automático sin flags
        - Media (70-84%): Match válido con low_confidence=True
        - Baja (<40%): Requiere revisión manual con needs_review=True
        
        El código ISRC es el estándar internacional para identificación
        única de grabaciones de audio, proporcionando matching exacto
        cuando ambas plataformas lo exponen.
    """
    track_id: Optional[str] = None
    needs_review: bool = False
    low_confidence: bool = False
    isrc: Optional[str] = None


class LoadState(Enum):
    """
    Estados del ciclo de vida de carga de playlists.
    
    Representa las fases del proceso de carga de una playlist desde
    una plataforma o archivo local, permitiendo feedback visual al usuario.
    
    Attributes:
        IDLE: Estado inicial, sin operación de carga en curso.
        LOADING_META: Cargando metadatos de la playlist (nombre, descripción, etc).
        LOADING_TRACKS: Cargando lista de canciones de la playlist.
        READY: Playlist completamente cargada y lista para operaciones.
        ERROR: Error durante el proceso de carga.
    """
    IDLE           = auto()
    LOADING_META   = auto()
    LOADING_TRACKS = auto()
    READY          = auto()
    ERROR          = auto()


class TransferState(Enum):
    """
    Estados del proceso de transferencia de playlists entre plataformas.
    
    Modela el ciclo de vida de una operación de transferencia, desde
    el inicio hasta la finalización o error.
    
    Attributes:
        IDLE: Sin transferencia en curso.
        RUNNING: Transferencia en progreso (búsqueda y agregado de canciones).
        DONE: Transferencia completada exitosamente.
        ERROR: Error crítico que detuvo la transferencia.
    
    Note:
        El estado RUNNING puede persistir durante varios minutos en
        playlists grandes, ya que cada canción requiere búsqueda y
        validación de match antes de ser agregada a la plataforma destino.
    """
    IDLE    = auto()
    RUNNING = auto()
    DONE    = auto()
    ERROR   = auto()
