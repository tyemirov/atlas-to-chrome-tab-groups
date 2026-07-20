#!/usr/bin/env python3
"""Safely export persistent Atlas tab groups and recreate them in Chrome.

This program deliberately reads only Atlas's application-owned tab and tab-group
property lists.  It never reads, writes, copies, or imports browser cookies,
passwords, Chromium Sessions files, History, or Chrome profile databases.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.server
import json
import os
import plistlib
import secrets
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit


PROGRAM_VERSION = "1.0.0"
EXPORT_SCHEMA_VERSION = 1
EXTENSION_ID = "lciilifhlpkehndanddbcfcliikmmdhj"
PALETTE = ("grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange")
DEFAULT_ATLAS_ROOT = Path.home() / "Library/Application Support/com.openai.atlas"
DEFAULT_CHROME_DATA_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
DEFAULT_CHROME_APP = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
MAX_EXPORT_BYTES = 100 * 1024 * 1024


class MigrationError(RuntimeError):
    """A safe, actionable failure that leaves both browsers untouched."""


class EventLog:
    """Writes operational facts only: never URLs, tab titles, or group names."""

    def __init__(self, destination: Path | None = None) -> None:
        self.destination = destination
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.touch(mode=0o600, exist_ok=False)
            os.chmod(destination, 0o600)

    def write(self, message: str) -> None:
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}  {message}"
        print(line, flush=True)
        if self.destination is not None:
            with self.destination.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json_write(path: Path, value: Any) -> None:
    """Create a private JSON file without ever overwriting an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise MigrationError(f"Refusing to overwrite existing artifact: {path}")
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def load_bplist(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"Atlas record is not a regular file: {path.name}")
    try:
        content = path.read_bytes()
        decoded = plistlib.loads(content)
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise MigrationError(f"Unable to read Atlas record {path.name}: {error}") from error
    if not isinstance(decoded, dict):
        raise MigrationError(f"Atlas record {path.name} is not a dictionary")
    return decoded


