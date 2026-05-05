import os
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
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

# Tokens used as "no filter" sentinels in Select widgets
_NO_FILTER_TOKENS = {"_all", "_any", "_default"}


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


class MainScreen(Container):
    def compose(self) -> ComposeResult:
        yield Static("  ░█████╗░░██████╗██╗░░░██╗  ██████╗░██╗░░░░░", id="logo-1")
        yield Static("  ██╔══██╗██╔════╝██║░░░██║  ██╔══██╗██║░░░░░", id="logo-2")
        yield Static("  ██║░░██║╚█████╗░██║░░░██║  ██║░░██║██║░░░░░", id="logo-3")
        yield Static("  ██║░░██║░╚═══██╗██║░░░██║  ██║░░██║██║░░░░░", id="logo-4")
        yield Static("  ╚█████╔╝██████╔╝╚██████╔╝  ██████╔╝███████╗", id="logo-5")
        yield Static("  ░╚════╝░░╚════╝░░╚═════╝░  ╚═════╝░╚══════╝", id="logo-6")

        with Horizontal(id="auth-row"):
            yield Input(placeholder="Client ID", id="client-id")
            yield Input(placeholder="Client Secret", password=True, id="client-secret")
            yield Button("Authenticate", id="auth-btn")

        with Horizontal(id="search-row"):
            yield Input(placeholder="Search beatmaps…", id="search-input")
            yield Button("Search", id="search-btn")
            yield Button("Download", id="download-btn")

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

        yield DataTable(id="results", cursor_type="row")


class OsuTui(App):
    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }

    #logo-1, #logo-2, #logo-3, #logo-4, #logo-5 {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    #logo-6 {
        color: $primary;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }

    MainScreen {
        padding: 1 2;
    }

    #auth-row, #search-row, #filter-row, #path-row {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }

    #auth-row Input { width: 18; margin-right: 1; }
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
        margin: 0 0 1 0;
        color: $text-muted;
        padding: 0 1;
    }

    #progress-row {
        height: 1;
        margin-bottom: 1;
        align: left middle;
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
    }
    Footer {
        background: $background;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+a", "select_all", "Select All"),
        ("d", "download_selected", "Download"),
        ("enter", "download_selected", "Download"),
        ("space", "toggle_selection", "Toggle"),
        ("ctrl+t", "next_theme", "Next Theme"),
    ]

    def __init__(self):
        super().__init__()
        self.downloader = OsuDownloader()
        self.results: list = []
        self.selected_rows: set[int] = set()
        self.download_path = os.path.expanduser("~/Downloads")
        self._progress_lock = threading.Lock()
        self._completed_counts: dict[int, int] = {}
        self._theme_index = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield MainScreen()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).value = self.download_path
        # Pre-populate table columns so clicks before any search don't crash
        table = self.query_one("#results", DataTable)
        table.add_columns("", "#", "ID", "Artist", "Title", "Creator", "Maps", "Status")
        # Set default theme in the selector to match app's current theme
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

    def action_next_theme(self) -> None:
        self._theme_index = (self._theme_index + 1) % len(THEMES)
        new_theme = THEMES[self._theme_index]
        self.theme = new_theme
        try:
            self.query_one("#theme-select", Select).value = new_theme
        except Exception:
            pass
        self.set_status(f"Theme: {new_theme}", color="muted")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "auth-btn": self.authenticate,
            "search-btn": self.search,
            "download-btn": self.action_download_selected,
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
            self.set_status(f"{b['artist']} — {b['title']}  [{b['creator']}]  {sel}", color="muted")

    def set_status(self, msg: str, color: str = "normal") -> None:
        colors = {"normal": "#e0e0f0", "muted": "#666688", "ok": "#44cc88", "warn": "#ffaa44", "err": "#ff4466"}
        hex_color = colors.get(color, colors["normal"])
        self.query_one("#status", Static).update(f"[{hex_color}]{msg}[/{hex_color}]")

    def set_progress(self, percent: int, speed: str = "") -> None:
        self.query_one("#progress", ProgressBar).update(progress=percent)
        self.query_one("#progress-text", Static).update(f"{percent}%")
        self.query_one("#speed", Static).update(speed)

    def authenticate(self) -> None:
        client_id = self.query_one("#client-id", Input).value.strip()
        client_secret = self.query_one("#client-secret", Input).value.strip()

        if not client_id or not client_secret:
            self.set_status("Please enter both Client ID and Client Secret.", color="warn")
            return

        self.set_status("Authenticating…", color="muted")

        def _run():
            success = self.downloader.authenticate(client_id, client_secret)
            msg = "Authenticated successfully. Ready to search!" if success else "Authentication failed — check your credentials."
            color = "ok" if success else "err"
            self.call_later(lambda: self.set_status(msg, color=color))

        threading.Thread(target=_run, daemon=True).start()

    def _get_filter_values(self) -> tuple[str, str, str]:
        """Return (mode, status, sort) from the filter selects."""
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

        # Build a human-readable filter summary for the status bar
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
        table.add_columns("", "#", "ID", "Artist", "Title", "Creator", "Maps", "Status")

        for i, b in enumerate(results[:50]):
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
        for row_idx in range(len(self.results)):
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
        self.selected_rows = set(range(len(self.results)))
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
        count = len(indices)
        self.set_status(f"Starting {count} downloads (up to {MAX_PARALLEL_DOWNLOADS} parallel)…", color="muted")
        self.set_progress(0)

        self._completed_counts = {"success": 0, "fail": 0, "done": 0, "total": count}
        failed_maps: list[str] = []
        lock = threading.Lock()

        def _download_one(idx: int):
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
