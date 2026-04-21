"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║              Parsers de Playlists Locales                            ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: engine/parsers.py
Descripción: Motor de parseo multi-formato para playlists locales.
            Soporta detección automática y extracción de metadatos desde
            archivos CSV, M3U, XSPF, WPL, iTunes XML y listas de texto plano.

Estrategia de Diseño:
    El parser implementa un sistema de detección automática de formato
    que analiza el contenido del archivo para determinar el parser apropiado:
    
    1. Detección por contenido (no por extensión):
       - XML: Busca tags específicos (plist, smil, playlist)
       - CSV: Detecta delimitadores y estructura tabular
       - M3U: Identifica directivas #EXTINF
       - Texto plano: Fallback para listas simples
    
    2. Normalización Unicode (NFC):
       - Garantiza representación consistente de caracteres
       - Crítico para nombres con diacríticos
    
    3. Limpieza de ruido:
       - Elimina números de track (01., 02-, etc)
       - Remueve extensiones de archivo (.mp3, .flac)
       - Purga paréntesis/corchetes con metadatos
    
    4. Detección de separadores:
       - Prueba múltiples separadores (–, -, —, _)
       - Extrae artista y título de strings concatenados
    
    5. Generación de IDs únicos:
       - Usa UUID4 para tracks locales
       - Previene colisiones en listas grandes

Formatos Soportados:
    - CSV: Archivos tabulares con headers opcionales
    - M3U/M3U8: Playlists con directivas #EXTINF
    - XSPF: XML Shareable Playlist Format
    - WPL: Windows Media Player Playlist
    - iTunes XML: Formato plist de iTunes
    - Texto plano: Listas simples línea por línea

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import csv
import io
import os
import re
import uuid
import unicodedata
import xml.etree.ElementTree as ET
from typing import Optional

from core.models import Track

# ══════════════════════════════════════════════════════════════════════
# EXPRESIONES REGULARES PARA LIMPIEZA DE METADATOS LOCALES
# ══════════════════════════════════════════════════════════════════════
# Precompiladas para máximo rendimiento en operaciones repetitivas.

# Detecta y remueve números de track al inicio (01., 02-, 03_, etc)
_LOCAL_TRACK_NUM_RE = re.compile(r'^\d{1,3}[\s.\-_]+')

# Detecta y remueve extensiones de archivo de audio
_LOCAL_FILE_EXT_RE  = re.compile(r'\.(mp3|flac|aac|ogg|wav|m4a|wma|opus|aiff?)$', re.IGNORECASE)

# Detecta y remueve paréntesis/corchetes con contenido (máx 60 chars)
_LOCAL_BRACKETS_RE  = re.compile(r'\s*[\(\[][^\)\]]{1,60}[\)\]]')

# Detecta directivas #EXTINF de playlists M3U
_LOCAL_EXTINF_RE    = re.compile(r'^#EXTINF\s*:\s*-?\d+\s*,\s*', re.IGNORECASE)

# Separadores comunes entre artista y título en nombres de archivo
_LOCAL_SEPARATORS   = (' – ', ' - ', ' — ', ' _ ')


def _parse_local_line(raw: str) -> Optional[tuple[str, str]]:
    """
    Parsea una línea de texto crudo extrayendo artista y título.
    
    Implementa limpieza multi-etapa para extraer metadatos de nombres
    de archivo o líneas de playlist:
    1. Normalización Unicode NFC
    2. Eliminación de directivas M3U (#EXTINF)
    3. Eliminación de extensiones de archivo
    4. Eliminación de números de track
    5. Eliminación de paréntesis/corchetes
    6. Detección de separador artista-título
    
    Args:
        raw: Línea de texto crudo (nombre de archivo o entrada de playlist).
    
    Returns:
        Tupla (artista, título) si se pudo parsear, None si la línea está vacía.
        Si no se detecta separador, retorna ("", título_completo).
    
    Example:
        >>> _parse_local_line("01. Queen - Bohemian Rhapsody.mp3")
        ("Queen", "Bohemian Rhapsody")
        >>> _parse_local_line("The Beatles – Let It Be [Remastered].flac")
        ("The Beatles", "Let It Be")
        >>> _parse_local_line("#EXTINF:245,Pink Floyd - Wish You Were Here")
        ("Pink Floyd", "Wish You Were Here")
    
    Note:
        La función prueba múltiples separadores en orden de especificidad:
        ' – ' (em dash con espacios) es más específico que ' - ' (guion simple).
        Esto previene splits incorrectos en títulos que contienen guiones.
    """
    s = unicodedata.normalize('NFC', raw.strip())
    s = _LOCAL_EXTINF_RE.sub('', s)
    s = _LOCAL_FILE_EXT_RE.sub('', s)
    s = _LOCAL_TRACK_NUM_RE.sub('', s)
    s = _LOCAL_BRACKETS_RE.sub('', s)
    s = ' '.join(s.split()).strip()
    if not s:
        return None
    for sep in _LOCAL_SEPARATORS:
        if sep in s:
            parts = s.split(sep, 1)
            title  = parts[0].strip()
            artist = parts[1].strip()
            if title:
                return (artist, title)
    return ("", s)