def is_process_running(process_name: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-x", process_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def require_atlas_quit(allow_live_read: bool) -> None:
    if is_process_running("ChatGPT Atlas") and not allow_live_read:
        raise MigrationError(
            "ChatGPT Atlas is still running. Quit it normally and rerun, or use "
            "--allow-live-atlas only for a best-effort read. The script never force-quits Atlas."
        )


def discover_atlas_user(atlas_root: Path, selected: Path | None) -> Path:
    if selected is not None:
        candidate = selected.expanduser().resolve()
        if not (candidate / "tabs").is_dir() or not (candidate / "tabgroups").is_dir():
            raise MigrationError("--atlas-user-dir must contain both tabs/ and tabgroups/ directories")
        return candidate

    root = atlas_root.expanduser()
    if not root.is_dir():
        raise MigrationError(f"Atlas data folder was not found: {root}")
    candidates: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("user-"):
            continue
        if (child / "tabs").is_dir() and (child / "tabgroups").is_dir():
            if any((child / "tabgroups").glob("window:*.data")):
                candidates.append(child)
    if not candidates:
        raise MigrationError("No active Atlas window records were found under the Atlas data folder")
    if len(candidates) != 1:
        raise MigrationError(
            "More than one Atlas workspace has active window records. Re-run with "
            "--atlas-user-dir and the one workspace folder you intend to migrate."
        )
    return candidates[0]


def current_tab_url(record: dict[str, Any], file_name: str) -> str:
    states = record.get("state")
    current_index = record.get("currentStateIndex")
    if not isinstance(states, list) or not states:
        raise MigrationError(f"Tab {file_name} has no saved state")
    if not isinstance(current_index, int) or not 0 <= current_index < len(states):
        raise MigrationError(f"Tab {file_name} has an invalid currentStateIndex")
    try:
        details = states[current_index]["details"]["web"]["_0"]
        url = details["url"]
    except (KeyError, TypeError, IndexError) as error:
        raise MigrationError(f"Tab {file_name} does not match Atlas's web-tab schema") from error
    if not isinstance(url, str):
        raise MigrationError(f"Tab {file_name} has a non-text URL")
    return url


def clean_url(raw_url: str, strip_query: bool) -> tuple[str | None, str | None]:
    """Return a browser-safe URL and a content-free reason if it was changed/skipped."""
    if not raw_url or any(ord(character) < 32 for character in raw_url):
        return None, "invalid_url"
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return None, "invalid_url"

    if raw_url == "about:blank":
        return raw_url, None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None, "unsupported_scheme"

    # Do not transfer URL-embedded credentials. Query strings remain by default
    # because they are often required to reproduce an ordinary tab location.
    netloc = parts.netloc
    credentials_removed = "@" in netloc
    if credentials_removed:
        netloc = netloc.rsplit("@", 1)[1]
    query = "" if strip_query else parts.query
    fragment = "" if strip_query else parts.fragment
    cleaned = urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
    if len(cleaned) > 65_535:
        return None, "url_too_long"
    if credentials_removed:
        return cleaned, "embedded_credentials_removed"
    if strip_query and (parts.query or parts.fragment):
        return cleaned, "query_and_fragment_removed"
    return cleaned, None


def atlas_symbol(section: dict[str, Any]) -> str:
    symbol = section.get("symbol")
    if not isinstance(symbol, dict):
        return ""
    for kind in ("emoji", "icon"):
        nested = symbol.get(kind)
        if isinstance(nested, dict) and isinstance(nested.get("_0"), str):
            return f"{kind}:{nested['_0']}"
    return ""


def deterministic_color(title: str, symbol: str) -> str:
    # Atlas's persistent group record has symbol metadata, but no Chrome-style
    # color field. Assigning a stable color preserves visual differentiation
    # without pretending to recover a source color that does not exist.
    material = (title + "\0" + symbol).encode("utf-8")
    return PALETTE[hashlib.sha256(material).digest()[0] % len(PALETTE)]


def extract_window(
    window_file: Path,
    tabs_directory: Path,
    strip_query: bool,
) -> tuple[dict[str, Any], list[Path], Counter[str]]:
    window = load_bplist(window_file)
    try:
        sections = window["sections"]
        pinned_ids = sections["pinned"]["tabIDs"]
        normal_ids = sections["normal"]["tabIDs"]
        subsections = sections["subsections"]
    except (KeyError, TypeError) as error:
        raise MigrationError(f"Window record {window_file.name} does not match Atlas's tab-group schema") from error
    if not all(isinstance(value, str) for value in pinned_ids) or not all(isinstance(value, str) for value in normal_ids):
        raise MigrationError(f"Window record {window_file.name} contains invalid tab IDs")
    if not isinstance(subsections, list):
        raise MigrationError(f"Window record {window_file.name} contains invalid subgroups")

    all_ids = list(pinned_ids) + list(normal_ids)
    if len(set(all_ids)) != len(all_ids):
        raise MigrationError(f"Window record {window_file.name} contains duplicate tab IDs")

    exported_tabs: list[dict[str, Any]] = []
    output_index_by_source_id: dict[str, int] = {}
    source_records: list[Path] = [window_file]
    notes: Counter[str] = Counter()

    for source_id, is_pinned in [*( (tab_id, True) for tab_id in pinned_ids ), *( (tab_id, False) for tab_id in normal_ids )]:
        tab_file = tabs_directory / f"{source_id}.data"
        if not tab_file.is_file():
            raise MigrationError(f"Window record {window_file.name} refers to missing tab record {source_id}.data")
        source_records.append(tab_file)
        url = current_tab_url(load_bplist(tab_file), tab_file.name)
        cleaned_url, note = clean_url(url, strip_query)
        if note is not None:
            notes[note] += 1
        if cleaned_url is None:
            continue
        output_index_by_source_id[source_id] = len(exported_tabs)
        exported_tabs.append({"url": cleaned_url, "pinned": bool(is_pinned)})

    if not exported_tabs:
        raise MigrationError(f"Window record {window_file.name} has no importable web tabs")

    groups: list[dict[str, Any]] = []
    occupied_normal_indexes: set[int] = set()
    for subsection in subsections:
        # Atlas serializes its internal subsection IDs alongside each full
        # subgroup record. Only dictionary entries define a browser tab group.
        if isinstance(subsection, str):
            continue
        if not isinstance(subsection, dict):
            raise MigrationError(f"Window record {window_file.name} contains an unknown subsection entry")
        location_range = subsection.get("range")
        if (
            not isinstance(location_range, list)
            or len(location_range) != 2
            or not all(isinstance(number, int) for number in location_range)
        ):
            raise MigrationError(f"Window record {window_file.name} contains an invalid subgroup range")
        start, end = location_range
        if not (0 <= start < end <= len(normal_ids)):
            raise MigrationError(f"Window record {window_file.name} contains an out-of-bounds subgroup range")
        range_indexes = set(range(start, end))
        if occupied_normal_indexes.intersection(range_indexes):
            raise MigrationError(f"Window record {window_file.name} contains overlapping subgroup ranges")
        occupied_normal_indexes.update(range_indexes)

        title = subsection.get("title", "")
        collapsed = subsection.get("collapsed", False)
        if not isinstance(title, str) or not isinstance(collapsed, bool):
            raise MigrationError(f"Window record {window_file.name} contains invalid subgroup metadata")
        member_indexes = [
            output_index_by_source_id[tab_id]
            for tab_id in normal_ids[start:end]
            if tab_id in output_index_by_source_id
        ]
        if not member_indexes:
            notes["empty_group_after_url_filtering"] += 1
            continue
        if member_indexes != list(range(member_indexes[0], member_indexes[-1] + 1)):
            raise MigrationError(
                f"Window record {window_file.name} would produce a non-contiguous Chrome tab group after URL filtering"
            )
        symbol = atlas_symbol(subsection)
        groups.append(
            {
                "title": title,
                "color": deterministic_color(title, symbol),
                "collapsed": collapsed,
                "tab_indexes": member_indexes,
            }
        )

    selected_tab_id = window.get("selectedTabId")
    selected_tab_index = output_index_by_source_id.get(selected_tab_id) if isinstance(selected_tab_id, str) else None
    return (
        {
            "tabs": exported_tabs,
            "groups": groups,
            "selected_tab_index": selected_tab_index,
        },
        source_records,
        notes,
    )


def build_export(
    atlas_root: Path,
    selected_user: Path | None,
    strip_query: bool,
) -> tuple[dict[str, Any], Path, list[Path], Counter[str]]:
    user_directory = discover_atlas_user(atlas_root, selected_user)
    window_files = sorted((user_directory / "tabgroups").glob("window:*.data"))
    if not window_files:
        raise MigrationError("No Atlas window records were found")

    windows: list[dict[str, Any]] = []
    source_records: list[Path] = []
    notes: Counter[str] = Counter()
    for window_file in window_files:
        window, records, window_notes = extract_window(window_file, user_directory / "tabs", strip_query)
        windows.append(window)
        source_records.extend(records)
        notes.update(window_notes)

    summary = summarize_export_windows(windows)
    migration = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exporter": "atlas-to-chrome-migrator",
        "exporter_version": PROGRAM_VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "application": "ChatGPT Atlas",
            "format": "application-owned binary plist tabgroups",
            "color_policy": "deterministic_mapping_from_atlas_symbol_and_title",
        },
        "windows": windows,
        "summary": {
            **summary,
            "filtered": dict(sorted(notes.items())),
        },
    }
    return migration, user_directory, source_records, notes


