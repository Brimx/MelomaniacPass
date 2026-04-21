import customtkinter as ctk  # Framework de UI moderno basado en Tkinter. Maneja el escalado de alta resolución y temas
from tkinter import messagebox # Módulo estándar para diálogos de error/alerta (única pieza de Tkinter nativo necesaria).
import spotipy, webbrowser    # 'spotipy' es el cliente de la API de Spotify; 'webbrowser' permite abrir el navegador para el login.
from spotipy.oauth2 import SpotifyOAuth # Maneja el flujo de seguridad OAuth2 (intercambio de códigos por tokens de acceso).
from spotipy.exceptions import SpotifyException # Permite capturar errores específicos de Spotify (ej: token expirado, playlist no encontrada).
from dotenv import load_dotenv # Carga variables desde el archivo .env al entorno del sistema para proteger credenciales.
import os                      # Interfaz con el Sistema Operativo para leer las variables de entorno cargadas por dotenv.
import threading               # Permite ejecutar tareas pesadas (como buscar canciones) en un hilo paralelo para no congelar la UI.
import requests                # Cliente HTTP para realizar peticiones web manuales (útil para descargar imágenes o APIs simples).

# --- LIBRERÍAS DE INTEGRACIÓN Y LÓGICA ---
from ytmusicapi import YTMusic  
# Problema que resuelve: YouTube no tiene una API pública oficial para usuarios finales (solo para empresas).
# Flujo de datos: Emula una sesión de navegador enviando "headers" que le dicen a YouTube: "Soy un usuario real logueado".

from rapidfuzz import process, fuzz
# Problema que resuelve: La discrepancia de nombres entre plataformas (ej: "Song A" vs "Song A - Official Video").
# Flujo de datos: Algoritmo de comparación estadística que mide la similitud entre textos para encontrar coincidencias.

from PIL import Image, ImageTk
# Problema que resuelve: Tkinter nativo no soporta formatos modernos de imagen como WebP o JPEG de forma directa.
# Flujo de datos: Decodifica los bytes de las portadas de álbumes y los convierte en objetos que la UI puede dibujar.

load_dotenv()  # Lee credenciales desde .env

# ════════════════════════════════════════════════════════════════
# 1. AUTH
# ════════════════════════════════════════════════════════════════

class Auth:
    def __init__(self):
        self.spotify_auth = None
        self.sp = None

    def authenticate_spotify(self, client_id, client_secret, redirect_uri):
        self.spotify_auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="playlist-read-private playlist-modify-private",
            open_browser=True,
            cache_path=".spotify_cache"  # Guarda el token para no pedir login cada vez
        )
        return self.spotify_auth

    def login_spotify(self, client_id, client_secret, redirect_uri):
        try:
            oauth = self.authenticate_spotify(client_id, client_secret, redirect_uri)
            token_info = oauth.get_cached_token()

            if not token_info:
                auth_url = oauth.get_authorize_url()
                webbrowser.open(auth_url)
                # Diálogo CTk en lugar de input() para no bloquear la UI
                code = self._ask_auth_code()
                if not code:
                    return False
                token_info = oauth.get_access_token(code=code, check_cache=False)

            self.sp = spotipy.Spotify(auth=token_info["access_token"])
            print("Autenticación exitosa con Spotify.")
            return True
        except Exception as e:
            print("Error en autenticación de Spotify:", e)
            return False

    def _ask_auth_code(self):
        dialog = ctk.CTkInputDialog(
            text="Pega aquí el código de autorización que obtuviste:",
            title="Código de autorización"
        )
        return dialog.get_input()

    # ── YouTube Music ─────────────────────────────────────────────────────────
    def login_youtube(self):
        """
        Autentica con YouTube Music.
        - Si oauth.json ya existe en disco, lo reutiliza directamente.
        - Si no, ejecuta YTMusic.setup(filepath="oauth.json") que abre el navegador,
          pide al usuario que copie el código y lo guarda para futuras sesiones.
        Devuelve una instancia de YTMusic lista para usar, o None si falla.
        """
        try:
            if os.path.exists("oauth.json"):
                client = YTMusic("oauth.json")
                print("YouTube Music: sesión restaurada desde oauth.json")
                return client

            # Primera vez: guiar al usuario por el flujo OAuth
            print("YouTube Music: iniciando setup OAuth...")
            YTMusic.setup(filepath="oauth.json")          # genera oauth.json
            client = YTMusic("oauth.json")
            print("YouTube Music: autenticación exitosa.")
            return client
        except Exception as e:
            print(f"Error en autenticación de YouTube Music: {e}")
            return None

    # ── Apple Music ───────────────────────────────────────────────────────────
    def login_apple(self):
        """
        Autentica con Apple Music usando los tokens del .env:
          APPLE_DEV_TOKEN  → Bearer JWT generado en Apple Developer Portal
          APPLE_USER_TOKEN → Music User Token obtenido en el dispositivo Apple

        No hay flujo interactivo: si los tokens están en el .env, la sesión
        es válida inmediatamente. Devuelve el dict de headers o None si faltan.
        """
        dev_token  = os.getenv("APPLE_DEV_TOKEN")
        user_token = os.getenv("APPLE_USER_TOKEN")

        if not dev_token or not user_token:
            print("Error: APPLE_DEV_TOKEN o APPLE_USER_TOKEN no encontrados en .env")
            return None

        headers = {
            "Authorization":    f"Bearer {dev_token}",
            "Music-User-Token": user_token,
            "Content-Type":     "application/json",
        }
        print("Apple Music: tokens cargados correctamente.")
        return headers


