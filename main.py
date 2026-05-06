import os
import json
import time
import threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import BUILTIN_THEMES
from textual.widgets import Header, Footer, Static, Input, Button, DataTable, ProgressBar, Select

THEMES = list(BUILTIN_THEMES.keys())


MIRRORS = [
    "https://api.nerinyan.moe/d/{id}",
    "https://catboy.best/d/{id}"
]

BACKUP_DB_MIRRORS = [
    "https://chimu.moe/d/{id}",
]

OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_SEARCH_URL = "https://osu.ppy.sh/api/v2/beatmapsets/search"
OSU_BEATMAPSET_URL = "https://osu.ppy.sh/api/v2/beatmapsets/{id}"
OSUCOLLECTOR_API = "https://osucollector.com/api/collections/{id}"
OSUCOLLECTOR_BEATMAPSETS_API = "https://osucollector.com/api/collections/{id}/beatmapsets"

MAX_PARALLEL_DOWNLOADS = 10
CHUNK_SIZE = 65_536
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 60

MODE_OPTIONS = [
    ("All Modes", "_all"),
    ("osu!", "0"),
    ("Taiko", "1"),
    ("Catch", "2"),
    ("Mania", "3"),
]

STATUS_OPTIONS = [
    ("Any Status", "_any"),
    ("Ranked", "ranked"),
    ("Loved", "loved"),
    ("Qualified", "qualified"),
    ("Pending", "pending"),
    ("Graveyard", "graveyard"),
]

SORT_OPTIONS = [
    ("Relevance", "_default"),
    ("Title ↑", "title_asc"),
    ("Title ↓", "title_desc"),
    ("Artist ↑", "artist_asc"),
    ("Artist ↓", "artist_desc"),
    ("Difficulty ↑", "difficulty_asc"),
    ("Difficulty ↓", "difficulty_desc"),
    ("Plays ↓", "plays_desc"),
    ("Favourites ↓", "favourites_desc"),
    ("Ranked ↓", "ranked_desc"),
]

_NO_FILTER_TOKENS = {"_all", "_any", "_default"}


class ConfigManager:
    def __init__(self, config_path: str | None = None):
        if config_path:
            self.config_path = Path(config_path)
        else:
            if "XDG_CONFIG_HOME" in os.environ:
                config_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "osu-beatmapdl"
            else:
                config_dir = Path.home() / ".config" / "osu-beatmapdl"
            
            self.config_path = config_dir / "config.json"
        
        self.config = self._load()
    
    def _default_config(self) -> dict:
        return {
            "version": "1.0",
            "auth": {
                "client_id": "",
                "client_secret": "",
                "save_credentials": False,
            },
            "download": {
                "path": os.path.expanduser("~/Downloads"),
                "max_parallel": MAX_PARALLEL_DOWNLOADS,
                "chunk_size": CHUNK_SIZE,
            },
            "ui": {
                "theme": "nord",
            },
            "advanced": {
                "connect_timeout": CONNECT_TIMEOUT,
                "read_timeout": READ_TIMEOUT,
            }
        }
    
    def _load(self) -> dict:
        if self.config_path.exists():
            try:
                with open(self.config_path, "r") as f:
                    loaded = json.load(f)
                    defaults = self._default_config()
                    return self._deep_merge(defaults, loaded)
            except Exception as e:
                print(f"Warning: Failed to load config from {self.config_path}: {e}")
        
        return self._default_config()
    
    def _deep_merge(self, defaults: dict, loaded: dict) -> dict:
        result = defaults.copy()
        for key, value in loaded.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def save(self) -> bool:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=2)
            return True
        except Exception as e:
            print(f"Warning: Failed to save config to {self.config_path}: {e}")
            return False
    
    def get(self, key: str, default=None):
        keys = key.split(".")
        value = self.config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value
    
    def set(self, key: str, value):
        keys = key.split(".")
        target = self.config
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value



