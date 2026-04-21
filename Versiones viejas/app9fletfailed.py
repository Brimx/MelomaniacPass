"""
╔══════════════════════════════════════════════════════════════════════╗
║          Playlist Manager v2.0  —  Flet (Flutter for Python)        ║
║                                                                      ║
║  Architecture : BLoC-inspired (AppState ◄─ Service ◄─ UI)           ║
║  Design       : Glassmorphism · IBM Plex Sans · Dark Premium         ║
║  Performance  : Virtual ListView · asyncio · Circuit Breaker         ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §1  IMPORTS & ENVIRONMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import asyncio
import os
import time
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

import flet as ft
from dotenv import load_dotenv

# ── Optional heavy deps (graceful degradation) ────────────────────────
try:
    import spotipy
    from spotipy.cache_handler import CacheFileHandler
    import requests as _requests
    HAS_SPOTIFY = True
except ImportError:
    HAS_SPOTIFY = False

try:
    from ytmusicapi import YTMusic
    HAS_YTMUSIC = True
except ImportError:
    HAS_YTMUSIC = False

try:
    from rapidfuzz import fuzz as _fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

import requests  # always needed for Apple Music + Spotify shadow auth

load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §2  DATA MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Track:
    id: str
    name: str
    artist: str
    album: str
    duration: str
    img_url: str
    platform: str
    selected: bool = True
    # transfer_status:  pending | searching | found | not_found | transferred | error
    transfer_status: str = "pending"


class LoadState(Enum):
    IDLE          = auto()
    LOADING_META  = auto()   # fetching playlist name + count
    LOADING_TRACKS = auto()  # streaming tracks in
    READY         = auto()
    ERROR         = auto()


class TransferState(Enum):
    IDLE    = auto()
    RUNNING = auto()
    DONE    = auto()
    ERROR   = auto()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §3  CIRCUIT BREAKER  (Rate-Limit Protection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RateLimitError(Exception):
    def __init__(self, platform: str, retry_after: int):
        self.platform    = platform
        self.retry_after = retry_after
        super().__init__(f"Rate-limited by {platform}. Retry in {retry_after}s.")


class CircuitBreaker:
    """
    Pattern: Circuit Breaker
    ────────────────────────
    If a 429 is detected, the breaker *trips* and notifies subscribers.
    The UI disables all network buttons and shows a live countdown.
    The breaker auto-resets after `cooldown` seconds.

    Observer callbacks receive: (is_open: bool, remaining_seconds: int)
    """

    def __init__(self, platform: str, default_cooldown: int = 60):
        self.platform         = platform
        self.default_cooldown = default_cooldown
        self.is_open: bool    = False
        self._until: float    = 0.0
        self._callbacks: list[Callable[[bool, int], None]] = []

    # ── Public API ────────────────────────────────────────────────────

    def subscribe(self, cb: Callable[[bool, int], None]) -> None:
        self._callbacks.append(cb)

    def trip(self, retry_after: Optional[int] = None) -> None:
        """Called when an HTTP 429 is received."""
        wait         = retry_after or self.default_cooldown
        self.is_open = True
        self._until  = time.monotonic() + wait
        self._notify(True, wait)
        asyncio.create_task(self._auto_reset(wait))

    def check_or_raise(self) -> None:
        """Call before any network request. Raises RateLimitError if tripped."""
        if self.is_open:
            raise RateLimitError(self.platform, int(self.remaining))

    @property
    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic())

    # ── Private ───────────────────────────────────────────────────────

    def _notify(self, is_open: bool, remaining: int) -> None:
        for cb in self._callbacks:
            try:
                cb(is_open, remaining)
            except Exception:
                pass

    async def _auto_reset(self, wait: float) -> None:
        await asyncio.sleep(wait)
        self.is_open = False
        self._notify(False, 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §4  MOCK DATA  (Demo / Anti-Tofu Unicode Stress Test)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SEED_TRACKS = [
    # ─── Japanese City Pop (IBM Plex Sans JP stress test) ─────────────
    ("Plastic Love",               "竹内 まりや (Mariya Takeuchi)",     "VARIETY",              "6:02"),
    ("Ride on Time",               "山下 達郎 (Tatsuro Yamashita)",     "RIDE ON TIME",         "5:22"),
    ("真夜中のドア / Stay With Me", "松原 みき (Miki Matsubara)",        "POCKET PARK",          "4:58"),
    ("September",                  "竹内 まりや (Mariya Takeuchi)",     "REQUEST",              "5:31"),
    ("After 5 Clash",              "杏里 (Anri)",                      "TIMELY!!",             "4:07"),
    ("夜に駆ける",                  "YOASOBI",                          "THE BOOK",             "4:08"),
    ("ドライフラワー",               "優里 (Yuri)",                      "ドライフラワー",        "4:01"),
    ("Groovin' Magic",             "中原 めいこ (Meiko Nakahara)",      "Half Moon",            "4:30"),
    ("FUNKY FLUSHIN'",             "角松 敏生 (Toshiki Kadomatsu)",     "Sea Breeze",           "5:11"),
    ("I Love You So",              "山下 達郎 (Tatsuro Yamashita)",     "MELODIES",             "4:45"),
    # ─── Latin / Urban ────────────────────────────────────────────────
    ("Natural",                    "Imagine Dragons",                  "Origins",              "3:09"),
    ("La Bilirrubina",             "Juan Luis Guerra",                 "Ojalá que llueva...",  "4:05"),
    ("De la Vida Como Película",   "Canserbero",                       "Vida",                 "8:01"),
    ("A Dónde se Fue la Conciencia","Canserbero",                      "Vida",                 "3:36"),
    ("Sharks",                     "Imagine Dragons",                  "Mercury - Act 1",      "3:10"),
    # ─── Electronic / Indie ───────────────────────────────────────────
    ("Rhinestone Eyes",            "Gorillaz",                         "Plastic Beach",        "3:20"),
    ("Around the World",           "Daft Punk",                        "Homework",             "7:09"),
    ("Harder, Better, Faster, Stronger", "Daft Punk",                 "Discovery",            "3:45"),
    ("Get Lucky",                  "Daft Punk ft. Pharrell Williams",  "Random Access Memories","4:08"),
    ("Blinding Lights",            "The Weeknd",                       "After Hours",          "3:20"),
    ("Levitating",                 "Dua Lipa",                         "Future Nostalgia",     "3:23"),
    ("Enemy",                      "Imagine Dragons ft. JID",          "Arcane Season 1",      "2:53"),
    ("Discord",                    "The Living Tombstone",             "My Little Pony Single","3:13"),
    # ─── Classic Rock ─────────────────────────────────────────────────
    ("Bohemian Rhapsody",          "Queen",                            "A Night at the Opera", "5:55"),
    ("Hotel California",           "Eagles",                           "Hotel California",     "6:30"),
    ("Stairway to Heaven",         "Led Zeppelin",                     "Led Zeppelin IV",      "8:02"),
    ("It Was a Good Day",          "Ice Cube",                         "The Predator",         "4:19"),
    ("Dear Mama",                  "2Pac",                             "Me Against the World", "4:40"),
]

def _build_mock_tracks(n: int = 844) -> list[Track]:
    """Tile seed tracks to reach `n` total, with unique IDs."""
    base  = len(_SEED_TRACKS)
    tiles = math.ceil(n / base)
    result: list[Track] = []
    idx = 0
    for _ in range(tiles):
        for name, artist, album, dur in _SEED_TRACKS:
            if idx >= n:
                break
            result.append(Track(
                id=str(idx + 1), name=name, artist=artist,
                album=album, duration=dur, img_url="",
                platform="Apple Music", selected=True,
            ))
            idx += 1
    return result


MOCK_TRACKS: list[Track] = _build_mock_tracks(844)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §5  SPOTIFY SHADOW AUTH MANAGER  (async-compatible)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SpotifyShadowAuthManager:
    """
    Duck-typed auth_manager for Spotipy using the Spotify Web Player shadow API.

    Cache-Aside strategy (same as v1):
      DISK → return if valid
      NET  → fetch shadow token, persist to disk, return
    """

    def __init__(self, cache_handler):
        self.cache_handler = cache_handler

    def get_access_token(self, as_dict: bool = False):
        # Layer 0: Manual Bearer override (dev escape hatch)
        manual = os.getenv("SPOTIFY_MANUAL_BEARER", "").strip()
        if manual:
            token = manual.replace("Bearer ", "")
            return {"access_token": token} if as_dict else token

        # Layer 1: Disk cache
        info = self.cache_handler.get_cached_token()
        if info and time.time() < (info.get("expires_at", 0) - 300):
            tok = info["access_token"]
            return {"access_token": tok} if as_dict else tok

        # Layer 2: Shadow API
        sp_dc = os.getenv("SPOTIFY_SP_DC", "").strip()
        if not sp_dc:
            raise RuntimeError("SPOTIFY_SP_DC missing from .env")

        headers = {
            "Cookie": f"sp_dc={sp_dc}",
            "User-Agent": os.getenv(
                "SPOTIFY_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36",
            ),
            "App-Platform": "WebPlayer",
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
            "Accept": "application/json",
        }
        for env_key, header in [
            ("SPOTIFY_APP_VERSION", "Spotify-App-Version"),
            ("SPOTIFY_CLIENT_TOKEN", "Client-Token"),
        ]:
            val = os.getenv(env_key)
            if val:
                headers[header] = val

        resp = requests.get(
            "https://open.spotify.com/get_access_token"
            "?reason=transport&productType=web_player",
            headers=headers, timeout=10,
        )
        if resp.status_code == 403:
            raise RuntimeError("Spotify 403: refresh sp_dc and Client-Token.")
        resp.raise_for_status()
        data  = resp.json()
        exp_ms = data.get("accessTokenExpirationTimestampMs")
        expires_at = int(exp_ms / 1000) if exp_ms else int(time.time()) + 3300
        token = data.get("accessToken")
        if not token:
            raise RuntimeError("Shadow API returned no accessToken.")

        # Layer 3: Persist
        self.cache_handler.save_token_to_cache(
            {"access_token": token, "expires_at": expires_at}
        )
        return {"access_token": token} if as_dict else token


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §6  MUSIC API SERVICE  (unified async façade)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MusicApiService:
    """
    Unified async façade over Spotify, YouTube Music, and Apple Music.

    Design Decisions
    ────────────────
    • All public methods are `async`.  Blocking I/O is offloaded via
      `asyncio.to_thread()`, keeping the Flet event loop free.
    • Each platform has its own CircuitBreaker injected at construction.
    • Fuzzy matching uses rapidfuzz with a configurable threshold (85 %).
    • Pagination is handled internally; callers receive a flat list.
    """

    FUZZY_THRESHOLD = 85

    def __init__(self, circuit_breakers: dict[str, "CircuitBreaker"]):
        self._cb   = circuit_breakers   # platform → CircuitBreaker
        self._sp   = None               # spotipy.Spotify instance
        self._ytm  = None               # YTMusic instance
        self._am_headers: dict  = {}    # Apple Music request headers
        self._am_storefront: str = "us"

    # ── Authentication (all sync under the hood, wrapped async) ───────

    async def init_spotify(self) -> bool:
        return await asyncio.to_thread(self._sync_init_spotify)

    def _sync_init_spotify(self) -> bool:
        if not HAS_SPOTIFY:
            return False
        try:
            cache = CacheFileHandler(cache_path=".cache-shadow-spotify")
            mgr   = SpotifyShadowAuthManager(cache)
            mgr.get_access_token()
            self._sp = spotipy.Spotify(auth_manager=mgr)
            self._sp.current_user()
            return True
        except Exception as exc:
            print(f"[Spotify] init failed: {exc}")
            return False

    async def init_youtube(self) -> bool:
        return await asyncio.to_thread(self._sync_init_youtube)

    def _sync_init_youtube(self) -> bool:
        if not HAS_YTMUSIC or not os.path.exists("browser.json"):
            return False
        try:
            self._ytm = YTMusic("browser.json")
            self._ytm.get_library_playlists(limit=1)
            return True
        except Exception as exc:
            print(f"[YouTube Music] init failed: {exc}")
            return False

    async def init_apple(self) -> bool:
        return await asyncio.to_thread(self._sync_init_apple)

    def _sync_init_apple(self) -> bool:
        raw   = os.getenv("APPLE_AUTH_BEARER", "").strip()
        utok  = os.getenv("APPLE_MUSIC_USER_TOKEN", "").strip()
        if not raw or not utok:
            return False
        bearer = raw if raw.startswith("Bearer ") else f"Bearer {raw}"
        headers = {
            "Authorization":           bearer,
            "media-user-token":        utok,
            "x-apple-music-user-token": utok,
            "Origin":   "https://music.apple.com",
            "Referer":  "https://music.apple.com/",
            "Accept":   "application/json",
        }
        try:
            resp = requests.get(
                "https://amp-api.music.apple.com/v1/me/storefront",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                self._am_headers     = headers
                self._am_storefront  = resp.json().get("data", [{}])[0].get("id", "us")
                return True
            print(f"[Apple Music] login {resp.status_code}: {resp.text[:120]}")
            return False
        except Exception as exc:
            print(f"[Apple Music] init failed: {exc}")
            return False

    # ── Playlist Fetching ─────────────────────────────────────────────

    async def fetch_playlist(
        self,
        platform: str,
        playlist_id: str,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[str, list[Track]]:
        """
        Returns (playlist_name, tracks).
        `progress_cb(fetched, total, playlist_name)` is called as tracks arrive.
        """
        self._cb[platform].check_or_raise()

        if platform == "Spotify":
            return await asyncio.to_thread(
                self._sync_fetch_spotify, playlist_id, progress_cb
            )
        elif platform == "YouTube Music":
            return await asyncio.to_thread(
                self._sync_fetch_youtube, playlist_id, progress_cb
            )
        elif platform == "Apple Music":
            return await asyncio.to_thread(
                self._sync_fetch_apple, playlist_id, progress_cb
            )
        else:
            raise ValueError(f"Unknown platform: {platform}")

    def _sync_fetch_spotify(self, pid: str, cb) -> tuple[str, list[Track]]:
        if not self._sp:
            self._sync_init_spotify()
        sp = self._sp
        info   = sp.playlist(pid, fields="name")
        name   = info.get("name", "Spotify Playlist")
        result = sp.playlist_tracks(pid)
        raw    = result["items"]
        while result["next"]:
            result = sp.next(result)
            raw.extend(result["items"])
        tracks = []
        total  = len(raw)
        for i, item in enumerate(raw, 1):
            t = item.get("track")
            if not t or not t.get("id"):
                continue
            ms  = t["duration_ms"]
            dur = f"{int(ms/60000)}:{int((ms/1000)%60):02d}"
            imgs = t.get("album", {}).get("images", [])
            tracks.append(Track(
                id=t["id"], name=t["name"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                album=t.get("album", {}).get("name", ""),
                duration=dur,
                img_url=imgs[-1]["url"] if imgs else "",
                platform="Spotify",
            ))
            if cb and i % 50 == 0:
                cb(i, total, name)
        return name, tracks

    def _sync_fetch_youtube(self, pid: str, cb) -> tuple[str, list[Track]]:
        if not self._ytm:
            self._sync_init_youtube()
        pl    = self._ytm.get_playlist(pid, limit=None)
        name  = pl.get("title", "YouTube Playlist")
        raw   = pl.get("tracks", [])
        total = len(raw)
        tracks = []
        for i, t in enumerate(raw, 1):
            thumbs = t.get("thumbnails", [])
            tracks.append(Track(
                id=t["videoId"], name=t["title"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                album=(t.get("album") or {}).get("name", "Single"),
                duration=t.get("duration", "0:00"),
                img_url=thumbs[-1]["url"] if thumbs else "",
                platform="YouTube Music",
            ))
            if cb and i % 50 == 0:
                cb(i, total, name)
        return name, tracks

    def _sync_fetch_apple(self, pid: str, cb) -> tuple[str, list[Track]]:
        base = "https://amp-api.music.apple.com/v1"
        is_lib = pid.startswith("p.")
        info_url = (
            f"{base}/me/library/playlists/{pid}"
            if is_lib else
            f"{base}/catalog/{self._am_storefront}/playlists/{pid}"
        )
        name = "Apple Music Playlist"
        try:
            r = requests.get(info_url, headers=self._am_headers, timeout=10)
            if r.status_code == 429:
                ra = int(r.headers.get("Retry-After", 60))
                raise RateLimitError("Apple Music", ra)
            if r.ok:
                name = r.json()["data"][0]["attributes"].get("name", name)
        except RateLimitError:
            raise
        except Exception:
            pass

        tracks, url = [], f"{info_url}/tracks"
        while url:
            full = url if url.startswith("http") else f"https://amp-api.music.apple.com{url}"
            r    = requests.get(full, headers=self._am_headers, timeout=10)
            if r.status_code == 429:
                ra = int(r.headers.get("Retry-After", 60))
                raise RateLimitError("Apple Music", ra)
            r.raise_for_status()
            data = r.json()
            for item in data.get("data", []):
                attrs  = item.get("attributes", {})
                ms     = attrs.get("durationInMillis", 0)
                arturl = attrs.get("artwork", {}).get("url", "")
                if arturl:
                    arturl = arturl.replace("{w}", "60").replace("{h}", "60")
                tracks.append(Track(
                    id=item["id"], name=attrs.get("name", "Unknown"),
                    artist=attrs.get("artistName", "Unknown"),
                    album=attrs.get("albumName", "Unknown"),
                    duration=f"{int(ms/60000)}:{int((ms/1000)%60):02d}",
                    img_url=arturl, platform="Apple Music",
                ))
            url = data.get("next")
            if cb:
                cb(len(tracks), 0, name)
        return name, tracks

    # ── Fuzzy Search (for transfer) ────────────────────────────────────

    async def search_track(self, platform: str, name: str, artist: str) -> Optional[str]:
        """Returns platform-specific track ID, or None if no match ≥ 85%."""
        self._cb[platform].check_or_raise()
        source = f"{name} - {artist}".lower()

        if platform == "YouTube Music":
            return await asyncio.to_thread(self._yt_search, source, name, artist)
        elif platform == "Apple Music":
            return await asyncio.to_thread(self._am_search, source, name, artist)
        elif platform == "Spotify":
            return await asyncio.to_thread(self._sp_search, source, name, artist)
        return None

    def _fuzzy_best(self, source: str, candidates: list[tuple[str, str]]) -> Optional[str]:
        """candidates: list of (candidate_string, track_id). Returns best id or None."""
        if not HAS_RAPIDFUZZ:
            return candidates[0][1] if candidates else None
        best_id, best_score = None, 0
        for cand_str, tid in candidates:
            score = _fuzz.token_sort_ratio(source, cand_str.lower())
            if score > best_score:
                best_score, best_id = score, tid
        return best_id if best_score >= self.FUZZY_THRESHOLD else None

    def _yt_search(self, source: str, name: str, artist: str) -> Optional[str]:
        results = self._ytm.search(f"{name} {artist}", filter="songs", limit=5)
        candidates = [
            (
                f"{r.get('title','')} - {', '.join(a['name'] for a in r.get('artists',[]))}",
                r.get("videoId", ""),
            )
            for r in results
        ]
        return self._fuzzy_best(source, candidates)

    def _am_search(self, source: str, name: str, artist: str) -> Optional[str]:
        term = f"{name} {artist}".replace(" ", "+")
        url  = (f"https://amp-api.music.apple.com/v1/catalog/{self._am_storefront}"
                f"/search?types=songs&term={term}&limit=5")
        r    = requests.get(url, headers=self._am_headers, timeout=10)
        if r.status_code == 429:
            raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
        songs = r.json().get("results", {}).get("songs", {}).get("data", [])
        candidates = [
            (f"{s['attributes'].get('name','')} - {s['attributes'].get('artistName','')}",
             s["id"])
            for s in songs
        ]
        return self._fuzzy_best(source, candidates)

    def _sp_search(self, source: str, name: str, artist: str) -> Optional[str]:
        results = self._sp.search(q=f"track:{name} artist:{artist}", type="track", limit=5)
        items   = results.get("tracks", {}).get("items", [])
        candidates = [
            (f"{t.get('name','')} - {', '.join(a['name'] for a in t.get('artists',[]))}",
             t["id"])
            for t in items
        ]
        return self._fuzzy_best(source, candidates)

    # ── Playlist Creation ─────────────────────────────────────────────

    async def create_playlist(
        self, platform: str, title: str, track_ids: list[str]
    ) -> tuple[bool, str]:
        self._cb[platform].check_or_raise()
        if platform == "YouTube Music":
            return await asyncio.to_thread(self._yt_create, title, track_ids)
        elif platform == "Apple Music":
            return await asyncio.to_thread(self._am_create, title, track_ids)
        elif platform == "Spotify":
            return await asyncio.to_thread(self._sp_create, title, track_ids)
        return False, "Platform not supported"

    def _yt_create(self, title: str, ids: list[str]) -> tuple[bool, str]:
        pl_id = self._ytm.create_playlist(
            title, "Transferida por Playlist Manager", video_ids=ids
        )
        return True, pl_id

    def _am_create(self, title: str, ids: list[str]) -> tuple[bool, str]:
        payload = {
            "attributes": {"name": title, "description": "Transferida por Playlist Manager"},
            "relationships": {"tracks": {"data": [{"id": i, "type": "songs"} for i in ids]}},
        }
        r = requests.post(
            "https://amp-api.music.apple.com/v1/me/library/playlists",
            headers=self._am_headers, json=payload, timeout=15,
        )
        if r.status_code == 429:
            raise RateLimitError("Apple Music", int(r.headers.get("Retry-After", 60)))
        r.raise_for_status()
        return True, "Playlist creada"

    def _sp_create(self, title: str, ids: list[str]) -> tuple[bool, str]:
        me = self._sp.current_user()["id"]
        pl = self._sp.user_playlist_create(me, title, description="Transferida por Playlist Manager")
        self._sp.playlist_add_items(pl["id"], ids)
        return True, pl["id"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §7  APP STATE  (single source of truth, observable)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AppState:
    """
    BLoC-style ViewModel.  The UI registers listeners; state mutates only here.

    Principles
    ──────────
    • UI never calls APIs directly — it dispatches actions to AppState.
    • AppState calls MusicApiService, then mutates itself and notifies listeners.
    • All mutations happen on the asyncio event loop (no raw threads touching state).
    """

    PLATFORMS = ["Apple Music", "Spotify", "YouTube Music"]

    def __init__(self, service: MusicApiService):
        self.service = service

        # ── Platform state ────────────────────────────────────────────
        self.source: str      = "Apple Music"
        self.destination: str = "YouTube Music"

        # ── Playlist state ────────────────────────────────────────────
        self.playlist_id: str        = ""
        self.playlist_name: str      = "Cargar una playlist"
        self.tracks: list[Track]     = []
        self.filtered: list[Track]   = []   # after search filter
        self.load_state: LoadState   = LoadState.IDLE
        self.load_error: str         = ""

        # ── Transfer state ────────────────────────────────────────────
        self.transfer_state: TransferState = TransferState.IDLE
        self.transfer_progress: int        = 0
        self.transfer_total: int           = 0
        self.log_lines: list[str]          = []

        # ── Search ────────────────────────────────────────────────────
        self.search_query: str = ""

        # ── Circuit Breakers (one per platform) ───────────────────────
        self.cb: dict[str, CircuitBreaker] = {
            p: CircuitBreaker(p) for p in self.PLATFORMS
        }
        # Wire service to the same breakers
        self.service._cb = self.cb

        # ── Observer list ─────────────────────────────────────────────
        self._listeners: list[Callable[[], None]] = []

    # ── Observer API ─────────────────────────────────────────────────

    def subscribe(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                pass

    # ── Computed properties ───────────────────────────────────────────

    @property
    def selected_count(self) -> int:
        return sum(1 for t in self.tracks if t.selected)

    @property
    def select_all(self) -> bool:
        return all(t.selected for t in self.tracks) if self.tracks else False

    @property
    def display_tracks(self) -> list[Track]:
        return self.filtered if self.search_query else self.tracks

    # ── Actions (called by UI, do actual work) ────────────────────────

    async def load_playlist(self, playlist_id: str) -> None:
        if not playlist_id.strip():
            return
        self.playlist_id  = playlist_id.strip()
        self.tracks       = []
        self.filtered     = []
        self.search_query = ""
        self.load_state   = LoadState.LOADING_META
        self.load_error   = ""
        self.playlist_name = "Cargando metadatos…"
        self.notify()

        def _progress(fetched: int, total: int, name: str) -> None:
            self.playlist_name = name
            if total:
                self.load_state = LoadState.LOADING_TRACKS
            self.notify()

        try:
            # Use mock data if env flag set, or no credentials available
            use_mock = os.getenv("PM_USE_MOCK", "1") == "1"
            if use_mock:
                # Simulate network latency for realistic demo
                await asyncio.sleep(0.6)
                self.playlist_name = "M.M."
                self.load_state    = LoadState.LOADING_TRACKS
                self.notify()
                await asyncio.sleep(0.5)
                self.tracks     = [Track(**vars(t)) for t in MOCK_TRACKS]
                self.load_state = LoadState.READY
            else:
                name, tracks = await self.service.fetch_playlist(
                    self.source, self.playlist_id, _progress
                )
                self.playlist_name = name
                self.tracks        = tracks
                self.load_state    = LoadState.READY
        except RateLimitError as e:
            self.cb[self.source].trip(e.retry_after)
            self.load_state = LoadState.ERROR
            self.load_error = f"Rate limit en {e.platform}. Espera {e.retry_after}s."
        except Exception as e:
            self.load_state = LoadState.ERROR
            self.load_error = str(e)
        finally:
            self.notify()

    async def transfer_playlist(self) -> None:
        selected = [t for t in self.tracks if t.selected]
        if not selected:
            return
        self.transfer_state    = TransferState.RUNNING
        self.transfer_progress = 0
        self.transfer_total    = len(selected)
        self._log(f"🔍 Buscando {len(selected)} canciones en {self.destination}…")
        self.notify()

        dest_ids: list[str] = []
        try:
            # Ensure destination client is authenticated
            init_ok = await self._ensure_auth(self.destination)
            if not init_ok:
                raise RuntimeError(f"No se pudo autenticar en {self.destination}")

            for i, track in enumerate(selected, 1):
                track.transfer_status = "searching"
                self.notify()
                try:
                    match_id = await self.service.search_track(
                        self.destination, track.name, track.artist
                    )
                    if match_id:
                        dest_ids.append(match_id)
                        track.transfer_status = "found"
                    else:
                        track.transfer_status = "not_found"
                except RateLimitError as e:
                    self.cb[self.destination].trip(e.retry_after)
                    track.transfer_status = "error"
                    self.load_error = e.args[0]
                    break
                self.transfer_progress = i
                if i % 10 == 0:
                    self._log(f"[{i}/{self.transfer_total}] {track.name[:40]}…")
                self.notify()

            if dest_ids:
                self._log(f"📁 Creando playlist con {len(dest_ids)} canciones…")
                self.notify()
                ok, msg = await self.service.create_playlist(
                    self.destination, f"[PM] {self.playlist_name}", dest_ids
                )
                if ok:
                    self._log(f"✓ ¡Éxito! {len(dest_ids)}/{len(selected)} canciones transferidas.")
                    self.transfer_state = TransferState.DONE
                else:
                    raise RuntimeError(msg)
            else:
                raise RuntimeError("No se encontraron coincidencias en el destino.")

        except RateLimitError as e:
            self.cb[e.platform].trip(e.retry_after)
            self._log(f"⚠ Rate limit: espera {e.retry_after}s")
            self.transfer_state = TransferState.ERROR
        except Exception as e:
            self._log(f"✗ Error: {e}")
            self.transfer_state = TransferState.ERROR
        finally:
            self.notify()

    async def _ensure_auth(self, platform: str) -> bool:
        if platform == "Spotify":
            return await self.service.init_spotify()
        elif platform == "YouTube Music":
            return await self.service.init_youtube()
        elif platform == "Apple Music":
            return await self.service.init_apple()
        return False

    def toggle_select_all(self) -> None:
        new_val = not self.select_all
        for t in self.tracks:
            t.selected = new_val
        self.notify()

    def toggle_track(self, track_id: str) -> None:
        for t in self.tracks:
            if t.id == track_id:
                t.selected = not t.selected
                break
        self.notify()

    def apply_search(self, query: str) -> None:
        self.search_query = query
        if not query:
            self.filtered = []
        else:
            q = query.lower()
            self.filtered = [
                t for t in self.tracks
                if q in t.name.lower() or q in t.artist.lower() or q in t.album.lower()
            ]
        self.notify()

    def set_source(self, val: str) -> None:
        self.source = val
        self.notify()

    def set_destination(self, val: str) -> None:
        self.destination = val
        self.notify()

    def _log(self, msg: str) -> None:
        self.log_lines.append(msg)
        if len(self.log_lines) > 200:
            self.log_lines = self.log_lines[-200:]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §8  UI COMPONENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Design tokens ─────────────────────────────────────────────────────
BG_DEEP      = "#080B14"   # page background
BG_PANEL     = "#0D1117"   # content panel
BG_SURFACE   = "#111827"   # table rows base
BG_HOVER     = "#1C2434"   # row hover
GLASS_BG     = "#FFFFFF14" # sidebar glass fill  (~8% white)
GLASS_BORDER = "#FFFFFF22" # sidebar glass border (~13% white)
ACCENT       = "#4F8BFF"   # primary blue
ACCENT_DIM   = "#2D5FCC"   # pressed state
SUCCESS      = "#00D084"   # transfer success
WARNING      = "#FFB547"   # warning / rate-limit
ERROR_COL    = "#FF5C5C"   # error
TEXT_PRIMARY = "#F0F4FF"
TEXT_MUTED   = "#6B7280"
TEXT_DIM     = "#374151"

SKELETON_DARK  = "#1A2035"
SKELETON_LIGHT = "#243050"

ITEM_H = 64    # px — fixed row height (enables ListView virtualization)


class SkeletonRow(ft.Container):
    """
    Animated shimmer row shown while tracks load.
    Uses opacity pulse to simulate the shimmer sweep.
    """

    def __init__(self, index: int):
        self._pulse_task: Optional[asyncio.Task] = None

        # Shimmer blocks
        self._num    = ft.Container(width=28, height=12, border_radius=3, bgcolor=SKELETON_DARK)
        self._thumb  = ft.Container(width=40, height=40, border_radius=6, bgcolor=SKELETON_DARK)
        self._title  = ft.Container(width=180, height=12, border_radius=3, bgcolor=SKELETON_DARK)
        self._artist = ft.Container(width=110, height=12, border_radius=3, bgcolor=SKELETON_DARK)
        self._dur    = ft.Container(width=36,  height=12, border_radius=3, bgcolor=SKELETON_DARK)
        self._chk    = ft.Container(width=18,  height=18, border_radius=4, bgcolor=SKELETON_DARK)

        super().__init__(
            height=ITEM_H,
            padding=ft.padding.symmetric(horizontal=20, vertical=12),
            border=ft.border.only(bottom=ft.BorderSide(1, "#0F172A")),
            content=ft.Row(
                controls=[self._num, self._thumb, self._title,
                           ft.Container(expand=True),
                           self._artist, self._dur, self._chk],
                spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            animate_opacity=ft.animation.Animation(400, ft.AnimationCurve.EASE_IN_OUT),
            opacity=1.0 - (index % 3) * 0.15,  # staggered initial opacity
        )

    async def start_pulse(self) -> None:
        """Background task: pulse opacity to create shimmer feel."""
        self._pulse_task = asyncio.current_task()
        state = True
        while True:
            try:
                self.opacity = 1.0 if state else 0.35
                if self.page:
                    self.update()
                state = not state
                await asyncio.sleep(0.75)
            except asyncio.CancelledError:
                break

    def stop_pulse(self) -> None:
        if self._pulse_task:
            self._pulse_task.cancel()


def _status_icon(status: str) -> ft.Control:
    icons = {
        "found":       (ft.icons.CHECK_CIRCLE_ROUNDED,    SUCCESS),
        "not_found":   (ft.icons.CANCEL_ROUNDED,           ERROR_COL),
        "searching":   (ft.icons.LOOP_ROUNDED,             ACCENT),
        "transferred": (ft.icons.CLOUD_DONE_ROUNDED,       SUCCESS),
        "error":       (ft.icons.ERROR_OUTLINE_ROUNDED,    ERROR_COL),
        "pending":     (ft.icons.RADIO_BUTTON_UNCHECKED,   TEXT_DIM),
    }
    ico, col = icons.get(status, (ft.icons.RADIO_BUTTON_UNCHECKED, TEXT_DIM))
    return ft.Icon(ico, color=col, size=16)


class SongRow(ft.Container):
    """
    A single song row in the virtual list.

    Performance notes
    ──────────────────
    • Height is FIXED at ITEM_H — mandatory for ListView virtualization.
    • Image download is deferred (placeholder shown until image arrives).
    • Hover triggers a bgcolor tween via animate_bgcolor.
    """

    def __init__(
        self,
        track: Track,
        index: int,
        on_toggle: Callable[[str], None],
    ):
        self._track     = track
        self._on_toggle = on_toggle

        # ── Thumbnail placeholder ──────────────────────────────────────
        self._thumb = ft.Container(
            width=40, height=40,
            border_radius=6,
            bgcolor=SKELETON_DARK,
            content=ft.Icon(ft.icons.MUSIC_NOTE_ROUNDED, color=TEXT_DIM, size=18),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

        # ── Checkbox ──────────────────────────────────────────────────
        self._chk = ft.Checkbox(
            value=track.selected,
            fill_color={
                ft.MaterialState.SELECTED: ACCENT,
                ft.MaterialState.DEFAULT:  "#FFFFFF00",
            },
            check_color=TEXT_PRIMARY,
            border_side=ft.BorderSide(1.5, TEXT_DIM),
            on_change=lambda e: on_toggle(track.id),
        )

        # ── Status icon ───────────────────────────────────────────────
        self._status_icon = _status_icon(track.transfer_status)

        # ── Number ────────────────────────────────────────────────────
        num_label = ft.Text(
            str(index), size=11, color=TEXT_MUTED,
            font_family="IBM Plex Sans",
            weight=ft.FontWeight.W_500,
        )

        # ── Title & Artist ────────────────────────────────────────────
        title_text = ft.Text(
            track.name, size=13, color=TEXT_PRIMARY,
            font_family="IBM Plex Sans",
            weight=ft.FontWeight.W_600,
            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True,
        )
        artist_text = ft.Text(
            track.artist, size=12, color=TEXT_MUTED,
            font_family="IBM Plex Sans",
            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
        )
        dur_text = ft.Text(
            track.duration, size=11, color=TEXT_DIM,
            font_family="IBM Plex Sans",
            weight=ft.FontWeight.W_500,
        )

        row_content = ft.Row(
            controls=[
                ft.Container(content=num_label,  width=32),
                self._thumb,
                ft.Column(
                    controls=[title_text, artist_text],
                    spacing=2, expand=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Container(content=dur_text, width=48,
                             alignment=ft.alignment.center_right),
                ft.Container(content=self._status_icon, width=26,
                             alignment=ft.alignment.center),
                ft.Container(content=self._chk, width=32,
                             alignment=ft.alignment.center),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        super().__init__(
            height=ITEM_H,
            padding=ft.padding.symmetric(horizontal=16, vertical=0),
            border=ft.border.only(bottom=ft.BorderSide(1, "#0F172A")),
            border_radius=0,
            bgcolor=ft.colors.TRANSPARENT,
            animate=ft.animation.Animation(120, ft.AnimationCurve.EASE_OUT),
            on_hover=self._on_hover,
            content=row_content,
        )

    def _on_hover(self, e: ft.HoverEvent) -> None:
        self.bgcolor = BG_HOVER if e.data == "true" else ft.colors.TRANSPARENT
        self.update()

    def refresh(self, track: Track) -> None:
        """Update the row's visual state without rebuilding the widget."""
        self._track = track
        self._chk.value = track.selected
        # Replace status icon — find it in the row
        status_container = self.content.controls[4]
        status_container.content = _status_icon(track.transfer_status)
        self.update()