# ════════════════════════════════════════════════════════════════
# 2. CLIENTES (Capa de Datos y APIs)
# ════════════════════════════════════════════════════════════════

class SpotifyClient:
    def __init__(self, auth: Auth):
        self.auth = auth
        self.sp = None

    def login(self):
        client_id     = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        redirect_uri  = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")
        success = self.auth.login_spotify(client_id, client_secret, redirect_uri)
        if success:
            self.sp = self.auth.sp
        return success

    def get_playlist_songs(self, playlist_id):
        if not self.sp: return[]
        
        try:
            # Paginación: Spotify solo devuelve 100 canciones por petición
            results = self.sp.playlist_tracks(playlist_id)
            tracks = results['items']
            while results['next']:
                results = self.sp.next(results)
                tracks.extend(results['items'])

            # NORMALIZACIÓN: Convertimos el formato complejo de Spotify a un diccionario simple
            normalized_songs =[]
            for item in tracks:
                track = item.get("track")
                if not track: continue
                
                # Convertir milisegundos a formato MM:SS
                millis = track['duration_ms']
                seconds = int((millis / 1000) % 60)
                minutes = int((millis / (1000 * 60)) % 60)

                normalized_songs.append({
                    "id": track["id"],
                    "name": track["name"],
                    "artist": track["artists"][0]["name"],
                    "album": track["album"]["name"],
                    "duration": f"{minutes}:{seconds:02d}",
                    "platform": "Spotify"
                })
            return normalized_songs
        except SpotifyException as e:
            print("Error al obtener playlist de Spotify:", e)
            return[]

    def search_song(self, title, artist):
        """Busca una canción y devuelve su URI de Spotify"""
        if not self.sp: return None
        query = f"track:{title} artist:{artist}"
        results = self.sp.search(q=query, type='track', limit=1)
        tracks = results.get('tracks', {}).get('items',[])
        if tracks:
            return tracks[0]['uri']
        return None

    def create_playlist(self, title, song_uris):
        if not self.sp: return False
        try:
            user_id = self.sp.current_user()['id']
            # 1. Crear la playlist vacía
            playlist = self.sp.user_playlist_create(user_id, title, public=False, description="Transferida por Playlist Manager")
            # 2. Añadir las canciones (Spotify permite máx 100 por petición, lo dividimos en fragmentos)
            for i in range(0, len(song_uris), 100):
                self.sp.playlist_add_items(playlist['id'], song_uris[i:i+100])
            return True, playlist['id']
        except Exception as e:
            return False, str(e)