def summarize_export_windows(windows: Iterable[dict[str, Any]]) -> dict[str, int]:
    window_list = list(windows)
    return {
        "windows": len(window_list),
        "tabs": sum(len(window["tabs"]) for window in window_list),
        "pinned_tabs": sum(sum(1 for tab in window["tabs"] if tab["pinned"]) for window in window_list),
        "groups": sum(len(window["groups"]) for window in window_list),
    }


def print_summary(summary: dict[str, Any], prefix: str = "") -> None:
    filtered = summary.get("filtered", {})
    filtered_count = sum(int(count) for count in filtered.values()) if isinstance(filtered, dict) else 0
    print(
        f"{prefix}{summary['windows']} window(s), {summary['tabs']} tab(s), "
        f"{summary['pinned_tabs']} pinned tab(s), {summary['groups']} named group(s), "
        f"{filtered_count} URL adjustment(s)/omission(s)."
    )


def create_run_directory(requested: str | None) -> Path:
    if requested:
        run_directory = Path(requested).expanduser()
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_directory = Path.cwd() / f"atlas-to-chrome-export-{stamp}"
    if run_directory.exists():
        if any(run_directory.iterdir()):
            raise MigrationError(f"Refusing to use a non-empty output directory: {run_directory}")
    else:
        run_directory.mkdir(parents=True, mode=0o700)
    os.chmod(run_directory, 0o700)
    return run_directory