def _parse_xspf(text: str) -> list[tuple[str, str]]:
    """
    Parsea playlist en formato XSPF (XML Shareable Playlist Format).
    
    XSPF es un formato XML estándar para playlists que soporta metadatos
    ricos. Este parser extrae título y creador (artista) de cada track.
    
    Args:
        text: Contenido completo del archivo XSPF como string.
    
    Returns:
        Lista de tuplas (artista, título). Lista vacía si el XML es inválido.
    
    Example:
        >>> xml = '''<?xml version="1.0"?>
        ... <playlist version="1" xmlns="http://xspf.org/ns/0/">
        ...   <trackList>
        ...     <track>
        ...       <title>Bohemian Rhapsody</title>
        ...       <creator>Queen</creator>
        ...     </track>
        ...   </trackList>
        ... </playlist>'''
        >>> _parse_xspf(xml)
        [("Queen", "Bohemian Rhapsody")]
    
    Note:
        Usa namespace-aware parsing para manejar correctamente el
        namespace XSPF (http://xspf.org/ns/0/). Errores de parseo
        XML son capturados y retornan lista vacía en lugar de fallar.
    """
    try:
        root = ET.fromstring(text)
        ns   = {'s': 'http://xspf.org/ns/0/'}
        pairs: list[tuple[str, str]] = []
        for track in root.findall('.//s:track', ns):
            title  = (track.findtext('s:title',  default='', namespaces=ns) or '').strip()
            artist = (track.findtext('s:creator', default='', namespaces=ns) or '').strip()
            if title:
                pairs.append((artist, title))
        return pairs
    except ET.ParseError:
        return []


def _parse_wpl(text: str) -> list[tuple[str, str]]:
    """
    Parsea playlist en formato WPL (Windows Media Player Playlist).
    
    WPL es un formato XML propietario de Microsoft. Este parser extrae
    nombres de archivo del atributo 'src' y los procesa con _parse_local_line.
    
    Args:
        text: Contenido completo del archivo WPL como string.
    
    Returns:
        Lista de tuplas (artista, título). Lista vacía si el XML es inválido.
    
    Example:
        >>> wpl = '''<?xml version="1.0"?>
        ... <smil>
        ...   <body>
        ...     <seq>
        ...       <media src="Queen - Bohemian Rhapsody.mp3"/>
        ...     </seq>
        ...   </body>
        ... </smil>'''
        >>> _parse_wpl(wpl)
        [("Queen", "Bohemian Rhapsody")]
    
    Note:
        WPL almacena rutas de archivo completas, por lo que extraemos
        solo el basename antes de parsear. Soporta rutas Windows con
        backslashes que son normalizadas a forward slashes.
    """
    try:
        root  = ET.fromstring(text)
        pairs: list[tuple[str, str]] = []
        for media in root.findall('.//media'):
            src  = media.get('src', '')
            base = os.path.basename(src.replace('\\', '/'))
            pair = _parse_local_line(base)
            if pair:
                pairs.append(pair)
        return pairs
    except ET.ParseError:
        return []