class YouTubeMusicClient:
    """
    Cliente de YouTube Music.
    Usa la librería ytmusicapi. Delega la autenticación a Auth.login_youtube().
    """
    def __init__(self, auth: Auth):
        self.auth   = auth
        self.client = None

    def login(self):
        client = self.auth.login_youtube()
        if client:
            self.client = client
            return True
        return False

    def get_playlist_songs(self, playlist_id):
        if not self.client: return[]
        try:
            # ytmusicapi extrae las canciones automáticamente
            playlist = self.client.get_playlist(playlist_id, limit=None)
            
            normalized_songs =[]
            for track in playlist.get('tracks',[]):
                
                # Extraer artistas (puede ser una lista)
                artists = ", ".join([a['name'] for a in track.get('artists',[])])
                album = track.get('album', {})
                album_name = album.get('name', 'Single/Unknown') if album else 'Single/Unknown'

                normalized_songs.append({
                    "id": track["videoId"],
                    "name": track["title"],
                    "artist": artists,
                    "album": album_name,
                    "duration": track.get("duration", "0:00"), # YT devuelve el string directo "3:45"
                    "platform": "YouTube Music"
                })
            return normalized_songs
        except Exception as e:
            print("Error en YT Music:", e)
            return[]

    def search_song(self, title, artist):
        """Busca una canción y devuelve su Video ID usando fuzzy matching (umbral >85%)"""
        if not self.client: return None
        query = f"{title} {artist}"
        results = self.client.search(query, filter="songs", limit=5)
        
        if not results:
            return None
        
        source_str = f"{title} - {artist}"
        best_id    = None
        best_score = 0

        for r in results:
            r_title   = r.get("title", "")
            r_artists = ", ".join([a["name"] for a in r.get("artists", [])])
            candidate = f"{r_title} - {r_artists}"
            score = fuzz.token_sort_ratio(source_str.lower(), candidate.lower())
            if score > best_score:
                best_score = score
                best_id    = r.get("videoId")

        return best_id if best_score > 85 else None

    def create_playlist(self, title, video_ids):
        if not self.client: return False
        try:
            # YTMusic crea y añade en un solo paso
            playlist_id = self.client.create_playlist(title, "Transferida por Playlist Manager", video_ids=video_ids)
            return True, playlist_id
        except Exception as e:
            return False, str(e)


class AppleMusicClient:
    """
    Cliente de Apple Music.
    Los tokens (Bearer JWT + Music User Token) se leen del .env y se
    validan a través de Auth.login_apple(). No hay flujo interactivo.
    """
    def __init__(self, auth: Auth):
        self.auth     = auth
        self.base_url = "https://api.music.apple.com/v1"
        self.headers  = {}

    def login(self):
        headers = self.auth.login_apple()
        if headers:
            self.headers = headers
            return True
        return False

    def get_playlist_songs(self, playlist_id):
        if not self.headers: return[]
        try:
            # Endpoint de Apple para obtener playlist (requiere el storefront/país del usuario)
            # Simplificamos asumiendo storefront 'us' o que viene en el ID
            url = f"{self.base_url}/me/library/playlists/{playlist_id}/tracks"
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()

            normalized_songs =[]
            for item in data.get('data',[]):
                attrs = item.get('attributes', {})
                
                # Convertir milisegundos
                millis = attrs.get('durationInMillis', 0)
                seconds = int((millis / 1000) % 60)
                minutes = int((millis / (1000 * 60)) % 60)

                normalized_songs.append({
                    "id": item["id"],
                    "title": attrs.get("name", "Unknown"),
                    "artist": attrs.get("artistName", "Unknown"),
                    "album": attrs.get("albumName", "Unknown"),
                    "duration": f"{minutes}:{seconds:02d}",
                    "platform": "Apple Music"
                })
            return normalized_songs
        except Exception as e:
            print("Error en Apple Music:", e)
            return[]

    def search_song(self, title, artist):
        """Busca una canción en Apple Music con fuzzy matching (umbral >85%)"""
        if not self.headers: return None
        term = f"{title} {artist}".replace(" ", "+")
        url  = f"{self.base_url}/catalog/us/search?types=songs&term={term}&limit=5"
        
        try:
            res   = requests.get(url, headers=self.headers)
            data  = res.json()
            songs = data.get("results", {}).get("songs", {}).get("data", [])
            if not songs:
                return None

            source_str = f"{title} - {artist}"
            best_id    = None
            best_score = 0

            for song in songs:
                attrs      = song.get("attributes", {})
                candidate  = f"{attrs.get('name', '')} - {attrs.get('artistName', '')}"
                score = fuzz.token_sort_ratio(source_str.lower(), candidate.lower())
                if score > best_score:
                    best_score = score
                    best_id    = song["id"]

            return best_id if best_score > 85 else None
        except Exception:
            return None

    def create_playlist(self, title, track_ids):
        if not self.headers: return False
        url = f"{self.base_url}/me/library/playlists"
        
        # Formato específico que pide Apple
        tracks_data =[{"id": tid, "type": "songs"} for tid in track_ids]
        payload = {
            "attributes": {
                "name": title,
                "description": "Transferida por Playlist Manager"
            },
            "relationships": {
                "tracks": {"data": tracks_data}
            }
        }
        
        try:
            res = requests.post(url, headers=self.headers, json=payload)
            res.raise_for_status()
            new_id = res.json()['data'][0]['id']
            return True, new_id
        except Exception as e:
            return False, str(e)