def write_safe_backup(
    run_directory: Path,
    migration: dict[str, Any],
    user_directory: Path,
    source_records: Iterable[Path],
) -> Path:
    """Back up only the migratable topology, never Atlas web-state blobs."""
    export_path = run_directory / "atlas-tab-groups.json"
    backup_directory = run_directory / "backup"
    atomic_json_write(export_path, migration)
    atomic_json_write(backup_directory / "atlas-tab-groups.json", migration)
    manifest_records = []
    for record in sorted(set(source_records)):
        try:
            relative = record.relative_to(user_directory)
        except ValueError as error:
            raise MigrationError("Atlas source record escaped its selected workspace directory") from error
        manifest_records.append(
            {
                "relative_path": str(relative),
                "bytes": record.stat().st_size,
                "sha256": sha256_file(record),
            }
        )
    atomic_json_write(
        backup_directory / "source-record-manifest.json",
        {
            "purpose": "Integrity record for the Atlas files read during this export",
            "raw_records_copied": False,
            "records": manifest_records,
        },
    )
    return export_path


def read_export(path: Path) -> dict[str, Any]:
    source = path.expanduser()
    if not source.is_file() or source.is_symlink():
        raise MigrationError("The export file must be a regular JSON file")
    if source.stat().st_size > MAX_EXPORT_BYTES:
        raise MigrationError("The export file is unexpectedly large")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError(f"Unable to read the export JSON: {error}") from error
    if not isinstance(value, dict):
        raise MigrationError("The export JSON must contain an object")
    validate_export(value)
    return value


def validate_export(value: dict[str, Any]) -> None:
    if value.get("schema_version") != EXPORT_SCHEMA_VERSION:
        raise MigrationError("This is not a compatible Atlas tab-group export")
    windows = value.get("windows")
    if not isinstance(windows, list) or not windows:
        raise MigrationError("The export contains no windows")
    for window_number, window in enumerate(windows, start=1):
        if not isinstance(window, dict):
            raise MigrationError(f"Window {window_number} is invalid")
        tabs = window.get("tabs")
        groups = window.get("groups")
        selected = window.get("selected_tab_index")
        if not isinstance(tabs, list) or not tabs:
            raise MigrationError(f"Window {window_number} contains no tabs")
        if not isinstance(groups, list):
            raise MigrationError(f"Window {window_number} has invalid groups")
        seen_unpinned = False
        for tab_number, tab in enumerate(tabs, start=1):
            if not isinstance(tab, dict) or not isinstance(tab.get("url"), str) or not isinstance(tab.get("pinned"), bool):
                raise MigrationError(f"Window {window_number}, tab {tab_number} is invalid")
            cleaned, change = clean_url(tab["url"], strip_query=False)
            if cleaned != tab["url"] or change is not None:
                raise MigrationError(f"Window {window_number}, tab {tab_number} has an unsafe URL")
            if not tab["pinned"]:
                seen_unpinned = True
            elif seen_unpinned:
                raise MigrationError(f"Window {window_number} has pinned tabs after ordinary tabs")
        if selected is not None and (not isinstance(selected, int) or not 0 <= selected < len(tabs)):
            raise MigrationError(f"Window {window_number} has an invalid selected tab")

        used_indexes: set[int] = set()
        for group_number, group in enumerate(groups, start=1):
            if not isinstance(group, dict):
                raise MigrationError(f"Window {window_number}, group {group_number} is invalid")
            title = group.get("title")
            color = group.get("color")
            collapsed = group.get("collapsed")
            indexes = group.get("tab_indexes")
            if not isinstance(title, str) or not isinstance(color, str) or color not in PALETTE or not isinstance(collapsed, bool):
                raise MigrationError(f"Window {window_number}, group {group_number} has invalid metadata")
            if not isinstance(indexes, list) or not indexes or not all(isinstance(index, int) for index in indexes):
                raise MigrationError(f"Window {window_number}, group {group_number} has invalid tab indexes")
            if indexes != list(range(indexes[0], indexes[-1] + 1)):
                raise MigrationError(f"Window {window_number}, group {group_number} is not contiguous")
            if any(index < 0 or index >= len(tabs) for index in indexes):
                raise MigrationError(f"Window {window_number}, group {group_number} has out-of-range tabs")
            if any(tabs[index]["pinned"] for index in indexes):
                raise MigrationError(f"Window {window_number}, group {group_number} includes a pinned tab")
            if used_indexes.intersection(indexes):
                raise MigrationError(f"Window {window_number} has overlapping groups")
            used_indexes.update(indexes)