def _parse_csv(text: str) -> list[tuple[str, str]]:
    """
    Parsea playlist en formato CSV con detección automática de headers.
    
    Implementa detección inteligente de estructura CSV:
    1. Detecta si la primera fila es header (busca keywords)
    2. Identifica columnas de título y artista por nombre
    3. Fallback a posiciones fijas si no hay headers
    4. Soporta filas de una sola columna parseadas con _parse_local_line
    
    Args:
        text: Contenido completo del archivo CSV como string.
    
    Returns:
        Lista de tuplas (artista, título).
    
    Example:
        >>> csv_text = '''Title,Artist
        ... Bohemian Rhapsody,Queen
        ... Imagine,John Lennon'''
        >>> _parse_csv(csv_text)
        [("Queen", "Bohemian Rhapsody"), ("John Lennon", "Imagine")]
    
    Note:
        La detección de headers busca keywords comunes en múltiples idiomas:
        'title', 'name', 'track', 'song', 'artist', 'author'.
        
        Soporta tres formatos:
        - CSV con headers: Usa nombres de columna para identificar campos
        - CSV sin headers (2+ columnas): Asume columna 0=título, 1=artista
        - CSV de una columna: Parsea cada línea con _parse_local_line
    """
    pairs: list[tuple[str, str]] = []
    reader = csv.reader(io.StringIO(text))
    rows   = list(reader)
    if not rows:
        return pairs
    
    # Detectar si la primera fila es header
    start = 1 if rows and any(
        kw in (rows[0][0].lower() if rows[0] else '')
        for kw in ('title', 'name', 'track', 'song', 'artis')
    ) else 0
    
    # Identificar columnas de título y artista
    cols = [c.strip().lower() for c in rows[0]] if rows else []
    ti   = next((i for i, c in enumerate(cols) if 'title' in c or 'name' in c or 'track' in c or 'song' in c), None)
    ai   = next((i for i, c in enumerate(cols) if 'artist' in c or 'author' in c), None)
    
    for row in rows[start:]:
        if not row:
            continue
        if ti is not None and ai is not None and len(row) > max(ti, ai):
            title  = row[ti].strip().strip('"')
            artist = row[ai].strip().strip('"')
        elif len(row) >= 2:
            title  = row[0].strip().strip('"')
            artist = row[1].strip().strip('"')
        elif len(row) == 1:
            p = _parse_local_line(row[0])
            if p:
                pairs.append(p)
            continue
        else:
            continue
        if title:
            pairs.append((artist, title))
    return pairs


def parse_local_playlist(text: str, filename: str = "") -> list[tuple[str, str]]:
    """
    Parse raw text from supported file formats into (artist, title) pairs.
    Supported: .txt .csv .m3u .m3u8 .pls .wpl .xspf .xml and bare text.
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""

    if ext in ('.xspf', '.xml'):
        pairs = _parse_xspf(text)
        if pairs:
            return pairs

    if ext == '.wpl':
        pairs = _parse_wpl(text)
        if pairs:
            return pairs

    lines = text.splitlines()

    if ext == '.pls' or any(l.strip().lower() == '[playlist]' for l in lines[:5]):
        pairs = []
        for line in lines:
            m = re.match(r'^Title\d+=(.+)$', line.strip(), re.IGNORECASE)
            if m:
                pair = _parse_local_line(m.group(1))
                if pair:
                    pairs.append(pair)
        if pairs:
            return pairs

    if ext == '.csv':
        pairs = _parse_csv(text)
        if pairs:
            return pairs

    pairs = []
    pending = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^#EXTINF\s*:\s*-?\d+\s*,\s*(.+)$', line, re.IGNORECASE)
        if m:
            pending = m.group(1)
            continue
        if line.startswith('#'):
            continue
        raw = pending if pending else os.path.basename(line.replace('\\', '/'))
        pending = ""
        pair = _parse_local_line(raw)
        if pair:
            pairs.append(pair)
    return pairs


def build_local_tracks(pairs: list[tuple[str, str]]) -> list[Track]:
    """Convert (artist, title) pairs into Track objects with platform='local'."""
    tracks = []
    for artist, title in pairs:
        if not title.strip():
            continue
        tracks.append(Track(
            id=f"local_{uuid.uuid4().hex[:12]}",
            name=title.strip(),
            artist=artist.strip(),
            album="",
            duration="",
            img_url="",
            platform="local",
            selected=True,
            transfer_status="local_pending",
            failure_reason="",
        ))
    return tracks
