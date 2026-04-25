"""
Motor de transformación de datos local para la gestión de playlists.
Proporciona funciones para ordenar y segmentar listas de canciones en memoria
sin realizar peticiones de red, optimizando la experiencia de usuario.
"""

from collections import defaultdict
from typing import Any, Dict, List

from core.models import Track


def _get_attr_safe(track: Track, key: str) -> Any:
    """Obtiene un atributo del Track de forma segura para ordenamiento."""
    val = getattr(track, key, None)
    if val is None:
        return ""
    if isinstance(val, str):
        return val.lower()
    return val


def sort_tracks(tracks: List[Track], keys: List[str], reverse: bool = False) -> List[Track]:
    """
    Ordena una lista de Tracks bajo criterios jerárquicos.
    
    Args:
        tracks: Lista maestra de canciones.
        keys: Lista de nombres de atributos por los cuales ordenar (ej: ['artist', 'album', 'name']).
        reverse: Si es True, ordena de forma descendente.
        
    Returns:
        Nueva lista ordenada (no muta la original).
    """
    if not tracks or not keys:
        return list(tracks)
        
    def sort_key(track: Track):
        return tuple(_get_attr_safe(track, k) for k in keys)
        
    return sorted(tracks, key=sort_key, reverse=reverse)


def split_tracks(tracks: List[Track], key: str) -> Dict[str, List[Track]]:
    """
    Agrupa la lista maestra basándose en un atributo específico.
    
    Args:
        tracks: Lista maestra de canciones.
        key: Nombre del atributo por el cual agrupar (ej: 'artist').
        
    Returns:
        Diccionario donde la clave es el valor del atributo y el valor es la lista de Tracks.
    """
    segments = defaultdict(list)
    for track in tracks:
        # Usar el valor original para mantener capitalización en las claves
        val = getattr(track, key, "Desconocido")
        if val is None or str(val).strip() == "":
            val = "Desconocido"
        segments[str(val)].append(track)
        
    return dict(segments)