def _glass_container(content: ft.Control, **kwargs) -> ft.Container:
    """Returns a glassmorphism-styled container."""
    return ft.Container(
        content=content,
        bgcolor=GLASS_BG,
        border=ft.border.all(1, GLASS_BORDER),
        border_radius=kwargs.pop("border_radius", 14),
        blur=ft.Blur(sigma_x=20, sigma_y=20, tile_mode=ft.BlurTileMode.MIRROR),
        **kwargs,
    )


def _section_label(text: str) -> ft.Text:
    return ft.Text(
        text, size=10, color=TEXT_DIM,
        font_family="IBM Plex Sans",
        weight=ft.FontWeight.W_700,
        letter_spacing=1.2,
    )


def _primary_btn(text: str, icon: str, on_click, width=None) -> ft.ElevatedButton:
    return ft.ElevatedButton(
        text=text, icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor={ft.MaterialState.DEFAULT: ACCENT,
                     ft.MaterialState.HOVERED: "#6B9FFF",
                     ft.MaterialState.PRESSED: ACCENT_DIM},
            color=TEXT_PRIMARY,
            elevation={"default": 0, "hovered": 4},
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.padding.symmetric(horizontal=16, vertical=12),
            animation_duration=120,
        ),
        width=width,
    )


def _ghost_btn(text: str, icon: str, on_click, width=None) -> ft.OutlinedButton:
    return ft.OutlinedButton(
        text=text, icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            color={ft.MaterialState.DEFAULT: TEXT_MUTED,
                   ft.MaterialState.HOVERED: TEXT_PRIMARY},
            side={ft.MaterialState.DEFAULT: ft.BorderSide(1, "#2D3748"),
                  ft.MaterialState.HOVERED: ft.BorderSide(1, ACCENT)},
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.padding.symmetric(horizontal=14, vertical=12),
            animation_duration=120,
        ),
        width=width,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §9  PLAYLIST MANAGER UI  ("dumb" view layer)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PlaylistManagerUI:
    """
    Pure UI class.  Knows nothing about APIs.  Reacts to AppState changes.

    Layout
    ──────
        ┌──────────────────┬──────────────────────────────────────────┐
        │  SIDEBAR (glass) │  CONTENT AREA                            │
        │  ─────────────── │  ┌──────────────────────────────────┐   │
        │  Logo            │  │ Header bar (title + search + sel) │   │
        │  Source/Dest     │  ├──────────────────────────────────┤   │
        │  Playlist ID     │  │ Column headers                    │   │
        │  Actions         │  ├──────────────────────────────────┤   │
        │  Rate-limit bar  │  │ Virtual ListView (skeletons/rows) │   │
        │  Log console     │  └──────────────────────────────────┘   │
        └──────────────────┴──────────────────────────────────────────┘
    """

    SKELETON_COUNT = 14

    def __init__(self, page: ft.Page, state: AppState):
        self.page  = page
        self.state = state

        # Debounce task for search
        self._search_task: Optional[asyncio.Task] = None

        # Skeleton pulse tasks
        self._skeleton_tasks: list[asyncio.Task] = []

        # Row widget cache: track_id → SongRow (avoids recreating on re-render)
        self._row_cache: dict[str, SongRow] = {}

        # ── Build all UI pieces ────────────────────────────────────────
        self._build_sidebar()
        self._build_content()

        # ── Root layout ───────────────────────────────────────────────
        self.root = ft.Row(
            controls=[self._sidebar, self._content],
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        # ── Subscribe to state changes ─────────────────────────────────
        state.subscribe(self._on_state_changed)

        # ── Subscribe circuit breakers ────────────────────────────────
        for platform, cb in state.cb.items():
            cb.subscribe(lambda is_open, rem, p=platform: self._on_circuit_change(p, is_open, rem))

    # ══════════════════════════════════════════════════════════════════
    # BUILD — SIDEBAR
    # ══════════════════════════════════════════════════════════════════

    def _build_sidebar(self) -> None:
        s = self.state

        # ── Logo ──────────────────────────────────────────────────────
        logo = ft.Column([
            ft.Row([
                ft.Icon(ft.icons.QUEUE_MUSIC_ROUNDED, color=ACCENT, size=26),
                ft.Text("Playlist Manager", size=20, weight=ft.FontWeight.W_700,
                        color=TEXT_PRIMARY, font_family="IBM Plex Sans"),
            ], spacing=10),
            ft.Text("Transfiere y gestiona tus playlists", size=11,
                    color=TEXT_MUTED, font_family="IBM Plex Sans"),
        ], spacing=4)

        # ── Platform selectors ────────────────────────────────────────
        self._src_dd = ft.Dropdown(
            label="Origen",
            value=s.source,
            options=[ft.dropdown.Option(p) for p in AppState.PLATFORMS],
            bgcolor="#0D1117", border_color="#2D3748",
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11,
                                     font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=13,
                                    font_family="IBM Plex Sans"),
            border_radius=10, expand=True,
            on_change=lambda e: s.set_source(e.control.value),
        )
        self._dst_dd = ft.Dropdown(
            label="Destino",
            value=s.destination,
            options=[ft.dropdown.Option(p) for p in AppState.PLATFORMS],
            bgcolor="#0D1117", border_color="#2D3748",
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11,
                                     font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=13,
                                    font_family="IBM Plex Sans"),
            border_radius=10, expand=True,
            on_change=lambda e: s.set_destination(e.control.value),
        )
        self._status_badge = ft.Text("", size=11, color=SUCCESS,
                                     font_family="IBM Plex Sans")

        platform_section = ft.Column([
            _section_label("PLATAFORMAS"),
            ft.Row([self._src_dd, self._dst_dd], spacing=10),
            self._status_badge,
        ], spacing=10)

        # ── Playlist ID input ─────────────────────────────────────────
        self._id_field = ft.TextField(
            label="ID de la Playlist",
            hint_text="pl.u-xxxx  /  PLxxxx  /  37i9dQ…",
            bgcolor="#0D1117", border_color="#2D3748",
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11,
                                     font_family="IBM Plex Sans"),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=13,
                                    font_family="IBM Plex Sans"),
            hint_style=ft.TextStyle(color=TEXT_DIM, size=12),
            border_radius=10, focused_border_color=ACCENT,
            on_submit=self._on_load,
        )

        # ── Action buttons ────────────────────────────────────────────
        self._load_btn = _primary_btn(
            "Cargar Playlist", ft.icons.DOWNLOAD_ROUNDED,
            self._on_load, width=None,
        )
        self._transfer_btn = _ghost_btn(
            "Transferir", ft.icons.SWAP_HORIZ_ROUNDED,
            self._on_transfer,
        )
        self._sync_btn   = _ghost_btn("Sincronizar", ft.icons.SYNC_ROUNDED,
                                      lambda _: self._snack("Función próximamente"))
        self._split_btn  = _ghost_btn("Dividir",     ft.icons.CALL_SPLIT_ROUNDED,
                                      lambda _: self._snack("Función próximamente"))
        self._delete_btn = _ghost_btn("Eliminar",    ft.icons.DELETE_OUTLINE_ROUNDED,
                                      lambda _: self._snack("Función próximamente"))

        actions = ft.Column([
            self._load_btn,
            ft.Row([self._transfer_btn, self._sync_btn], spacing=8),
            ft.Row([self._split_btn, self._delete_btn], spacing=8),
        ], spacing=8)

        # ── Rate-limit banner ─────────────────────────────────────────
        self._rl_banner = ft.Container(
            content=ft.Row([
                ft.Icon(ft.icons.TIMER_OUTLINED, color=WARNING, size=16),
                ft.Text("", size=11, color=WARNING, font_family="IBM Plex Sans"),
            ], spacing=8),
            bgcolor="#1A1200",
            border=ft.border.all(1, "#3D2A00"),
            border_radius=8,
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            visible=False,
        )

        # ── Transfer progress bar ─────────────────────────────────────
        self._progress_bar = ft.ProgressBar(
            value=0, bgcolor="#1A2035", color=ACCENT,
            border_radius=4,
        )
        self._progress_row = ft.Container(
            content=ft.Column([
                self._progress_bar,
                ft.Row([
                    ft.Text("", size=10, color=TEXT_MUTED,
                            font_family="IBM Plex Sans"),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ], spacing=4),
            visible=False,
        )

        # ── Log console ───────────────────────────────────────────────
        self._log_list = ft.ListView(spacing=0, expand=True)
        self._log_box  = ft.Container(
            content=self._log_list,
            bgcolor="#080B14",
            border=ft.border.all(1, "#1A2035"),
            border_radius=8,
            padding=8,
            height=120,
            visible=False,
        )

        # ── Assemble sidebar ──────────────────────────────────────────
        inner = ft.Column(
            controls=[
                logo,
                ft.Divider(height=1, color="#1A2035"),
                platform_section,
                ft.Divider(height=1, color="#1A2035"),
                _section_label("PLAYLIST"),
                self._id_field,
                ft.Divider(height=1, color="#1A2035"),
                _section_label("ACCIONES"),
                actions,
                self._rl_banner,
                self._progress_row,
                ft.Divider(height=1, color="#1A2035"),
                _section_label("CONSOLA"),
                self._log_box,
            ],
            spacing=14,
            scroll=ft.ScrollMode.AUTO,
        )

        self._sidebar = ft.Container(
            width=320,
            padding=ft.padding.all(20),
            bgcolor=GLASS_BG,
            border=ft.border.only(right=ft.BorderSide(1, GLASS_BORDER)),
            blur=ft.Blur(sigma_x=24, sigma_y=24, tile_mode=ft.BlurTileMode.MIRROR),
            content=inner,
        )

    # ══════════════════════════════════════════════════════════════════
    # BUILD — CONTENT AREA
    # ══════════════════════════════════════════════════════════════════

    def _build_content(self) -> None:
        # ── Header bar ────────────────────────────────────────────────
        self._playlist_title = ft.Text(
            "Cargar una playlist", size=22, weight=ft.FontWeight.W_700,
            color=TEXT_PRIMARY, font_family="IBM Plex Sans",
        )
        self._track_count = ft.Text(
            "", size=13, color=TEXT_MUTED, font_family="IBM Plex Sans",
        )

        self._search_field = ft.TextField(
            hint_text="Buscar título, artista…",
            prefix_icon=ft.icons.SEARCH_ROUNDED,
            bgcolor="#0D1117", border_color="#2D3748",
            hint_style=ft.TextStyle(color=TEXT_DIM, size=12),
            text_style=ft.TextStyle(color=TEXT_PRIMARY, size=13,
                                    font_family="IBM Plex Sans"),
            border_radius=10, focused_border_color=ACCENT,
            width=260, height=40,
            content_padding=ft.padding.symmetric(horizontal=12, vertical=8),
            on_change=self._on_search_change,
        )

        self._select_all_chk = ft.Checkbox(
            label="Seleccionar todo",
            label_style=ft.TextStyle(color=TEXT_MUTED, size=12,
                                     font_family="IBM Plex Sans"),
            fill_color={ft.MaterialState.SELECTED: ACCENT},
            check_color=TEXT_PRIMARY,
            border_side=ft.BorderSide(1.5, TEXT_DIM),
            on_change=lambda _: self.state.toggle_select_all(),
        )

        header_bar = ft.Row(
            controls=[
                ft.Column([self._playlist_title, self._track_count], spacing=2),
                ft.Container(expand=True),
                self._search_field,
                self._select_all_chk,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ── Column headers ────────────────────────────────────────────
        def _col_header(text: str, width=None, expand=False) -> ft.Container:
            ctrl = ft.Text(text, size=10, color=TEXT_DIM, weight=ft.FontWeight.W_700,
                           font_family="IBM Plex Sans", letter_spacing=0.8)
            return ft.Container(
                content=ctrl, width=width,
                expand=expand,
            )

        col_headers = ft.Container(
            content=ft.Row(
                controls=[
                    _col_header("#",       width=32),
                    _col_header("PORTADA", width=40),
                    _col_header("TÍTULO / ARTISTA", expand=True),
                    _col_header("",        width=14),  # spacer
                    _col_header("DUR.",    width=48),
                    _col_header("",        width=26),
                    _col_header("SEL.",    width=32),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=10),
            bgcolor=BG_PANEL,
            border=ft.border.only(bottom=ft.BorderSide(1, "#1A2035")),
        )

        # ── Virtual list (THE performance core) ──────────────────────
        # item_extent MUST be set for Flutter to skip invisible items.
        self._list_view = ft.ListView(
            item_extent=ITEM_H,
            spacing=0,
            expand=True,
            padding=ft.padding.only(bottom=20),
        )

        # ── Skeletons (shown during loading) ─────────────────────────
        self._skeletons = [SkeletonRow(i) for i in range(self.SKELETON_COUNT)]
        self._skeleton_view = ft.ListView(
            item_extent=ITEM_H,
            spacing=0,
            expand=True,
            controls=self._skeletons,
            visible=False,
        )

        # ── Empty state ───────────────────────────────────────────────
        self._empty_state = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Icon(ft.icons.LIBRARY_MUSIC_ROUNDED, size=64, color=TEXT_DIM),
                    ft.Text("Sin playlist cargada", size=16, color=TEXT_DIM,
                            font_family="IBM Plex Sans", weight=ft.FontWeight.W_600),
                    ft.Text("Introduce un ID y pulsa Enter", size=13, color=TEXT_DIM,
                            font_family="IBM Plex Sans"),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            expand=True,
            alignment=ft.alignment.center,
            visible=True,
        )

        # ── Error state ───────────────────────────────────────────────
        self._error_text = ft.Text("", size=13, color=ERROR_COL,
                                   font_family="IBM Plex Sans")
        self._error_state = ft.Container(
            content=ft.Column([
                ft.Icon(ft.icons.ERROR_OUTLINE_ROUNDED, size=48, color=ERROR_COL),
                self._error_text,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            expand=True, alignment=ft.alignment.center, visible=False,
        )

        # ── Stack: empty / skeleton / list / error ────────────────────
        list_area = ft.Stack(
            controls=[
                self._empty_state,
                self._skeleton_view,
                self._list_view,
                self._error_state,
            ],
            expand=True,
        )

        # ── Assemble content ──────────────────────────────────────────
        self._content = ft.Container(
            expand=True,
            bgcolor=BG_DEEP,
            padding=ft.padding.all(24),
            content=ft.Column(
                controls=[header_bar, col_headers, list_area],
                spacing=12,
                expand=True,
            ),
        )

    # ══════════════════════════════════════════════════════════════════
    # STATE REACTIONS
    # ══════════════════════════════════════════════════════════════════

    def _on_state_changed(self) -> None:
        """Called by AppState.notify(). Updates all UI to match current state."""
        s = self.state

        # ── Header ───────────────────────────────────────────────────
        self._playlist_title.value = s.playlist_name
        n = len(s.display_tracks)
        total = len(s.tracks)
        self._track_count.value = (
            f"{n} canciones" if not s.search_query
            else f"{n} de {total} coincidencias"
        )

        # ── Platform badge ────────────────────────────────────────────
        if s.source == s.destination:
            self._status_badge.value  = "⚠ Origen y destino iguales"
            self._status_badge.color  = WARNING
        else:
            self._status_badge.value  = f"✓ {s.source} → {s.destination}"
            self._status_badge.color  = SUCCESS

        # ── Select-all checkbox ───────────────────────────────────────
        self._select_all_chk.value = s.select_all

        # ── List visibility ───────────────────────────────────────────
        is_loading = s.load_state in (LoadState.LOADING_META, LoadState.LOADING_TRACKS)
        is_ready   = s.load_state == LoadState.READY
        is_error   = s.load_state == LoadState.ERROR
        is_idle    = s.load_state == LoadState.IDLE

        self._empty_state.visible    = is_idle
        self._skeleton_view.visible  = is_loading
        self._list_view.visible      = is_ready and not is_error
        self._error_state.visible    = is_error

        if is_error:
            self._error_text.value = s.load_error

        if is_loading:
            self._ensure_skeletons_pulsing()

        if is_ready:
            self._stop_skeleton_pulse()
            self._sync_list_view(s.display_tracks)

        # ── Transfer progress ─────────────────────────────────────────
        is_transferring = s.transfer_state == TransferState.RUNNING
        self._progress_row.visible = is_transferring or s.transfer_state == TransferState.DONE
        if s.transfer_total:
            frac = s.transfer_progress / s.transfer_total
            self._progress_bar.value = frac
            prog_label = self._progress_row.content.controls[1].controls[0]
            prog_label.value = f"{s.transfer_progress} / {s.transfer_total}"

        # ── Log console ───────────────────────────────────────────────
        self._log_box.visible = bool(s.log_lines)
        self._log_list.controls.clear()
        for line in s.log_lines[-50:]:
            self._log_list.controls.append(
                ft.Text(f"› {line}", size=10, color=TEXT_MUTED,
                        font_family="IBM Plex Sans")
            )

        # ── Button states ─────────────────────────────────────────────
        net_blocked = any(cb.is_open for cb in s.cb.values())
        self._load_btn.disabled     = net_blocked or is_loading
        self._transfer_btn.disabled = net_blocked or is_transferring or not is_ready

        self.page.update()

    def _sync_list_view(self, tracks: list[Track]) -> None:
        """
        Efficiently sync the ListView controls to the current track list.

        Strategy:
          • If track count is different, rebuild from scratch (rare).
          • If same count, just refresh individual rows (common: toggle, search update).
        """
        lv = self._list_view
        existing_ids = {c._track.id for c in lv.controls if hasattr(c, "_track")}
        incoming_ids  = {t.id for t in tracks}

        if existing_ids != incoming_ids:
            # Full rebuild — e.g. new playlist loaded or search changed
            lv.controls.clear()
            self._row_cache.clear()
            for i, track in enumerate(tracks, 1):
                row = SongRow(track, i, self.state.toggle_track)
                self._row_cache[track.id] = row
                lv.controls.append(row)
        else:
            # Incremental update — only refresh changed rows
            for row in lv.controls:
                if hasattr(row, "_track"):
                    current = next((t for t in tracks if t.id == row._track.id), None)
                    if current:
                        row.refresh(current)

    # ══════════════════════════════════════════════════════════════════
    # EVENT HANDLERS
    # ══════════════════════════════════════════════════════════════════

    async def _on_load(self, _) -> None:
        pid = self._id_field.value.strip()
        if not pid:
            self._snack("Introduce un ID de playlist")
            return
        await self.state.load_playlist(pid)

    async def _on_transfer(self, _) -> None:
        if self.state.source == self.state.destination:
            self._snack("Origen y destino no pueden ser iguales", error=True)
            return
        if self.state.selected_count == 0:
            self._snack("Selecciona al menos una canción", error=True)
            return
        await self.state.transfer_playlist()

    async def _on_search_change(self, e: ft.ControlEvent) -> None:
        """Debounced search — 300 ms after last keystroke."""
        if self._search_task and not self._search_task.done():
            self._search_task.cancel()
        query = e.control.value
        self._search_task = asyncio.create_task(self._do_search(query))

    async def _do_search(self, query: str) -> None:
        await asyncio.sleep(0.30)  # 300 ms debounce
        self.state.apply_search(query)

    # ══════════════════════════════════════════════════════════════════
    # SKELETON PULSE
    # ══════════════════════════════════════════════════════════════════

    def _ensure_skeletons_pulsing(self) -> None:
        if self._skeleton_tasks:
            return  # already pulsing
        for sk in self._skeletons:
            task = asyncio.create_task(sk.start_pulse())
            self._skeleton_tasks.append(task)

    def _stop_skeleton_pulse(self) -> None:
        for task in self._skeleton_tasks:
            task.cancel()
        self._skeleton_tasks.clear()
        for sk in self._skeletons:
            sk.stop_pulse()

    # ══════════════════════════════════════════════════════════════════
    # CIRCUIT BREAKER REACTIONS
    # ══════════════════════════════════════════════════════════════════

    def _on_circuit_change(self, platform: str, is_open: bool, remaining: int) -> None:
        """Live countdown banner when a platform is rate-limited."""
        banner_text = self._rl_banner.content.controls[1]
        self._rl_banner.visible = is_open
        if is_open:
            banner_text.value = f"Rate limit en {platform} · {remaining}s"
            asyncio.create_task(self._countdown(platform, remaining))
        else:
            banner_text.value = ""
        self._rl_banner.update()

    async def _countdown(self, platform: str, seconds: int) -> None:
        banner_text = self._rl_banner.content.controls[1]
        for rem in range(seconds, 0, -1):
            banner_text.value = f"Rate limit en {platform} · {rem}s"
            try:
                banner_text.update()
            except Exception:
                break
            await asyncio.sleep(1)

    # ══════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════

    def _snack(self, msg: str, error: bool = False) -> None:
        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color=ft.colors.WHITE,
                            font_family="IBM Plex Sans", size=13),
            bgcolor=ERROR_COL if error else "#1E3A5F",
            duration=3000,
            behavior=ft.SnackBarBehavior.FLOATING,
            width=400,
            action="OK",
            action_color=ACCENT,
        )
        self.page.snack_bar.open = True
        self.page.update()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §10  ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main(page: ft.Page) -> None:
    # ── Page configuration ────────────────────────────────────────────
    page.title            = "Playlist Manager"
    page.bgcolor          = BG_DEEP
    page.window.width     = 1200
    page.window.height    = 720
    page.window.min_width  = 960
    page.window.min_height = 600
    page.padding          = 0
    page.spacing          = 0
    page.theme_mode       = ft.ThemeMode.DARK

    # ── IBM Plex Sans (anti-tofu: covers Latin + Japanese) ────────────
    page.fonts = {
        "IBM Plex Sans": (
            "https://fonts.gstatic.com/s/ibmplexsans/v19/"
            "zYXgKVElMYYaJe8bpLHnCwDKjR7_MIZs.woff2"
        ),
        "IBM Plex Sans JP": (
            "https://fonts.gstatic.com/s/ibmplexsansjp/v5/"
            "Z9XLDn9KbTDf6_f7dISNqYf_-aYNey-sDg.woff2"
        ),
    }
    page.theme = ft.Theme(
        font_family="IBM Plex Sans",
        color_scheme=ft.ColorScheme(
            primary=ACCENT,
            surface=BG_DEEP,        # Usamos tu variable de fondo profundo aquí
            on_primary=TEXT_PRIMARY,
            on_surface=TEXT_PRIMARY,
            # 'background' y 'on_background' se eliminan en versiones nuevas
        ),
    )

    # ── Wire up architecture ──────────────────────────────────────────
    circuit_breakers = {p: CircuitBreaker(p) for p in AppState.PLATFORMS}
    service  = MusicApiService(circuit_breakers)
    state    = AppState(service)
    ui       = PlaylistManagerUI(page, state)

    page.add(ui.root)

    # ── Initial platform auth (background, non-blocking) ─────────────
    # Show live status in snack while auths fire
    async def _init_auth_background() -> None:
        results = await asyncio.gather(
            service.init_spotify(),
            service.init_youtube(),
            service.init_apple(),
            return_exceptions=True,
        )
        platforms = AppState.PLATFORMS
        ok = [r is True for r in results]
        msg = " · ".join(
            f"{'✓' if o else '–'} {p}" for p, o in zip(platforms, ok)
        )
        # Brief status update — don't block UI
        state._log(f"Auth: {msg}")
        state.notify()

    asyncio.create_task(_init_auth_background())


if __name__ == "__main__":
    ft.app(target=main)