def safe_filename(name: str, max_len: int = 100) -> str:
    return "".join(c for c in name if c.isalnum() or c in " -_").strip()[:max_len]


_session = requests.Session()
_session.headers.update({"User-Agent": "osu-dl/2.0"})


class OsuDownloader:
    def __init__(self):
        self.access_token: str | None = None

    def authenticate(self, client_id: str, client_secret: str) -> bool:
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "public",
        }
        try:
            response = _session.post(OSU_TOKEN_URL, json=payload, timeout=(CONNECT_TIMEOUT, 15))
            if response.status_code == 200:
                self.access_token = response.json()["access_token"]
                return True
        except Exception:
            pass
        return False

    def search(self, query: str, mode: str = "", status: str = "", sort: str = "") -> list:
        if not self.access_token:
            return []
        headers = {"Authorization": f"Bearer {self.access_token}"}
        params: dict = {"q": query}
        if mode:
            params["m"] = mode
        if status:
            params["s"] = status
        if sort:
            params["sort"] = sort
        try:
            response = _session.get(
                OSU_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=(CONNECT_TIMEOUT, 15),
            )
            if response.status_code == 200:
                return response.json().get("beatmapsets", [])
        except Exception:
            pass
        return []

    def enrich_beatmapsets(self, beatmapsets: list, progress_callback=None, skip_event=None) -> list:
        if not self.access_token:
            return beatmapsets
        headers = {"Authorization": f"Bearer {self.access_token}"}
        enriched = list(beatmapsets)
        total = len(enriched)
        for i, b in enumerate(enriched):
            if skip_event and skip_event.is_set():
                break
            if b.get("artist") and b.get("title"):
                if progress_callback:
                    progress_callback(i + 1, total)
                continue
            bset_id = b.get("id") or b.get("beatmapset_id")
            if not bset_id:
                continue
            try:
                r = _session.get(
                    OSU_BEATMAPSET_URL.format(id=bset_id),
                    headers=headers,
                    timeout=(CONNECT_TIMEOUT, 15),
                )
                if r.status_code == 200:
                    enriched[i] = r.json()
            except Exception:
                pass
            if progress_callback:
                progress_callback(i + 1, total)
        return enriched

    def _try_mirror(self, url: str, file_path: str, progress_callback=None, speed_callback=None) -> tuple[bool, str]:
        try:
            r = _session.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True)

            if r.status_code in (404, 429, 403):
                return False, f"HTTP {r.status_code}"
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"

            content_type = r.headers.get("content-type", "").lower()
            content_length = int(r.headers.get("content-length", 0) or 0)

            is_file = (
                any(t in content_type for t in ("zip", "osu-beatmap", "octet-stream"))
                or content_length > 10_000
            )

            if "json" in content_type or (0 < content_length < 500):
                try:
                    return False, r.json().get("error", "mirror error")
                except Exception:
                    return False, "invalid response"

            if not is_file:
                return False, "unexpected content"

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            bytes_downloaded = 0
            start_time = time.monotonic()
            speed_tick = start_time

            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    f.write(chunk)
                    bytes_downloaded += len(chunk)

                    now = time.monotonic()

                    if content_length > 0 and progress_callback:
                        progress_callback(int(bytes_downloaded / content_length * 100))

                    if speed_callback and (now - speed_tick) >= 0.5:
                        elapsed = now - start_time
                        if elapsed > 0:
                            speed_callback(bytes_downloaded / elapsed / 1_048_576)
                        speed_tick = now

            return True, file_path

        except requests.exceptions.Timeout:
            return False, "timed out"
        except requests.exceptions.ConnectionError as e:
            return False, f"connection error: {str(e)[:40]}"
        except Exception as e:
            return False, f"error: {str(e)[:60]}"

    def download(
        self,
        beatmapset_id: int,
        path: str,
        title: str | None = None,
        progress_callback=None,
        speed_callback=None,
    ) -> tuple[bool, str]:
        name = safe_filename(title) if title else str(beatmapset_id)
        file_path = os.path.join(path, f"{name}.osz")

        all_mirrors = MIRRORS + BACKUP_DB_MIRRORS

        for mirror_template in all_mirrors:
            url = mirror_template.format(id=beatmapset_id)
            ok, result = self._try_mirror(url, file_path, progress_callback, speed_callback)
            if ok:
                return True, result

        return False, f"all {len(all_mirrors)} mirrors failed"