def chrome_extension_id(extension_directory: Path) -> str:
    manifest_path = extension_directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        public_key = base64.b64decode(manifest["key"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise MigrationError("The bundled Chrome importer extension is missing or malformed") from error
    digest = hashlib.sha256(public_key).digest()[:16]
    return "".join(chr(ord("a") + (byte >> 4)) + chr(ord("a") + (byte & 15)) for byte in digest)


class ImportServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], payload: bytes, token: str) -> None:
        super().__init__(address, ImportRequestHandler)
        self.payload = payload
        self.token = token
        self.report: dict[str, Any] | None = None
        self.reported = threading.Event()


class ImportRequestHandler(http.server.BaseHTTPRequestHandler):
    server: ImportServer

    def log_message(self, _format: str, *_arguments: Any) -> None:
        # The built-in logger can contain request query strings. Keep tokens out
        # of the terminal and migration logs.
        return

    def _authorized(self) -> bool:
        parsed = urlsplit(self.path)
        received = parse_qs(parsed.query).get("token", [])
        return len(received) == 1 and secrets.compare_digest(received[0], self.server.token)

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/migration.json" or not self._authorized():
            self._send(404)
            return
        self._send(200, self.server.payload, "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/report" or not self._authorized():
            self._send(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400)
            return
        if not 0 < length <= 32_768:
            self._send(400)
            return
        try:
            report = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400)
            return
        if not isinstance(report, dict):
            self._send(400)
            return
        self.server.report = report
        self.server.reported.set()
        self._send(204)


