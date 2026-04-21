import customtkinter as ctk  # Framework de UI moderno basado en Tkinter. Maneja el escalado de alta resolución y temas
from tkinter import messagebox # Módulo estándar para diálogos de error/alerta (única pieza de Tkinter nativo necesaria).
import spotipy, webbrowser    # 'spotipy' es el cliente de la API de Spotify; 'webbrowser' permite abrir el navegador para el login.
from spotipy.oauth2 import SpotifyOAuth # Maneja el flujo de seguridad OAuth2 (intercambio de códigos por tokens de acceso).
from spotipy.exceptions import SpotifyException # Permite capturar errores específicos de Spotify (ej: token expirado, playlist no encontrada).
from dotenv import load_dotenv # Carga variables desde el archivo .env al entorno del sistema para proteger credenciales.
import os                      # Interfaz con el Sistema Operativo para leer las variables de entorno cargadas por dotenv.
import threading               # Permite ejecutar tareas pesadas (como buscar canciones) en un hilo paralelo para no congelar la UI.
import requests                # Cliente HTTP para realizar peticiones web manuales (útil para descargar imágenes o APIs simples).
import io

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
        Autentica con Apple Music usando los tokens del .env.
        Corregido para incluir cabeceras de contexto (Origin/Referer) y evitar el 401.
        """
        raw_bearer = os.getenv("APPLE_AUTH_BEARER", "").strip()
        user_token = os.getenv("APPLE_MUSIC_USER_TOKEN", "").strip()

        if not raw_bearer or not user_token:
            print("Error: APPLE_AUTH_BEARER o APPLE_MUSIC_USER_TOKEN no encontrados en .env")
            return None

        # Asegurar formato correcto del Bearer
        bearer = raw_bearer if raw_bearer.startswith("Bearer ") else f"Bearer {raw_bearer}"

        headers = {
            "Authorization":    bearer,
            "media-user-token": user_token,
            "x-apple-music-user-token": user_token, # Variante para asegurar compatibilidad
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Accept": "application/json",
            "Content-Type": "application/json",
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
        if not self.sp: return {"name": "Error", "tracks": []}
        
        try:
            # 1. Obtener el nombre de la playlist
            pl_info = self.sp.playlist(playlist_id, fields="name")
            playlist_name = pl_info.get('name', 'Spotify Playlist')

            # 2. Obtener las canciones con paginación
            results = self.sp.playlist_tracks(playlist_id)
            tracks = results['items']
            while results['next']:
                results = self.sp.next(results)
                tracks.extend(results['items'])

            normalized_songs = []
            for item in tracks:
                track = item.get("track")
                if not track or not track.get("id"): continue
                
                millis = track['duration_ms']
                seconds = int((millis / 1000) % 60)
                minutes = int((millis / (1000 * 60)) % 60)
                
                images = track['album'].get('images', [])
                img_url = images[0]['url'] if images else ""

                normalized_songs.append({
                    "id": track["id"],
                    "name": track["name"],
                    "artist": track["artists"][0]["name"],
                    "album": track["album"]["name"],
                    "duration": f"{minutes}:{seconds:02d}",
                    "img_url": img_url,
                    "platform": "Spotify"
                })
            
            return {"name": playlist_name, "tracks": normalized_songs}
        except Exception as e:
            print(f"Error en Spotify: {e}")
            return {"name": "Error", "tracks": []}

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
        if not self.client: return {"name": "Error", "tracks": []}
        try:
            # Obtenemos la playlist completa (limit=None para traer todas)
            playlist = self.client.get_playlist(playlist_id, limit=None)
            
            playlist_name = playlist.get('title', 'YouTube Playlist')
            
            normalized_songs = []
            for track in playlist.get('tracks', []):
                # Extraer artistas
                artists = ", ".join([a['name'] for a in track.get('artists', [])])
                
                # Extraer álbum
                album = track.get('album', {})
                album_name = album.get('name', 'Single/Unknown') if album else 'Single/Unknown'

                # Extraer miniatura
                thumbnails = track.get('thumbnails', [])
                img_url = thumbnails[-1]['url'] if thumbnails else ""

                normalized_songs.append({
                    "id": track["videoId"],
                    "name": track["title"],
                    "artist": artists,
                    "album": album_name,
                    "duration": track.get("duration", "0:00"),
                    "img_url": img_url,
                    "platform": "YouTube Music"
                })
            
            return {"name": playlist_name, "tracks": normalized_songs}
        except Exception as e:
            print(f"Error en YT Music: {e}")
            return {"name": "Error", "tracks": []}
        
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
    def __init__(self, auth: Auth):
        self.auth = auth
        self.base_url = "https://amp-api.music.apple.com/v1"
        self.headers = {}
        self.storefront = "us" # Valor por defecto

    def login(self):
        headers = self.auth.login_apple()
        if not headers: return False

        try:
            # 1. Validamos sesión y de paso OBTENEMOS TU PAÍS (Storefront)
            resp = requests.get(f"{self.base_url}/me/storefront", headers=headers, timeout=10)
            if resp.status_code == 200:
                self.headers = headers
                # Guardamos el ID del país (ej: 'co', 'us', 'es')
                self.storefront = resp.json().get('data', [{}])[0].get('id', 'us')
                print(f"Apple Music: Sesión iniciada en Storefront: {self.storefront}")
                return True
            return False
        except:
            return False

    def get_playlist_songs(self, playlist_id):
        if not self.headers: return {"name": "Error", "tracks": []}
        
        is_library = playlist_id.startswith("p.")
        # Para obtener el nombre, primero pedimos la playlist en sí
        info_url = f"{self.base_url}/me/library/playlists/{playlist_id}" if is_library else \
                   f"{self.base_url}/catalog/{self.storefront}/playlists/{playlist_id}"
        
        playlist_name = "Playlist Desconocida"
        try:
            r_info = requests.get(info_url, headers=self.headers)
            if r_info.status_code == 200:
                playlist_name = r_info.json()['data'][0]['attributes'].get('name', 'Playlist')
        except: pass

        all_songs = []
        url = f"{info_url}/tracks" # URL inicial de canciones

        try:
            while url:
                full_url = url if "http" in url else f"https://amp-api.music.apple.com{url}"
                response = requests.get(full_url, headers=self.headers)
                response.raise_for_status()
                data = response.json()

                for item in data.get('data', []):
                    attrs = item.get('attributes', {})
                    img_url = attrs.get('artwork', {}).get('url', "").replace('{w}', '60').replace('{h}', '60')
                    millis = attrs.get('durationInMillis', 0)
                    
                    all_songs.append({
                        "id": item["id"],
                        "name": attrs.get("name", "Unknown"),
                        "artist": attrs.get("artistName", "Unknown"),
                        "album": attrs.get("albumName", "Unknown"),
                        "duration": f"{int((millis/1000)//60)}:{int((millis/1000)%60):02d}",
                        "img_url": img_url,
                        "platform": "Apple Music"
                    })
                url = data.get('next')
            return {"name": playlist_name, "tracks": all_songs}
        except:
            return {"name": playlist_name, "tracks": all_songs}

    def search_song(self, title, artist):
        if not self.headers: return None
        term = f"{title} {artist}".replace(" ", "+")
        # Usamos el storefront detectado para buscar
        url = f"{self.base_url}/catalog/{self.storefront}/search?types=songs&term={term}&limit=5"
        try:
            res = requests.get(url, headers=self.headers)
            songs = res.json().get("results", {}).get("songs", {}).get("data", [])
            if not songs: return None

            source_str = f"{title} - {artist}"
            best_id, best_score = None, 0
            for song in songs:
                attrs = song.get("attributes", {})
                candidate = f"{attrs.get('name', '')} - {attrs.get('artistName', '')}"
                score = fuzz.token_sort_ratio(source_str.lower(), candidate.lower())
                if score > best_score:
                    best_score, best_id = score, song["id"]
            return best_id if best_score > 85 else None
        except: return None

    def create_playlist(self, title, track_ids):
        if not self.headers: return False
        url = f"{self.base_url}/me/library/playlists"
        tracks_data = [{"id": str(tid), "type": "songs"} for tid in track_ids]
        payload = {
            "attributes": {"name": title, "description": "Transferida por Playlist Manager"},
            "relationships": {"tracks": {"data": tracks_data}}
        }
        try:
            res = requests.post(url, headers=self.headers, json=payload)
            res.raise_for_status()
            return True, "Playlist creada"
        except Exception as e: return False, str(e)
        

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

    def ejecutar_transferencia_directa(self, dest_name, songs, ui_ref):
        """
        Transfiere una lista de canciones ya cargadas y seleccionadas al destino.
        Este es el método que usa tu botón 'Transferir Playlist'.
        """
        def _worker():
            # Helpers para escribir en la UI de forma segura desde el hilo
            def log(msg): ui_ref.root.after(0, lambda: ui_ref._log(msg))
            def done(ok, msg): ui_ref.root.after(0, lambda: ui_ref._on_transfer_done(ok, msg))

            try:
                dst_client = self.get_client(dest_name)
                
                log(f"🔑 Iniciando sesión en {dest_name}...")
                if not dst_client.login():
                    done(False, f"✗ Error de login en {dest_name}")
                    return

                dest_ids = []
                total = len(songs)
                
                log(f"🔍 Buscando {total} canciones en {dest_name}...")
                
                for i, song in enumerate(songs, 1):
                    # Actualizamos el log cada canción para dar feedback
                    log(f"[{i}/{total}] Buscando: {song['name']}")
                    
                    match_id = dst_client.search_song(song['name'], song['artist'])
                    if match_id:
                        dest_ids.append(match_id)

                if dest_ids:
                    log(f"📁 Creando playlist en {dest_name} con {len(dest_ids)} canciones...")
                    ok, res = dst_client.create_playlist("[Transferida] Playlist Manager", dest_ids)
                    
                    if ok:
                        done(True, f"✓ ¡Éxito! {len(dest_ids)} canciones transferidas a {dest_name}.")
                    else:
                        done(False, f"✗ Error al crear la playlist: {res}")
                else:
                    done(False, "✗ No se encontraron coincidencias en el destino.")
            
            except Exception as e:
                done(False, f"✗ Error crítico: {str(e)}")

        threading.Thread(target=_worker, daemon=True).start()

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
    _current_tooltip = None # Variable de clase para rastrear el tooltip activo

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event=None):
        # Si ya hay un tooltip activo, lo borramos con seguridad
        if _Tooltip._current_tooltip is not None:
            try:
                _Tooltip._current_tooltip.destroy()
            except:
                pass
        
        x = event.x_root + 15
        y = event.y_root + 15
        
        tw = ctk.CTkToplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        
        label = ctk.CTkLabel(tw, text=self.text, fg_color="#333333", 
                             text_color="white", corner_radius=6,
                             padx=10, pady=5, font=ctk.CTkFont(size=11))
        label.pack()
        _Tooltip._current_tooltip = tw

    def hide_tooltip(self, event=None):
        if _Tooltip._current_tooltip:
            try:
                _Tooltip._current_tooltip.destroy()
            except:
                pass
            _Tooltip._current_tooltip = None

class UI:
    def __init__(self, root: ctk.CTk, main: Main):
        self.root = root
        self.main = main
        self.root.title("Playlist Manager")
        
        self.root.minsize(1100, 600) 
        self.root.geometry("1100x600") 
        self.root.resizable(True, True)

        # --- Estado de la UI y Optimización ---
        self.song_vars = [] 
        self.album_widgets = []    
        self.show_album = False    
        self.logs_visible = False  
        
        self.loaded_songs_data = [] # Mochila de datos (800 canciones)
        self.rendered_count = 0     # Cuántas filas hay dibujadas
        self.batch_size = 25        # Lote de dibujo
        self.is_loading_batch = False
        self.image_cache = {}       # Caché de portadas

        self._build_main_layout()
        
        # --- Eventos y Vinculaciones ---
        # 1. Enter en el ID para previsualizar
        self.playlist_id_entry.bind("<Return>", lambda event: self.load_preview())
        
        # 2. Scroll con la Rueda del Ratón (Vinculado al contenedor de canciones)
        self.scroll_songs.bind_all("<MouseWheel>", self._on_mousewheel_scroll)
        
        # 3. Scroll arrastrando la barra lateral
        original_command = self.scroll_songs._scrollbar.cget("command")
        def custom_scroll_command(*args):
            original_command(*args)
            self._on_scroll_check() 
        self.scroll_songs._scrollbar.configure(command=custom_scroll_command)
        
        # 4. Diseño Responsivo
        self.root.bind("<Configure>", self._on_window_resize)

    # ── DISEÑO DE INTERFAZ (LAYOUT) ──────────────────────────────────

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

        self.source_var = ctk.StringVar(value=AVAILABLE_PLATFORMS[2])
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

        for label, cmd in [
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
        
        # Guardamos referencia para actualizar el nombre y conteo
        self.playlist_info_label = ctk.CTkLabel(top_bar, text="Contenido de la Playlist", font=ctk.CTkFont(size=20, weight="bold"))
        self.playlist_info_label.pack(side="left")
        
        # Botón para cargar el resto de golpe (Turbo)
        self.load_all_btn = ctk.CTkButton(top_bar, text="⚡ Cargar todo", width=100, height=24, 
                                          fg_color="gray30", hover_color="gray40", command=self._force_load_all)
        self.load_all_btn.pack(side="left", padx=20)
        self.load_all_btn.pack_forget() # Oculto hasta que haya datos
        
        self.select_all_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(top_bar, text="Seleccionar todo", variable=self.select_all_var, command=self._on_select_all).pack(side="right")

        self.header_row = ctk.CTkFrame(self.content_frame, fg_color="gray20", height=35, corner_radius=5)
        self.header_row.pack(fill="x", padx=5, pady=5)
        self.header_row.grid_propagate(False)
        
        self.cols_config = [
            {"id": "num", "text": "#", "width": 30},
            {"id": "port", "text": "Portada", "width": 50},
            {"id": "title", "text": "Título", "width": 240},
            {"id": "artist", "text": "Artista", "width": 160},
            {"id": "spacer", "text": "", "width": 0}, 
            {"id": "album", "text": "Álbum", "width": 160},
            {"id": "dur", "text": "Duración", "width": 70},
            {"id": "status", "text": "Estado", "width": 60}
        ]
        
        self.header_labels = {}
        for idx, col in enumerate(self.cols_config):
            if col["id"] == "spacer":
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

    # ── RENDERIZADO DE FILAS ─────────────────────────────────────────

    def add_song_row(self, index, song_data):
        """Crea una fila física con altura estricta y protección de colapso."""
        # Frame contenedor con altura bloqueada
        row = ctk.CTkFrame(self.scroll_songs, fg_color="transparent", height=50)
        row.pack(fill="x", side="top", expand=False) # expand=False evita que se estiren
        row.pack_propagate(False) 

        # Separador visual (línea inferior)
        ctk.CTkFrame(row, height=1, fg_color="gray20").pack(side="bottom", fill="x")

        # Contenedor de contenido para manejar el padding sin afectar al padre
        content = ctk.CTkFrame(row, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=5)

        # Col 0: #
        ctk.CTkLabel(content, text=str(index), width=30, text_color="gray").grid(row=0, column=0, padx=5, pady=10)
        
        # Col 1: Portada
        img_label = ctk.CTkLabel(content, text="⌛", width=40, height=40, fg_color="gray25", corner_radius=4)
        img_label.grid(row=0, column=1, padx=5, pady=5)
        if song_data.get('img_url'):
            self._load_image_async(img_label, song_data['img_url'])

        # Col 2: Título
        self._create_truncated_label(content, song_data['name'], 240, 0, 2, {"weight": "bold"})
        
        # Col 3: Artista
        self._create_truncated_label(content, song_data['artist'], 160, 0, 3, text_color="gray80")

        # Col 4: Resorte
        spacer = ctk.CTkFrame(content, fg_color="transparent", width=0, height=0)
        spacer.grid(row=0, column=4, sticky="ew")
        content.grid_columnconfigure(4, weight=1)

        # Col 5: Álbum
        album_lbl = self._create_truncated_label(content, song_data['album'], 160, 0, 5, text_color="gray60")
        self.album_widgets.append(album_lbl)
        if not self.show_album: album_lbl.grid_remove()

        # Col 6: Duración
        ctk.CTkLabel(content, text=song_data['duration'], width=70).grid(row=0, column=6, padx=5)

        # Col 7: Checkbox
        var = ctk.BooleanVar(value=True)
        self.song_vars.append(var)
        ctk.CTkCheckBox(content, text="", variable=var, width=45).grid(row=0, column=7, padx=5)
    
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
        if is_truncated: _Tooltip(lbl, text) 
        return lbl

    # ── CONTROLADORES DE OPTIMIZACIÓN (SCROLL & BATCH) ────────────────

    def load_preview(self, event=None):
        source = self.source_var.get()
        p_id = self.playlist_id_entry.get().strip()
        if not p_id: return

        self._log(f"Descargando metadatos de {source}...")
        self._show_progress(True)
        
        # Reset
        for widget in self.scroll_songs.winfo_children(): widget.destroy()
        self.song_vars.clear()
        self.album_widgets.clear()
        self.loaded_songs_data = []
        self.rendered_count = 0
        self.batch_size = 50 # Turbo: de 50 en 50

        def _task():
            client = self.main.get_client(source)
            if client.login():
                data = client.get_playlist_songs(p_id) # Ahora recibe un dict
                if data["tracks"]:
                    self.loaded_songs_data = data["tracks"]
                    # Actualizar UI con nombre y cantidad
                    self.root.after(0, lambda: self.playlist_info_label.configure(
                        text=f"{data['name']} ({len(data['tracks'])} canciones)"))
                    self.root.after(0, lambda: self.load_all_btn.pack(side="left", padx=20))
                    
                    self.root.after(0, self._render_next_batch_safe)
                    self.root.after(0, self._log, f"✓ Lista cargada. Desliza o usa 'Cargar todo'.")
                else:
                    self.root.after(0, self._log, "✗ No se encontraron canciones.")
            self.root.after(0, self._show_progress, False)

        threading.Thread(target=_task, daemon=True).start()

    def _force_load_all(self):
        """Dibuja todas las canciones restantes de forma acelerada."""
        if self.rendered_count >= len(self.loaded_songs_data): return
        
        self.load_all_btn.configure(state="disabled", text="Cargando...")
        self.is_loading_batch = True
        
        # Aumentamos el lote al máximo restante
        self.batch_size = len(self.loaded_songs_data) - self.rendered_count
        
        # Reducimos el delay a 5ms para carga ultra rápida
        def _turbo_loop(current_idx):
            if current_idx >= len(self.loaded_songs_data):
                self.is_loading_batch = False
                self.load_all_btn.pack_forget()
                self._log("✓ Todas las canciones han sido renderizadas.")
                return
            
            self.add_song_row(current_idx + 1, self.loaded_songs_data[current_idx])
            self.rendered_count += 1
            
            # Cada 50 canciones, damos un respiro a la UI para que no se congele
            if current_idx % 50 == 0:
                self.root.after(5, lambda: _turbo_loop(current_idx + 1))
            else:
                _turbo_loop(current_idx + 1)

        _turbo_loop(self.rendered_count)

    def _on_mousewheel_scroll(self, event):
        """Maneja la rueda del ratón con velocidad aumentada y suavidad."""
        # Multiplicador agresivo para pantallas de alta resolución
        # En Windows, event.delta suele ser 120 o -120
        delta = -(event.delta / 120) * 20 
        self.scroll_songs._parent_canvas.yview_scroll(int(delta), "units")
        
        # Verificación de carga perezosa
        self._on_scroll_check()

    def _on_scroll_check(self):
        """Verifica si llegamos al final de forma más sensible."""
        if self.is_loading_batch or self.rendered_count >= len(self.loaded_songs_data):
            return
        
        # Obtenemos la posición del scrollbar
        _, bottom = self.scroll_songs._scrollbar.get()
        
        # Si pasamos del 75% de la lista, cargamos el siguiente lote
        if bottom > 0.75:
            self._render_next_batch_safe()
            
    def _render_next_batch_safe(self):
        """
        Dibuja canciones una a una en cascada rápida.
        Evita congelamientos al dar 'respiros' constantes a la CPU.
        """
        # Si ya estamos dibujando o ya terminamos, salimos
        if self.is_loading_batch or self.rendered_count >= len(self.loaded_songs_data):
            return

        self.is_loading_batch = True
        
        # Definimos un grupo pequeño para procesar (ej: 20 canciones)
        # Esto evita que el scroll se dispare infinitamente
        target_end = min(self.rendered_count + 20, len(self.loaded_songs_data))

        def _draw_step(current_idx):
            if current_idx >= target_end:
                # Terminamos este pequeño grupo, liberamos para el siguiente scroll
                self.is_loading_batch = False
                # Forzamos al canvas a reconocer su nuevo tamaño
                self.scroll_songs._parent_canvas.configure(
                    scrollregion=self.scroll_songs._parent_canvas.bbox("all")
                )
                return

            # Dibujar la fila física
            song = self.loaded_songs_data[current_idx]
            self.add_song_row(current_idx + 1, song)
            self.rendered_count += 1

            # EL SECRETO: Esperar solo 5ms antes de la siguiente canción.
            # Esto es suficiente para que la UI no se congele.
            self.root.after(5, lambda: _draw_step(current_idx + 1))

        _draw_step(self.rendered_count)
    
    def _load_image_async(self, label_widget, url):
        """Carga de imágenes con caché."""
        if not url: return
        if url in self.image_cache:
            label_widget.configure(image=self.image_cache[url], text="")
            return

        def _download():
            try:
                import io
                response = requests.get(url, timeout=3)
                img_data = Image.open(io.BytesIO(response.content))
                ctk_img = ctk.CTkImage(light_image=img_data, dark_image=img_data, size=(40, 40))
                self.image_cache[url] = ctk_img
                if label_widget.winfo_exists():
                    self.root.after(0, lambda: label_widget.configure(image=ctk_img, text=""))
            except: pass
        threading.Thread(target=_download, daemon=True).start()

    # ── LÓGICA DE NEGOCIO (TRANSFERENCIA) ────────────────────────────

    def transfer_playlist(self):
        """Transfiere solo las canciones seleccionadas en la tabla."""
        if not self.loaded_songs_data:
            messagebox.showwarning("Sin datos", "Primero carga una playlist con 'Enter'.")
            return

        dest = self.dest_var.get()
        selected_songs = [self.loaded_songs_data[i] for i, var in enumerate(self.song_vars) if var.get()]

        if not selected_songs:
            messagebox.showwarning("Selección vacía", "No hay canciones marcadas.")
            return

        self._log(f"Transfiriendo {len(selected_songs)} canciones a {dest}...")
        self.transfer_btn.configure(state="disabled")
        self._show_progress(True)
        self.main.ejecutar_transferencia_directa(dest, selected_songs, self)

    def _on_transfer_done(self, ok, msg):
        self._show_progress(False)
        self.transfer_btn.configure(state="normal")
        self._log(msg)

    # ── UTILIDADES DE UI ─────────────────────────────────────────────

    def _on_window_resize(self, event):
        if event.widget == self.root:
            scaling = self.root._get_window_scaling()
            logical_width = event.width / scaling
            if logical_width < 1240 and self.show_album:
                self.show_album = False
                self._toggle_album_column(False)
            elif logical_width >= 1240 and not self.show_album:
                self.show_album = True
                self._toggle_album_column(True)

    def _toggle_album_column(self, show: bool):
        if show:
            self.header_labels["album"].grid()
            for widget in self.album_widgets: widget.grid()
        else:
            self.header_labels["album"].grid_remove()
            for widget in self.album_widgets: widget.grid_remove()

    def _divider(self, parent):
        ctk.CTkFrame(parent, height=1, fg_color="gray25").pack(fill="x", padx=30, pady=10)

    def _on_select_all(self):
        val = self.select_all_var.get()
        for var in self.song_vars: var.set(val)

    def _on_platform_change(self, _=None): 
        self._update_status_badge()
        
    def _log(self, msg: str):
        """Muestra mensajes en la caja de registros de la interfaz."""
        if not self.logs_visible:
            self.logs_container.pack(fill="x")
            self.logs_visible = True

        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"› {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_progress(self, show: bool):
        """Controla la visibilidad y animación de la barra de progreso."""
        if show: 
            self.progress.pack(fill="x", padx=30, pady=(0, 6))
            self.progress.start()
        else: 
            self.progress.stop()
            self.progress.pack_forget()

    def _update_status_badge(self):
        src, dst = self.source_var.get(), self.dest_var.get()
        src_ok, dst_ok = Main.PLATFORMS.get(src), Main.PLATFORMS.get(dst)
        if src == dst: 
            self.status_label.configure(text="⚠ Origen y destino iguales", text_color="orange")
        elif src_ok and dst_ok: 
            self.status_label.configure(text=f"✓ {src} → {dst} listos", text_color="#2CC985")
        else: 
            self.status_label.configure(text="⚠ Plataforma no implementada", text_color="orange")

    def sync_playlists(self): self._log("Sincronización iniciada...")
    def split_playlist(self): self._log("División pendiente...")
    def delete_playlist(self): self._log("Eliminación pendiente...")
    
# ════════════════════════════════════════════════════════════════
# 5. ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = Main()
    app.run()