class OsuCollectorScraper:
    def __init__(self):
        self.last_error: str = ""

    def fetch_collection(self, collection_id: int) -> tuple[str, list]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        try:
            r = _session.get(
                OSUCOLLECTOR_API.format(id=collection_id),
                headers=headers,
                timeout=(CONNECT_TIMEOUT, 15),
            )
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                return "", []

            meta = r.json()
            name = meta.get("name", f"Collection {collection_id}")

            if "beatmapsets" in meta and isinstance(meta["beatmapsets"], list):
                return name, meta["beatmapsets"]

            beatmapsets = []
            perPage = 100
            page = 1
            while True:
                r2 = _session.get(
                    OSUCOLLECTOR_BEATMAPSETS_API.format(id=collection_id),
                    headers=headers,
                    params={"perPage": perPage, "page": page},
                    timeout=(CONNECT_TIMEOUT, 15),
                )
                if r2.status_code != 200:
                    self.last_error = f"beatmapsets page {page} HTTP {r2.status_code}: {r2.text[:200]}"
                    break
                data = r2.json()

                if isinstance(data, list):
                    chunk = data
                    has_more = len(chunk) == perPage
                elif isinstance(data, dict):
                    chunk = (
                        data.get("beatmapsets")
                        or data.get("items")
                        or data.get("data")
                        or []
                    )
                    has_more = data.get("hasMore", data.get("has_more", len(chunk) == perPage))
                else:
                    break

                beatmapsets.extend(chunk)

                if not has_more or not chunk:
                    break

                page += 1

            return name, beatmapsets

        except Exception as e:
            self.last_error = str(e)
            return "", []