def start_import_server(migration: dict[str, Any]) -> tuple[ImportServer, threading.Thread, str]:
    token = secrets.token_urlsafe(32)
    payload = json.dumps(migration, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    server = ImportServer(("127.0.0.1", 0), payload, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, token


def resolve_chrome_paths(arguments: argparse.Namespace) -> tuple[Path, Path, bool]:
    app = Path(arguments.chrome_app).expanduser()
    if not app.is_file():
        raise MigrationError(f"Google Chrome executable was not found: {app}")
    custom_data_dir = arguments.chrome_user_data_dir is not None
    data_root = Path(arguments.chrome_user_data_dir).expanduser() if custom_data_dir else DEFAULT_CHROME_DATA_ROOT
    profile_directory = data_root / arguments.chrome_profile
    if not custom_data_dir and not profile_directory.is_dir():
        raise MigrationError(
            f"Chrome profile '{arguments.chrome_profile}' was not found. Use the profiles command to list available profiles."
        )
    return app, data_root, custom_data_dir


def import_into_chrome(arguments: argparse.Namespace, migration: dict[str, Any], log: EventLog) -> None:
    validate_export(migration)
    summary = migration["summary"]
    if arguments.dry_run:
        log.write("Chrome import dry-run passed; Chrome was not started.")
        print_summary(summary, "Would recreate ")
        return
    if not arguments.apply:
        raise MigrationError("Refusing to create Chrome windows without --apply. Use --dry-run first if you want a no-change check.")

    chrome_app, data_root, custom_data_dir = resolve_chrome_paths(arguments)
    chrome_is_running = is_process_running("Google Chrome")
    use_running_chrome = getattr(arguments, "use_running_chrome", False)
    if use_running_chrome and custom_data_dir:
        raise MigrationError("--use-running-chrome cannot be combined with --chrome-user-data-dir.")
    if use_running_chrome and not chrome_is_running:
        raise MigrationError("--use-running-chrome requires Google Chrome to be open in the target profile.")
    if chrome_is_running and not custom_data_dir and not use_running_chrome:
        raise MigrationError(
            "Google Chrome is still running. Quit it normally and rerun so Chrome receives the temporary importer extension. "
            "The script never force-quits Chrome."
        )
    extension_directory = Path(__file__).resolve().parent / "extension"
    if chrome_extension_id(extension_directory) != EXTENSION_ID:
        raise MigrationError("The bundled importer extension ID does not match the launcher; do not mix files from different releases.")

    server, _thread, token = start_import_server(migration)
    port = server.server_address[1]
    source_url = f"http://127.0.0.1:{port}/migration.json?token={quote(token, safe='')}"
    extension_url = (
        f"chrome-extension://{EXTENSION_ID}/import.html?source={quote(source_url, safe='')}&"
        f"token={quote(token, safe='')}&mode=apply"
    )
    if use_running_chrome:
        # macOS routes this local extension URL to the existing Chrome process. This
        # mode deliberately does not close, restart, or select a different profile.
        command = ["open", "-a", "Google Chrome", extension_url]
        log.write("Opening the local importer extension in the already running Chrome profile.")
    else:
        command = [
            str(chrome_app),
            f"--profile-directory={arguments.chrome_profile}",
            extension_url,
        ]
        if custom_data_dir:
            data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(data_root, 0o700)
            command.insert(1, f"--user-data-dir={data_root}")
        log.write("Starting Chrome and opening its previously installed local importer extension.")
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        server.shutdown()
        server.server_close()
        raise MigrationError(f"Unable to start Google Chrome: {error}") from error

    log.write(f"Waiting up to {arguments.timeout} seconds for Chrome's importer report.")
    try:
        received = server.reported.wait(timeout=arguments.timeout)
    except KeyboardInterrupt:
        log.write("Interrupted while waiting. Chrome was left open; no browser data was copied.")
        raise MigrationError("Interrupted") from None
    finally:
        server.shutdown()
        server.server_close()
    if not received or server.report is None:
        raise MigrationError(
            "Chrome did not return a completion report before the timeout. The importer page may still be open; "
            "check it before rerunning, because a partial import can have created new windows."
        )

    status = server.report.get("status")
    created_windows = server.report.get("created_windows")
    created_tabs = server.report.get("created_tabs")
    created_groups = server.report.get("created_groups")
    if status == "ok" and all(isinstance(item, int) for item in (created_windows, created_tabs, created_groups)):
        log.write(
            f"Chrome importer completed: {created_windows} window(s), {created_tabs} tab(s), {created_groups} group(s)."
        )
        return
    log.write("Chrome importer reported an error; inspect the local importer page before rerunning.")
    raise MigrationError("Chrome importer reported an error. It may have created partial new windows, but never changed existing tabs.")


def import_log_for(export_path: Path) -> EventLog:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return EventLog(export_path.parent / f"chrome-import-{stamp}.log")


def command_inspect(arguments: argparse.Namespace) -> int:
    migration, _user_directory, _records, _notes = build_export(
        Path(arguments.atlas_root),
        Path(arguments.atlas_user_dir) if arguments.atlas_user_dir else None,
        arguments.strip_query,
    )
    print("Atlas persistent workspace inspection succeeded.")
    print_summary(migration["summary"])
    return 0


def command_export(arguments: argparse.Namespace) -> int:
    require_atlas_quit(arguments.allow_live_atlas)
    migration, user_directory, source_records, _notes = build_export(
        Path(arguments.atlas_root),
        Path(arguments.atlas_user_dir) if arguments.atlas_user_dir else None,
        arguments.strip_query,
    )
    if arguments.dry_run:
        print("Atlas export dry-run passed; no files were written.")
        print_summary(migration["summary"], "Would export ")
        return 0
    run_directory = create_run_directory(arguments.output_dir)
    log = EventLog(run_directory / "migration.log")
    log.write("Read Atlas's application-owned tab and tab-group records only.")
    export_path = write_safe_backup(run_directory, migration, user_directory, source_records)
    log.write("Wrote private JSON export and a sanitized topology backup; no raw Atlas records were copied.")
    print_summary(migration["summary"], "Exported ")
    print(f"Export file: {export_path}")
    return 0


def command_import(arguments: argparse.Namespace) -> int:
    export_path = Path(arguments.export_file).expanduser()
    migration = read_export(export_path)
    if arguments.dry_run:
        import_into_chrome(arguments, migration, EventLog())
        return 0
    log = import_log_for(export_path)
    import_into_chrome(arguments, migration, log)
    return 0


def command_migrate(arguments: argparse.Namespace) -> int:
    require_atlas_quit(arguments.allow_live_atlas)
    migration, user_directory, source_records, _notes = build_export(
        Path(arguments.atlas_root),
        Path(arguments.atlas_user_dir) if arguments.atlas_user_dir else None,
        arguments.strip_query,
    )
    if arguments.dry_run:
        print("Atlas-to-Chrome dry-run passed; Atlas and Chrome were not changed.")
        print_summary(migration["summary"], "Would export and recreate ")
        return 0
    run_directory = create_run_directory(arguments.output_dir)
    export_log = EventLog(run_directory / "migration.log")
    export_log.write("Read Atlas's application-owned tab and tab-group records only.")
    export_path = write_safe_backup(run_directory, migration, user_directory, source_records)
    export_log.write("Wrote private JSON export and a sanitized topology backup; no raw Atlas records were copied.")
    print_summary(migration["summary"], "Exported ")
    import_arguments = argparse.Namespace(**vars(arguments))
    import_arguments.export_file = str(export_path)
    import_into_chrome(import_arguments, migration, import_log_for(export_path))
    print(f"Migration artifacts: {run_directory}")
    return 0


def command_install(arguments: argparse.Namespace) -> int:
    """Open the supported Chrome UI for one-time user-approved unpacked install."""
    chrome_app, data_root, custom_data_dir = resolve_chrome_paths(arguments)
    if is_process_running("Google Chrome") and not custom_data_dir:
        raise MigrationError(
            "Google Chrome is still running. Quit it normally first so the Extensions page opens in the profile named by --chrome-profile."
        )
    extension_directory = Path(__file__).resolve().parent / "extension"
    if chrome_extension_id(extension_directory) != EXTENSION_ID:
        raise MigrationError("The bundled importer extension is missing or has been altered.")
    command = [str(chrome_app), f"--profile-directory={arguments.chrome_profile}", "chrome://extensions/"]
    if custom_data_dir:
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data_root, 0o700)
        command.insert(1, f"--user-data-dir={data_root}")
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        subprocess.Popen(["open", str(extension_directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise MigrationError(f"Unable to open Chrome's Extensions page: {error}") from error
    print("Chrome's Extensions page and the extension folder are open.")
    print("In the target Chrome profile, turn on Developer mode, click Load unpacked, and choose the opened extension folder.")
    print("Confirm that 'Atlas Tab Groups Importer' appears, then quit Chrome normally and run migrate/import with --apply.")
    return 0


def command_profiles(arguments: argparse.Namespace) -> int:
    data_root = Path(arguments.chrome_user_data_dir).expanduser() if arguments.chrome_user_data_dir else DEFAULT_CHROME_DATA_ROOT
    if not data_root.is_dir():
        raise MigrationError(f"Chrome data folder was not found: {data_root}")
    profiles = [
        child.name
        for child in sorted(data_root.iterdir())
        if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile "))
    ]
    if not profiles:
        raise MigrationError("No Chrome profile directories were found")
    print("Chrome profile directories:")
    for profile in profiles:
        print(f"  {profile}")
    return 0


def add_atlas_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--atlas-root", default=str(DEFAULT_ATLAS_ROOT), help="Atlas Application Support folder")
    parser.add_argument("--atlas-user-dir", help="Explicit Atlas user workspace folder, when more than one exists")
    parser.add_argument("--strip-query", action="store_true", help="Remove URL query strings and fragments from the export")


def add_chrome_arguments(parser: argparse.ArgumentParser, *, include_apply: bool = True) -> None:
    parser.add_argument("--chrome-profile", default="Default", help="Chrome profile directory to open (default: Default)")
    parser.add_argument("--chrome-app", default=str(DEFAULT_CHROME_APP), help="Google Chrome executable")
    parser.add_argument(
        "--chrome-user-data-dir",
        help="Optional separate Chrome user-data directory for an isolated test import; it is not copied from Atlas",
    )
    if include_apply:
        parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the local Chrome importer (default: 600)")
        parser.add_argument("--apply", action="store_true", help="Explicitly authorize creation of new Chrome windows and tabs")
        parser.add_argument(
            "--use-running-chrome",
            action="store_true",
            help="Open the installed importer in the currently running Chrome profile without closing or restarting Chrome",
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Export Atlas's persistent tab-group configuration and recreate it in Chrome without copying browser credentials."
    )
    subcommands = result.add_subparsers(dest="command", required=True)

    inspect = subcommands.add_parser("inspect", help="Read and validate Atlas's persistent tab-group records without writing files")
    add_atlas_arguments(inspect)
    inspect.set_defaults(handler=command_inspect)

    export = subcommands.add_parser("export", help="Create a private Atlas tab-group JSON export and safe backup")
    add_atlas_arguments(export)
    export.add_argument("--output-dir", help="New/empty directory for this migration run")
    export.add_argument("--dry-run", action="store_true", help="Validate only; do not write files")
    export.add_argument("--allow-live-atlas", action="store_true", help="Allow a best-effort export while Atlas is running")
    export.set_defaults(handler=command_export)

    importer = subcommands.add_parser("import", help="Validate an export or recreate it in Chrome with the installed local importer extension")
    importer.add_argument("--export-file", required=True, help="atlas-tab-groups.json created by export")
    add_chrome_arguments(importer)
    importer.add_argument("--dry-run", action="store_true", help="Validate only; do not start Chrome")
    importer.set_defaults(handler=command_import)

    migrate = subcommands.add_parser("migrate", help="Export Atlas then recreate the exported topology in Chrome")
    add_atlas_arguments(migrate)
    add_chrome_arguments(migrate)
    migrate.add_argument("--output-dir", help="New/empty directory for this migration run")
    migrate.add_argument("--dry-run", action="store_true", help="Validate Atlas and Chrome import data only")
    migrate.add_argument("--allow-live-atlas", action="store_true", help="Allow a best-effort export while Atlas is running")
    migrate.set_defaults(handler=command_migrate)

    install = subcommands.add_parser(
        "install",
        help="Open Chrome's supported one-time UI for installing the local importer extension",
    )
    add_chrome_arguments(install, include_apply=False)
    install.set_defaults(handler=command_install)

    profiles = subcommands.add_parser("profiles", help="List Chrome profile-directory values")
    profiles.add_argument("--chrome-user-data-dir", help="Optional Chrome user-data root to inspect")
    profiles.set_defaults(handler=command_profiles)
    return result


def main() -> int:
    arguments = parser().parse_args()
    if getattr(arguments, "timeout", 1) <= 0:
        raise MigrationError("--timeout must be greater than zero")
    return arguments.handler(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