# ════════════════════════════════════════════════════════════════
# 3. LÓGICA PRINCIPAL
# ════════════════════════════════════════════════════════════════

class Main:
    PLATFORMS = {
        "Spotify":       True,
        "YouTube Music": True,
        "Apple Music":   True,
    }

    def __init__(self):
        self.auth           = Auth()
        self.spotify_client = SpotifyClient(self.auth)
        self.youtube_client = YouTubeMusicClient(self.auth)
        self.apple_client   = AppleMusicClient(self.auth)

        self._client_map = {
            "Spotify":       self.spotify_client,
            "YouTube Music": self.youtube_client,
            "Apple Music":   self.apple_client,
        }

    def get_client(self, platform: str):
        return self._client_map.get(platform)

    def start_transfer(self, source: str, dest: str, playlist_id: str, ui_ref):
        """
        Lanza la transferencia completa en un hilo secundario para no bloquear la UI.
        Flujo: Login → Obtener canciones → Mostrar en UI → Buscar equivalencias → Crear playlist.
        Todas las actualizaciones de UI se delegan via root.after(0, ...) para ser thread-safe.
        """
        def _worker():
            log  = lambda msg: ui_ref.root.after(0, lambda m=msg: ui_ref._log(m))
            done = lambda ok, msg: ui_ref.root.after(0, lambda: ui_ref._on_transfer_done(ok, msg))

            try:
                src_client = self.get_client(source)
                dst_client = self.get_client(dest)

                # ── 1. Login en origen ──────────────────────────────────────────
                log(f"🔑 Iniciando sesión en {source}...")
                if not src_client.login():
                    done(False, f"✗ No se pudo autenticar en {source}.")
                    return

                # ── 2. Obtener canciones ────────────────────────────────────────
                log(f"📋 Obteniendo canciones de la playlist...")
                songs = src_client.get_playlist_songs(playlist_id)
                if not songs:
                    done(False, "✗ La playlist está vacía o el ID no es válido.")
                    return

                # ── 3. Mostrar canciones en la UI en tiempo real ────────────────
                for idx, song in enumerate(songs, 1):
                    ui_ref.root.after(0, lambda i=idx, s=song: ui_ref.add_song_row(i, s))
                log(f"✓ {len(songs)} canciones cargadas desde {source}.")

                # ── 4. Login en destino ─────────────────────────────────────────
                log(f"🔑 Iniciando sesión en {dest}...")
                if not dst_client.login():
                    done(False, f"✗ No se pudo autenticar en {dest}.")
                    return

                # ── 5. Buscar equivalencias en destino ──────────────────────────
                log(f"🔍 Buscando canciones en {dest}...")
                dest_ids = []
                not_found = 0
                for song in songs:
                    dest_id = dst_client.search_song(song["name"], song["artist"])
                    if dest_id:
                        dest_ids.append(dest_id)
                    else:
                        not_found += 1
                        log(f"  ⚠ No encontrada: {song['name']} – {song['artist']}")

                log(f"✓ {len(dest_ids)}/{len(songs)} canciones encontradas en {dest}.")

                if not dest_ids:
                    done(False, "✗ Ninguna canción pudo encontrarse en el destino.")
                    return

                # ── 6. Crear playlist en destino ────────────────────────────────
                log(f"📁 Creando playlist en {dest}...")
                ok, result = dst_client.create_playlist(
                    f"[Transferida] Playlist",
                    dest_ids
                )

                if ok:
                    done(True, f"✓ ¡Listo! {len(dest_ids)} canciones transferidas a {dest}.")
                else:
                    done(False, f"✗ Error al crear la playlist: {result}")

            except Exception as exc:
                done(False, f"✗ Error inesperado: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def transfer_playlist(self, source: str, dest: str, playlist_id: str):
        """Versión síncrona (legacy). Usada solo para tests sin UI."""
        if not self.PLATFORMS.get(source):
            return False, f"{source} aún no está implementado."
        if not self.PLATFORMS.get(dest):
            return False, f"{dest} aún no está implementado."

        src_client = self.get_client(source)
        if not src_client.login():
            return False, f"No se pudo autenticar en {source}."

        songs = src_client.get_playlist_songs(playlist_id)
        if not songs:
            return False, "No se pudo obtener la playlist."

        return True, f"Se obtuvieron {len(songs)} canciones de {source}."

    def run(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        root = ctk.CTk()
        UI(root, self)
        root.mainloop()


# ════════════════════════════════════════════════════════════════
# 4. UI  — CustomTkinter
# ════════════════════════════════════════════════════════════════

AVAILABLE_PLATFORMS = list(Main.PLATFORMS.keys())

class _Tooltip:
    """Clase auxiliar para manejar Tooltips flotantes sin fugas de memoria."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event=None):
        if self.tooltip_window or not self.text:
            return
        
        # Obtenemos las coordenadas globales del ratón
        x = event.x_root + 15
        y = event.y_root + 15
        
        self.tooltip_window = ctk.CTkToplevel(self.widget)
        self.tooltip_window.wm_overrideredirect(True) # Quita los bordes de la ventana
        self.tooltip_window.wm_geometry(f"+{x}+{y}")
        self.tooltip_window.attributes("-topmost", True) # Fuerza a estar por encima de todo
        
        label = ctk.CTkLabel(
            self.tooltip_window, text=self.text,
            fg_color="#333333", text_color="white", corner_radius=6,
            padx=10, pady=5, font=ctk.CTkFont(size=11)
        )
        label.pack()

    def hide_tooltip(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy() # Destruye el objeto para liberar memoria
            self.tooltip_window = None

class UI:
    def __init__(self, root: ctk.CTk, main: Main):
        self.root = root
        self.main = main
        self.root.title("Playlist Manager")
        
        # --- Viewport Constraints (Tus medidas personalizadas) ---
        self.root.minsize(1100, 600) 
        self.root.geometry("1100x600") 
        self.root.resizable(True, True)

        # --- Estado de la UI ---
        self.song_vars =[] 
        self.image_cache = {} 
        self.album_widgets =[]    
        self.show_album = False    
        self.logs_visible = False  

        self._build_main_layout()

        # Patrón Observer para Diseño Responsivo
        self.root.bind("<Configure>", self._on_window_resize)

    def _build_main_layout(self):
        self.root.grid_columnconfigure(1, weight=1) 
        self.root.grid_rowconfigure(0, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self.root, width=350, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_propagate(False) 
        
        self.content_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        self.content_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)

        self._build_sidebar() 
        self._build_playlist_view() 

    def _build_sidebar(self):
        header = ctk.CTkFrame(self.sidebar_frame, corner_radius=0, fg_color="transparent")
        header.pack(fill="x", padx=30, pady=(24, 0))

        ctk.CTkLabel(header, text="🎵  Playlist Manager", font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(header, text="Transfiere y gestiona tus playlists", font=ctk.CTkFont(size=12), text_color="gray").pack(anchor="w", pady=(2, 0))

        self._divider(self.sidebar_frame)

        pf = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        pf.pack(fill="x", padx=30, pady=6)
        pf.columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(pf, text="Origen",  font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(pf, text="Destino", font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.source_var = ctk.StringVar(value=AVAILABLE_PLATFORMS[0])
        self.dest_var   = ctk.StringVar(value=AVAILABLE_PLATFORMS[1])

        ctk.CTkOptionMenu(pf, variable=self.source_var, values=AVAILABLE_PLATFORMS, command=self._on_platform_change).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ctk.CTkOptionMenu(pf, variable=self.dest_var, values=AVAILABLE_PLATFORMS, command=self._on_platform_change).grid(row=1, column=1, sticky="ew", pady=(4, 0), padx=(10, 0))

        self.status_label = ctk.CTkLabel(self.sidebar_frame, text="", font=ctk.CTkFont(size=11), text_color="gray")
        self.status_label.pack(padx=30, anchor="w", pady=(8, 0))
        self._update_status_badge()

        self._divider(self.sidebar_frame)

        id_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        id_frame.pack(fill="x", padx=30, pady=6)

        ctk.CTkLabel(id_frame, text="ID de la Playlist", font=ctk.CTkFont(weight="bold")).pack(anchor="w")
        self.playlist_id_entry = ctk.CTkEntry(id_frame, placeholder_text="Ej: 37i9dQZF1DXcBWIGoYBM5M", height=36)
        self.playlist_id_entry.pack(fill="x", pady=(6, 0))

        self._divider(self.sidebar_frame)

        actions = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        actions.pack(fill="x", padx=30, pady=6)

        self.transfer_btn = ctk.CTkButton(actions, text="⇄  Transferir Playlist", height=40, command=self.transfer_playlist)
        self.transfer_btn.pack(fill="x", pady=4)

        for label, cmd in[
            ("↻  Sincronizar Playlists", self.sync_playlists),
            ("⚡  Dividir Playlist",      self.split_playlist),
            ("✕  Eliminar Playlist",      self.delete_playlist),
        ]:
            ctk.CTkButton(actions, text=label, height=40, fg_color="transparent", border_width=1, text_color=("gray10", "gray90"), command=cmd).pack(fill="x", pady=4)

        self.progress = ctk.CTkProgressBar(self.sidebar_frame)
        self.progress.set(0)

        self.logs_container = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self._divider(self.logs_container)
        self.log_box = ctk.CTkTextbox(self.logs_container, height=100, state="disabled", font=ctk.CTkFont(size=11))
        self.log_box.pack(fill="x", padx=30, pady=(0, 16))

    def _build_playlist_view(self):
        top_bar = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        top_bar.pack(fill="x", pady=(0, 10))
        
        ctk.CTkLabel(top_bar, text="Contenido de la Playlist", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        
        self.select_all_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(top_bar, text="Seleccionar todo", variable=self.select_all_var, command=self._on_select_all).pack(side="right")

        self.header_row = ctk.CTkFrame(self.content_frame, fg_color="gray20", height=35, corner_radius=5)
        self.header_row.pack(fill="x", padx=5, pady=5)
        self.header_row.grid_propagate(False)
        
        # 1. INYECCIÓN DEL RESORTE: Añadimos una columna "spacer" invisible
        self.cols_config =[
            {"id": "num", "text": "#", "width": 30},
            {"id": "port", "text": "Portada", "width": 50},
            {"id": "title", "text": "Título", "width": 240},
            {"id": "artist", "text": "Artista", "width": 160},
            {"id": "spacer", "text": "", "width": 0}, # <--- EL RESORTE
            {"id": "album", "text": "Álbum", "width": 160},
            {"id": "dur", "text": "Duración", "width": 70},
            {"id": "status", "text": "Estado", "width": 60}
        ]
        
        self.header_labels = {}
        for idx, col in enumerate(self.cols_config):
            if col["id"] == "spacer":
                # Configuramos esta columna para que absorba todo el espacio extra (weight=1)
                spacer = ctk.CTkFrame(self.header_row, width=0, height=0, fg_color="transparent")
                spacer.grid(row=0, column=idx, sticky="ew")
                self.header_row.grid_columnconfigure(idx, weight=1)
                continue

            lbl = ctk.CTkLabel(self.header_row, text=col["text"], width=col["width"], font=ctk.CTkFont(size=11, weight="bold"), text_color="gray70", anchor="w")
            lbl.grid(row=0, column=idx, padx=5, pady=5)
            self.header_labels[col["id"]] = lbl

        self.header_labels["album"].grid_remove()

        self.scroll_songs = ctk.CTkScrollableFrame(self.content_frame, fg_color="transparent")
        self.scroll_songs.pack(fill="both", expand=True)

    def _create_truncated_label(self, parent, text, max_width, row, col, font_kwargs=None, **kwargs):
        font = ctk.CTkFont(**(font_kwargs or {}))
        safe_width = max_width - 5 
        
        display_text = text
        is_truncated = False
        
        if font.measure(text) > safe_width:
            is_truncated = True
            truncated = text
            while font.measure(truncated + "...") > safe_width and len(truncated) > 0:
                truncated = truncated[:-1]
            display_text = truncated + "..."
        
        lbl = ctk.CTkLabel(parent, text=display_text, width=max_width, font=font, anchor="w", **kwargs)
        lbl.grid(row=row, column=col, padx=5, pady=5)
        
        if is_truncated:
            _Tooltip(lbl, text) 
            
        return lbl

    def add_song_row(self, index, song_data):
        row = ctk.CTkFrame(self.scroll_songs, fg_color="transparent", height=50)
        row.pack(fill="x", pady=1)
        row.grid_propagate(False)

        # Col 0, 1, 2, 3
        ctk.CTkLabel(row, text=str(index), width=30, text_color="gray", anchor="w").grid(row=0, column=0, padx=5, pady=5)
        ctk.CTkLabel(row, text="🖼️", width=50, height=40, fg_color="gray25", corner_radius=4).grid(row=0, column=1, padx=5, pady=5)
        self._create_truncated_label(row, song_data['name'], max_width=240, row=0, col=2, font_kwargs={"weight": "bold"})
        self._create_truncated_label(row, song_data['artist'], max_width=160, row=0, col=3, text_color="gray80")
        
        # Col 4: EL RESORTE (Spacer)
        spacer = ctk.CTkFrame(row, width=0, height=0, fg_color="transparent")
        spacer.grid(row=0, column=4, sticky="ew")
        row.grid_columnconfigure(4, weight=1) # Le damos la orden de expandirse
        
        # Col 5: Álbum (Desplazado una posición por el resorte)
        album_lbl = self._create_truncated_label(row, song_data['album'], max_width=160, row=0, col=5, text_color="gray60")
        self.album_widgets.append(album_lbl)
        
        # Col 6 y 7: Duración y Estado (Empujados a la derecha por el resorte)
        ctk.CTkLabel(row, text=song_data['duration'], width=70, anchor="w").grid(row=0, column=6, padx=5, pady=5)

        var = ctk.BooleanVar(value=True)
        self.song_vars.append(var)
        ctk.CTkCheckBox(row, text="", variable=var, width=60).grid(row=0, column=7, padx=5, pady=5)

        if not self.show_album:
            album_lbl.grid_remove()

    # --- Responsive Viewport Logic ---
    def _on_window_resize(self, event):
        if event.widget == self.root:
            scaling = self.root._get_window_scaling()
            logical_width = event.width / scaling
            
            # 2. Tu Breakpoint personalizado: 1240px
            if logical_width < 1240 and self.show_album:
                self.show_album = False
                self._toggle_album_column(False)
            elif logical_width >= 1240 and not self.show_album:
                self.show_album = True
                self._toggle_album_column(True)

    def _toggle_album_column(self, show: bool):
        # Tkinter recuerda automáticamente que el álbum pertenece a la columna 5
        if show:
            self.header_labels["album"].grid()
            for widget in self.album_widgets:
                widget.grid()
        else:
            self.header_labels["album"].grid_remove()
            for widget in self.album_widgets:
                widget.grid_remove()

    # ════════════════════════════════════════════════════════════════
    # --- Métodos de Utilidad y Lógica de Negocio (Controladores) ---
    # ════════════════════════════════════════════════════════════════

    def _divider(self, parent):
        """
        Crea una línea separadora horizontal para mejorar la jerarquía visual (UI/UX).
        
        Concepto: Reusabilidad de Componentes. En lugar de instanciar un CTkFrame 
        con altura 1 cada vez que necesitamos una línea, centralizamos la lógica aquí.
        """
        ctk.CTkFrame(parent, height=1, fg_color="gray25").pack(fill="x", padx=30, pady=10)

    def _on_select_all(self):
        """
        Sincroniza el estado del checkbox maestro ("Seleccionar todo") con los checkboxes individuales.
        
        Flujo de datos: 
        1. Lee el valor booleano actual del checkbox maestro (self.select_all_var).
        2. Itera sobre la lista en memoria (self.song_vars) que contiene las referencias a cada canción.
        3. Actualiza el estado de cada variable, lo que automáticamente marca/desmarca la UI gracias al Data Binding de Tkinter.
        """
        val = self.select_all_var.get()
        for var in self.song_vars:
            var.set(val)

    def _on_platform_change(self, _=None): 
        """
        Manejador de eventos (Event Handler) que se dispara cuando el usuario cambia el origen o destino.
        Su única responsabilidad es delegar la validación visual a `_update_status_badge`.
        """
        self._update_status_badge()
    
    def _log(self, msg: str):
        """
        Sistema de registro (Logging) interno para la UI. Muestra mensajes al usuario.
        
        Arquitectura:
        - Lazy Rendering: Si es el primer log, inyecta el contenedor en la UI.
        - Read-Only State: Tkinter requiere que el Textbox esté en estado "normal" para inyectar texto, 
          y luego se devuelve a "disabled" para evitar que el usuario escriba en él.
        - Auto-Scroll: Usa `.see("end")` para que la vista siempre baje al último mensaje.
        """
        # 1. Renderizado perezoso: Solo dibuja la caja si no estaba visible
        if not self.logs_visible:
            self.logs_container.pack(fill="x")
            self.logs_visible = True

        # 2. Desbloquea el widget, inserta el texto, hace scroll al final y lo vuelve a bloquear
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"› {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _update_status_badge(self):
        """
        Validación de Formulario en Tiempo Real (Real-time Validation).
        
        Compara las selecciones de los menús desplegables con el diccionario `Main.PLATFORMS` 
        para verificar si la integración está desarrollada. Cambia el texto y el color del 
        indicador visual para dar feedback inmediato al usuario.
        """
        src = self.source_var.get()
        dst = self.dest_var.get()
        
        # Verificamos si las plataformas existen en nuestro diccionario de soporte
        src_ok = Main.PLATFORMS.get(src)
        dst_ok = Main.PLATFORMS.get(dst)
        
        if src == dst: 
            self.status_label.configure(text="⚠ Origen y destino iguales", text_color="orange")
        elif src_ok and dst_ok: 
            self.status_label.configure(text=f"✓ {src} → {dst} listos", text_color="#2CC985")
        else: 
            self.status_label.configure(text="⚠ Plataforma no implementada", text_color="orange")

    def _show_progress(self, show: bool):
        """
        Controla la visibilidad y animación de la barra de progreso.
        
        Concepto: Feedback Asíncrono. Cuando el backend está haciendo peticiones HTTP (I/O bound),
        la UI debe indicarle al usuario que el programa no se ha congelado, sino que está trabajando.
        """
        if show: 
            self.progress.pack(fill="x", padx=30, pady=(0, 6))
            self.progress.start() # Inicia la animación de la barra indeterminada
        else: 
            self.progress.stop()  # Detiene la animación para ahorrar CPU
            self.progress.pack_forget() # Oculta el widget del layout

    def transfer_playlist(self):
        """
        Punto de entrada (Controller) para el botón de transferencia.
        1. Valida los campos de la UI.
        2. Deshabilita el botón para evitar doble-click.
        3. Arranca la barra de progreso indeterminada.
        4. Limpia la tabla de canciones previa.
        5. Delega el trabajo pesado a Main.start_transfer (hilo secundario).
        """
        source = self.source_var.get()
        dest   = self.dest_var.get()
        p_id   = self.playlist_id_entry.get().strip()

        # ── Validación temprana ─────────────────────────────────────────────
        if not p_id:
            messagebox.showwarning("ID requerido", "Por favor, introduce el ID de la playlist.")
            return

        if source == dest:
            messagebox.showwarning("Plataformas iguales",
                                   "El origen y el destino no pueden ser la misma plataforma.")
            return

        # ── 1. Deshabilitar botón (evita doble-click mientras trabaja el hilo) ──
        self.transfer_btn.configure(state="disabled")

        # ── 2. Iniciar barra de progreso ────────────────────────────────────
        self._show_progress(True)

        # ── 3. Limpiar tabla de canciones y estado interno ──────────────────
        for widget in self.scroll_songs.winfo_children():
            widget.destroy()
        self.song_vars.clear()
        self.album_widgets.clear()

        # ── 4. Log inicial ──────────────────────────────────────────────────
        self._log(f"Iniciando transferencia: {source} → {dest}")

        # ── 5. Delegar al hilo secundario ───────────────────────────────────
        self.main.start_transfer(source, dest, p_id, self)

    def _on_transfer_done(self, ok, msg):
        """
        Callback (Función de retrollamada) que se ejecuta cuando el hilo de transferencia termina.
        
        Restaura el estado de la interfaz: oculta la barra de progreso, rehabilita el botón 
        de transferencia y registra el resultado final en los logs.
        """
        self._show_progress(False)
        self.transfer_btn.configure(state="normal")
        self._log(msg)

    # --- Stubs (Métodos placeholder para futuras implementaciones) ---
    def sync_playlists(self): 
        self._log("Sincronización iniciada...")
        
    def split_playlist(self): 
        self._log("División pendiente...")
        
    def delete_playlist(self): 
        self._log("Eliminación pendiente...")
    
# ════════════════════════════════════════════════════════════════
# 5. ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = Main()
    app.run()