class AuthPromptScreen(ModalScreen):
    """Modal dialog asking the user whether to authenticate before loading a collection."""

    CSS = """
    AuthPromptScreen {
        align: center middle;
    }
    #auth-prompt-dialog {
        width: 60;
        height: auto;
        border: tall $primary;
        background: $surface;
        padding: 1 2;
    }
    #auth-prompt-warning {
        color: #ffaa44;
        text-align: center;
        margin-bottom: 1;
    }
    #auth-prompt-msg {
        color: $foreground;
        text-align: center;
        margin-bottom: 1;
    }
    #auth-prompt-buttons {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    #auth-prompt-buttons Button {
        width: 16;
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="auth-prompt-dialog"):
            yield Static(
                "⚠  Not Authenticated",
                id="auth-prompt-warning",
            )
            yield Static(
                "Without osu! API authentication, metadata such as artist names, "
                "song titles and other details will not be shown for collection maps.\n\n"
                "Would you like to authenticate first?",
                id="auth-prompt-msg",
            )
            with Horizontal(id="auth-prompt-buttons"):
                yield Button("Authenticate", id="auth-prompt-yes", variant="primary")
                yield Button("Continue Anyway", id="auth-prompt-no")
                yield Button("Cancel", id="auth-prompt-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "auth-prompt-yes":
            self.dismiss("authenticate")
        elif event.button.id == "auth-prompt-no":
            self.dismiss("continue")
        else:
            self.dismiss("cancel")


class MainScreen(Container):
    def compose(self) -> ComposeResult:
        with Horizontal(id="auth-row"):
            yield Input(placeholder="Client ID", id="client-id")
            yield Input(placeholder="Client Secret", password=True, id="client-secret")
            yield Button("Authenticate", id="auth-btn")

        with Horizontal(id="search-row"):
            yield Input(placeholder="Search beatmaps…", id="search-input")
            yield Button("Search", id="search-btn")
            yield Button("Download", id="download-btn")

        with Horizontal(id="collector-row"):
            yield Input(placeholder="osu!collector collection ID or URL…", id="collector-input")
            yield Button("Load Collection", id="collector-btn")

        with Horizontal(id="filter-row"):
            yield Static("Filters:", id="filter-label")
            yield Select(
                options=MODE_OPTIONS,
                value="_all",
                id="mode-select",
                allow_blank=False,
            )
            yield Select(
                options=STATUS_OPTIONS,
                value="_any",
                id="status-select",
                allow_blank=False,
            )
            yield Select(
                options=SORT_OPTIONS,
                value="_default",
                id="sort-select",
                allow_blank=False,
            )

        with Horizontal(id="path-row"):
            yield Input(placeholder="Download path", id="path-input")


        yield Static("", id="status")

        with Horizontal(id="progress-row"):
            yield Static("", id="progress-text")
            yield ProgressBar(id="progress", show_percentage=False, total=100)
            yield Static("", id="speed")
            yield Button("Skip", id="skip-enrich-btn", classes="hidden")

        yield DataTable(id="results", cursor_type="row")


class OsuTui(App):
    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }

    MainScreen {
        padding: 0 1;
    }

    #collector-row, #auth-row, #search-row, #filter-row, #path-row {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }

    #collector-row Input { width: 1fr; margin-right: 1; }
    #collector-row Button { width: 20; }

    #auth-row Input { width: 1fr; margin-right: 1; }
    #auth-row Button { width: 18; }

    #search-row Input { width: 1fr; margin-right: 1; }
    #search-row Button { width: 14; margin-right: 1; }

    #filter-row Select { width: 1fr; margin-right: 1; }
    #filter-label {
        width: auto;
        margin-right: 1;
        color: $text-muted;
    }

    #path-row Input { width: 1fr; margin-right: 1; }
    #path-row Select { width: 22; }
    #theme-label {
        width: auto;
        margin-right: 1;
        color: $text-muted;
    }

    Input {
        background: $surface;
        border: tall $panel;
        color: $foreground;
        height: 3;
    }
    Input:focus {
        border: tall $primary;
    }

    Button {
        background: $surface;
        color: $primary;
        border: tall $panel;
        height: 3;
    }
    Button:hover {
        background: $panel;
        color: $accent;
        border: tall $primary;
    }
    Button:focus {
        border: tall $primary;
    }

    Select {
        background: $surface;
        border: tall $panel;
        color: $foreground;
    }
    Select:focus {
        border: tall $primary;
    }
    SelectOverlay {
        background: $surface;
        border: tall $primary;
    }
    SelectOverlay > .option-list--option-highlighted {
        background: $primary;
        color: $background;
    }
    SelectOverlay > .option-list--option-hover {
        background: $panel;
    }
    Select > SelectCurrent {
        background: $surface;
        color: $foreground;
    }

    #status {
        height: 1;
        margin: 1 0 0 0;
        color: $text-muted;
        padding: 0 1;
    }

    #progress-row {
        height: 3;
        margin-bottom: 0;
        align: left middle;
    }
    #skip-enrich-btn {
        width: 8;
        height: 3;
        margin-left: 1;
        display: none;
    }
    #skip-enrich-btn.visible {
        display: block;
    }
    #progress-text {
        width: 6;
        color: $accent;
        text-align: right;
    }
    #progress {
        width: 1fr;
        margin: 0 1;
    }
    #speed {
        width: 12;
        color: $accent;
        text-align: right;
    }

    ProgressBar > .bar--bar {
        color: $primary;
    }
    ProgressBar > .bar--complete {
        color: $success;
    }

    #results {
        height: 1fr;
        border: tall $surface;
        margin-top: 1;
    }
    DataTable {
        background: $background;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $primary;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $boost;
        color: $foreground;
    }
    DataTable > .datatable--hover {
        background: $surface;
    }

    Header {
        background: $background;
        color: $primary;
        height: 1;
    }
    Footer {
        background: $background;
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+q", "force_quit", "Force Quit"),
        ("ctrl+a", "select_all", "Select All"),
        ("d", "download_selected", "Download"),
        ("enter", "download_selected", "Download"),
        ("space", "toggle_selection", "Toggle"),
        ("ctrl+t", "next_theme", "Next Theme"),
    ]

    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.downloader = OsuDownloader()
        self.collector_scraper = OsuCollectorScraper()
        self.results: list = []
        self.selected_rows: set[int] = set()
        self.download_path = self.config.get("download.path", os.path.expanduser("~/Downloads"))
        self._progress_lock = threading.Lock()
        self._completed_counts: dict[int, int] = {}
        self._cancel_event = threading.Event()
        self._skip_enrich_event = threading.Event()
        self._theme_index = 0
        saved_theme = self.config.get("ui.theme", "nord")
        if saved_theme in THEMES:
            self._theme_index = THEMES.index(saved_theme)
            self.theme = saved_theme

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield MainScreen()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).value = self.download_path
        saved_client_id = self.config.get("auth.client_id", "")
        saved_client_secret = self.config.get("auth.client_secret", "")
        save_credentials = self.config.get("auth.save_credentials", False)
        
        if save_credentials and saved_client_id:
            self.query_one("#client-id", Input).value = saved_client_id
            if saved_client_secret:
                self.query_one("#client-secret", Input).value = saved_client_secret
        
        table = self.query_one("#results", DataTable)
        table.add_columns("", "#", "ID", "Artist", "Title", "Creator", "Difficulties", "Status")
        current = getattr(self, "theme", THEMES[0])
        if current in THEMES:
            self._theme_index = THEMES.index(current)
            try:
                self.query_one("#theme-select", Select).value = current
            except Exception:
                pass
        self.set_status("Enter your osu! API credentials and authenticate to begin.", color="muted")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "theme-select":
            val = event.value
            if val and val != Select.BLANK:
                self.theme = val
                if val in THEMES:
                    self._theme_index = THEMES.index(val)
                    self.config.set("ui.theme", val)
                    self.config.save()

    def action_next_theme(self) -> None:
        self._theme_index = (self._theme_index + 1) % len(THEMES)
        new_theme = THEMES[self._theme_index]
        self.theme = new_theme
        self.config.set("ui.theme", new_theme)
        self.config.save()
        try:
            self.query_one("#theme-select", Select).value = new_theme
        except Exception:
            pass
        self.set_status(f"Theme: {new_theme}", color="muted")

    def action_force_quit(self) -> None:
        self._cancel_event.set()
        self.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "auth-btn": self.authenticate,
            "search-btn": self.search,
            "download-btn": self.action_download_selected,
            "collector-btn": self.load_collection,
            "skip-enrich-btn": self._do_skip_enrichment,
        }
        handler = actions.get(event.button.id)
        if handler:
            handler()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self.search()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row = event.cursor_row
        if 0 <= row < len(self.results):
            b = self.results[row]
            sel = "✓ selected" if row in self.selected_rows else ""
            artist = b.get("artist") or b.get("artist_unicode") or "Unknown Artist"
            title = b.get("title") or b.get("title_unicode") or f"ID {b.get('id', '?')}"
            creator = b.get("creator", "")
            self.set_status(f"{artist} — {title}  [{creator}]  {sel}", color="muted")

    def set_status(self, msg: str, color: str = "normal") -> None:
        colors = {"normal": "#e0e0f0", "muted": "#666688", "ok": "#44cc88", "warn": "#ffaa44", "err": "#ff4466"}
        hex_color = colors.get(color, colors["normal"])
        self.query_one("#status", Static).update(f"[{hex_color}]{msg}[/{hex_color}]")

    def set_progress(self, percent: int, speed: str = "") -> None:
        self.query_one("#progress", ProgressBar).update(progress=percent)
        self.query_one("#progress-text", Static).update(f"{percent}%")
        self.query_one("#speed", Static).update(speed)

    def _set_skip_btn_visible(self, visible: bool) -> None:
        btn = self.query_one("#skip-enrich-btn", Button)
        if visible:
            btn.remove_class("hidden")
            btn.add_class("visible")
        else:
            btn.remove_class("visible")
            btn.add_class("hidden")

    def _do_skip_enrichment(self) -> None:
        self._skip_enrich_event.set()

    def load_collection(self) -> None:
        raw = self.query_one("#collector-input", Input).value.strip()
        if not raw:
            self.set_status("Enter a collection ID or osucollector.com URL.", color="warn")
            return

        collection_id: int | None = None
        if raw.isdigit():
            collection_id = int(raw)
        else:
            import re
            m = re.search(r"/collections/(\d+)", raw)
            if m:
                collection_id = int(m.group(1))

        if collection_id is None:
            self.set_status("Couldn't parse a collection ID from that input.", color="err")
            return

        if not self.downloader.access_token:
            def _on_auth_prompt(result: str) -> None:
                if result == "authenticate":
                    self.set_status(
                        "Enter your osu! API credentials above and press Authenticate, then load the collection again.",
                        color="warn",
                    )
                    self.query_one("#client-id", Input).focus()
                elif result == "continue":
                    self._do_load_collection(collection_id)
                # "cancel" → do nothing

            self.app.push_screen(AuthPromptScreen(), _on_auth_prompt)
            return

        self._do_load_collection(collection_id)

    def _do_load_collection(self, collection_id: int) -> None:
        self.set_status(f"Fetching collection {collection_id}…", color="muted")

        def _run():
            name, beatmapsets = self.collector_scraper.fetch_collection(collection_id)
            if not beatmapsets:
                err = self.collector_scraper.last_error or "no beatmapsets returned"
                self.call_later(lambda e=err: self.set_status(f"Collection fetch failed: {e}", color="err"))
                return

            needs_enrichment = any(not b.get("artist") or not b.get("title") for b in beatmapsets)
            if needs_enrichment and self.downloader.access_token:
                total = len(beatmapsets)
                skip_event = threading.Event()
                self._skip_enrich_event = skip_event
                self.call_later(lambda: self._set_skip_btn_visible(True))
                self.call_later(lambda n=name, t=total: self.set_status(
                    f'Loaded "{n}" — enriching {t} maps with osu! API…', color="muted"
                ))

                def _progress(done, total):
                    pct = int(done / total * 100)
                    self.call_later(lambda p=pct, d=done, t=total: (
                        self._set_progress_value(p),
                        self.set_status(f"Enriching metadata… {d}/{t} (Skip to load without metadata)", color="muted"),
                    ))

                beatmapsets = self.downloader.enrich_beatmapsets(
                    beatmapsets,
                    progress_callback=_progress,
                    skip_event=skip_event,
                )
                self.call_later(lambda: self._set_skip_btn_visible(False))
                self.call_later(lambda: self._set_progress_value(0))
            elif needs_enrichment:
                self.call_later(lambda: self.set_status(
                    "Tip: Authenticate with osu! API to enrich collection metadata.", color="muted"
                ))

            self.results = beatmapsets
            self.call_later(lambda n=name, bs=beatmapsets: self._on_collection_loaded(n, bs))

        threading.Thread(target=_run, daemon=True).start()

    def _on_collection_loaded(self, name: str, beatmapsets: list) -> None:
        self._populate_results(beatmapsets)
        self.set_status(f'Loaded "{name}" — {len(beatmapsets)} maps. Space to select, Ctrl+A for all, D to download.', color="ok")

    def authenticate(self) -> None:
        client_id = self.query_one("#client-id", Input).value.strip()
        client_secret = self.query_one("#client-secret", Input).value.strip()

        if not client_id or not client_secret:
            self.set_status("Please enter both Client ID and Client Secret.", color="warn")
            return

        self.set_status("Authenticating…", color="muted")

        def _run():
            success = self.downloader.authenticate(client_id, client_secret)
            if success:
                self.config.set("auth.client_id", client_id)
                self.config.set("auth.client_secret", client_secret)
                self.config.set("auth.save_credentials", True)
                self.config.save()
                msg = "Authenticated successfully. Ready to search!"
            else:
                msg = "Authentication failed — check your credentials."
            
            color = "ok" if success else "err"
            self.call_later(lambda: self.set_status(msg, color=color))

        threading.Thread(target=_run, daemon=True).start()

    def _get_filter_values(self) -> tuple[str, str, str]:
        def _val(widget_id: str) -> str:
            try:
                v = self.query_one(widget_id, Select).value
                if v == Select.BLANK or v in _NO_FILTER_TOKENS:
                    return ""
                return str(v)
            except Exception:
                return ""

        return _val("#mode-select"), _val("#status-select"), _val("#sort-select")

    def search(self) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if not query:
            return

        if not self.downloader.access_token:
            self.set_status("Not authenticated — click Authenticate first.", color="warn")
            return

        mode, status, sort = self._get_filter_values()

        active_filters = []
        if mode:
            label = next((l for l, v in MODE_OPTIONS if v == mode), mode)
            active_filters.append(label)
        if status:
            label = next((l for l, v in STATUS_OPTIONS if v == status), status)
            active_filters.append(label)
        if sort:
            label = next((l for l, v in SORT_OPTIONS if v == sort), sort)
            active_filters.append(f"sort: {label}")

        filter_str = f"  [{', '.join(active_filters)}]" if active_filters else ""
        self.set_status(f'Searching for "{query}"{filter_str}...', color="muted")
        self.query_one("#results", DataTable).clear()

        def _run():
            results = self.downloader.search(query, mode=mode, status=status, sort=sort)
            self.results = results
            self.call_later(self._populate_results, results)

        threading.Thread(target=_run, daemon=True).start()

    def _populate_results(self, results: list) -> None:
        self.selected_rows.clear()
        table = self.query_one("#results", DataTable)
        table.clear(columns=True)
        table.add_columns("", "#", "ID", "Artist", "Title", "Creator", "Difficulties", "Status")

        for i, b in enumerate(results):
            diff_count = len(b.get("beatmaps", []))
            status_val = b.get("status", "")
            table.add_row(
                " ",
                str(i + 1),
                str(b["id"]),
                b.get("artist", "")[:28],
                b.get("title", "")[:32],
                b.get("creator", "")[:18],
                str(diff_count),
                status_val,
            )

        table.focus()
        noun = "result" if len(results) == 1 else "results"
        self.set_status(f"Found {len(results)} {noun}. Space to select, Ctrl+A for all, D to download.", color="ok")

    def _refresh_row_markers(self) -> None:
        table = self.query_one("#results", DataTable)
        for row_idx in range(table.row_count):
            marker = "✓" if row_idx in self.selected_rows else " "
            table.update_cell_at((row_idx, 0), marker)

    def action_toggle_selection(self) -> None:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self.results)):
            return

        if row in self.selected_rows:
            self.selected_rows.discard(row)
        else:
            self.selected_rows.add(row)

        self._refresh_row_markers()
        count = len(self.selected_rows)
        noun = "map" if count == 1 else "maps"
        self.set_status(f"{count} {noun} selected — press D to download.", color="muted" if count == 0 else "ok")

    def action_select_all(self) -> None:
        table = self.query_one("#results", DataTable)
        all_rows = set(range(table.row_count))
        if self.selected_rows == all_rows and all_rows:
            self.selected_rows.clear()
            self._refresh_row_markers()
            self.set_status("All maps deselected.", color="muted")
        else:
            self.selected_rows = all_rows
            self._refresh_row_markers()
            self.set_status(f"All {len(self.selected_rows)} maps selected — press D to download.", color="ok")

    def action_download_selected(self) -> None:
        if not self.selected_rows:
            table = self.query_one("#results", DataTable)
            row = table.cursor_row
            if row is not None and 0 <= row < len(self.results):
                self.selected_rows.add(row)

        if self.selected_rows:
            self._download_batch(sorted(self.selected_rows))
        else:
            self.set_status("Nothing selected — use Space to select maps, or Ctrl+A for all.", color="warn")

    def _download_batch(self, indices: list[int]) -> None:
        path = self.query_one("#path-input", Input).value.strip() or self.download_path
        self.config.set("download.path", path)
        self.config.save()
        self.download_path = path
        
        count = len(indices)
        self.set_status(f"Starting {count} downloads (up to {MAX_PARALLEL_DOWNLOADS} parallel)…", color="muted")
        self.set_progress(0)

        self._completed_counts = {"success": 0, "fail": 0, "done": 0, "total": count}
        self._cancel_event.clear()
        failed_maps: list[str] = []
        lock = threading.Lock()

        def _download_one(idx: int):
            if self._cancel_event.is_set():
                return
            if not (0 <= idx < len(self.results)):
                return

            b = self.results[idx]
            beatmap_id = b["id"]
            artist = b.get("artist", "")
            title = b.get("title", "")
            filename = f"{artist} - {title}" if artist and title else str(beatmap_id)

            ok, result = self.downloader.download(beatmap_id, path, filename)

            with lock:
                self._completed_counts["done"] += 1
                done = self._completed_counts["done"]

                if ok:
                    self._completed_counts["success"] += 1
                else:
                    self._completed_counts["fail"] += 1
                    failed_maps.append(f"{filename[:25]}: {result}")

                overall = int(done / count * 100)
                label = filename[:40]
                s = self._completed_counts["success"]
                f_ = self._completed_counts["fail"]
                self.call_later(
                    lambda v=overall, lbl=label, sd=s, fd=f_: (
                        self._set_progress_value(v),
                        self.set_status(
                            f"({sd + fd}/{count}) ✓{sd} ✗{fd}  last: {lbl}", color="muted"
                        ),
                    )
                )

        def _run():
            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS) as pool:
                futures = [pool.submit(_download_one, idx) for idx in indices]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        pass

            self.call_later(
                lambda: self._finish_batch(
                    self._completed_counts["success"],
                    self._completed_counts["fail"],
                    failed_maps,
                )
            )

        threading.Thread(target=_run, daemon=True).start()

    def _set_progress_value(self, v: int) -> None:
        self.query_one("#progress", ProgressBar).update(progress=v)
        self.query_one("#progress-text", Static).update(f"{v}%")

    def _finish_batch(self, success: int, failed: int, failed_maps: list[str]) -> None:
        self.set_progress(100, "Done")

        if success > 0 and failed == 0:
            self.set_status(f"All {success} maps downloaded successfully!", color="ok")
        elif success > 0:
            preview = "; ".join(failed_maps[:3])
            extra = f" (+{len(failed_maps) - 3} more)" if len(failed_maps) > 3 else ""
            self.set_status(f"{success} downloaded, {failed} failed: {preview}{extra}", color="warn")
        else:
            preview = "; ".join(failed_maps[:3])
            extra = f" (+{len(failed_maps) - 3} more)" if len(failed_maps) > 3 else ""
            self.set_status(f"All {failed} downloads failed: {preview}{extra}", color="err")


if __name__ == "__main__":
    OsuTui().run()