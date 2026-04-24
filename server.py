#!/usr/bin/env python3
"""
RadioBook — Audiobook player for Raspberry Pi Zero
Main server: Flask API + static file serving
"""

import os
import tempfile
import json
import time
import threading
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from player import AudioPlayer
from amp_monitor import AmpMonitor
from config import CONFIG

_tmp_dir = CONFIG['tmp_dir']
os.makedirs(_tmp_dir, exist_ok=True)
tempfile.tempdir = _tmp_dir

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
log = logging.getLogger('radiobook')

# Suppress /api/status spam in logs
class StatusFilter(logging.Filter):
    def filter(self, record):
        return '/api/status' not in record.getMessage()

logging.getLogger('werkzeug').addFilter(StatusFilter())

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB max upload

# --- State persistence ---

STATE_FILE = CONFIG['state_file']
BOOKS_DIR = Path(CONFIG['books_dir'])
BOOKS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_STATIONS = [
    {'id':'classical_fm','name':'Classic FM','url':'https://media-ice.musicradio.com/ClassicFMMP3','genre':'Classical'},
    {'id':'jazz24','name':'Jazz24','url':'https://live.wostreaming.net/direct/ppm-jazz24mp3-ibc1','genre':'Jazz'},
    {'id':'soma_groovesalad','name':'Groove Salad','url':'https://ice2.somafm.com/groovesalad-128-mp3','genre':'Ambient'},
    {'id':'soma_dronezone','name':'Drone Zone','url':'https://ice2.somafm.com/dronezone-128-mp3','genre':'Ambient'},
    {'id':'soma_defcon','name':'DEF CON Radio','url':'https://ice2.somafm.com/defcon-128-mp3','genre':'Synth'},
    {'id':'radio_record','name':'Radio Record','url':'https://radiorecord.hostingradio.ru/rr_main96.aacp','genre':'Dance'},
    {'id':'europa_plus','name':'Europa Plus','url':'https://ep128.hostingradio.ru:8030/ep128','genre':'Pop'},
    {'id':'bbc_ws','name':'BBC World','url':'https://stream.live.vc.bbcmedia.co.uk/bbc_world_service','genre':'News'},
    {'id':'lofi','name':'Lofi Hip Hop','url':'https://ice2.somafm.com/lush-128-mp3','genre':'Lofi'},
    {'id':'swiss_classic','name':'Swiss Classic','url':'https://stream.srg-ssr.ch/m/rsc_de/mp3_128','genre':'Classical'},
]

ALLOWED_EXTENSIONS = {'.mp3', '.m4a', '.m4b', '.ogg', '.opus', '.flac', '.wav', '.aac', '.wma'}


# --- radio-browser.info integration ---
# Public open catalog of ~50k internet radio stations. No auth required.
# https://api.radio-browser.info/
import urllib.request
import urllib.parse
import urllib.error

RB_USER_AGENT = 'RadioBook/1.0'

# Official public mirrors per https://api.radio-browser.info/ docs.
# de1 is no longer active as of 2025; nl1 and de2 are currently live.
RB_SEED_MIRRORS = [
    'de2.api.radio-browser.info',
    'nl1.api.radio-browser.info',
]
RB_DISCOVERY_NAME = 'all.api.radio-browser.info'

_rb_mirror_cache = None
_rb_mirrors_list = None
_rb_mirrors_refreshed_at = 0.0
_rb_lock = threading.Lock()

# Use requests if available — it plays much better inside threaded Flask
# than stdlib urllib, and we can force IPv4 cleanly.
try:
    import requests as _requests
    _HAVE_REQUESTS = True
except ImportError:
    _HAVE_REQUESTS = False
    log.warning('python `requests` not installed; falling back to urllib. '
                'Install with: pip install requests')


def _rb_resolve_ipv4(host, timeout=5):
    """Resolve host to list of IPv4 addresses, or [] on failure.
    Uses a thread to enforce a timeout — socket.getaddrinfo has no
    native timeout and can hang inside threaded Flask on ARM."""
    import socket as _s
    result = []
    def _do():
        nonlocal result
        try:
            infos = _s.getaddrinfo(host, 443, family=_s.AF_INET, type=_s.SOCK_STREAM)
            result = sorted({ai[4][0] for ai in infos})
        except Exception:
            pass
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning(f'DNS(v4) resolve timeout for {host} ({timeout}s)')
        return []
    return result


def _rb_discover_mirrors():
    """Discover live mirrors dynamically via all.api.radio-browser.info.
    Falls back to seed list."""
    import socket as _s
    names = []
    seen = set()
    try:
        ips = _rb_resolve_ipv4(RB_DISCOVERY_NAME, timeout=5)
        for ip in ips:
            try:
                # Reverse DNS also can hang — use short default timeout
                old_to = _s.getdefaulttimeout()
                _s.setdefaulttimeout(3)
                try:
                    rname, _, _ = _s.gethostbyaddr(ip)
                finally:
                    _s.setdefaulttimeout(old_to)
                if rname and rname.endswith('radio-browser.info') and rname not in seen:
                    seen.add(rname)
                    names.append(rname)
            except Exception:
                pass
        if names:
            log.info(f'radio-browser: discovered {len(names)} mirror(s): {", ".join(names)}')
    except Exception as e:
        log.warning(f'radio-browser: discovery failed: {e}')

    for name in RB_SEED_MIRRORS:
        if name in seen:
            continue
        if _rb_resolve_ipv4(name, timeout=3):
            seen.add(name)
            names.append(name)

    return names or list(RB_SEED_MIRRORS)


def _rb_get_mirrors(max_age=600):
    global _rb_mirrors_list, _rb_mirrors_refreshed_at
    with _rb_lock:
        age = time.time() - _rb_mirrors_refreshed_at
        if _rb_mirrors_list and age < max_age:
            return list(_rb_mirrors_list)
    fresh = _rb_discover_mirrors()
    with _rb_lock:
        _rb_mirrors_list = fresh
        _rb_mirrors_refreshed_at = time.time()
    return list(fresh)


def _rb_proxy_dict():
    """Return requests-style proxies dict, or None.

    Only uses RADIOBOOK_PROXY env var (explicit). Streams are a separate
    knob via 'proxy' flag per station.
    """
    proxy = os.environ.get('RADIOBOOK_PROXY', '').strip()
    if not proxy:
        return None
    if '://' not in proxy:
        proxy = 'http://' + proxy
    return {'http': proxy, 'https': proxy}


def _rb_request_one(host, path, timeout):
    """Make one HTTPS GET to host. Forces IPv4 to avoid IPv6 hangs on ARM SBCs.
    Returns parsed JSON on success, raises on failure."""
    headers = {
        'User-Agent': RB_USER_AGENT,
        'Accept': 'application/json',
        'Connection': 'close',  # don't pool — each mirror call is independent
    }
    if _HAVE_REQUESTS:
        # Force IPv4: resolve manually, connect by IP with Host/SNI override
        ips = _rb_resolve_ipv4(host, timeout=4)
        if not ips:
            raise RuntimeError(f'Cannot resolve {host} to IPv4')
        ip = ips[0]
        # Connect to IP but keep Host header for virtual hosting and SNI
        url = f'https://{ip}{path}'
        headers['Host'] = host
        try:
            r = _requests.get(
                url,
                headers=headers,
                timeout=(5, timeout),
                proxies=_rb_proxy_dict(),
                verify=True,
            )
        except _requests.exceptions.SSLError:
            # SNI mismatch when connecting by IP — fall back to hostname
            url = f'https://{host}{path}'
            headers.pop('Host', None)
            r = _requests.get(
                url,
                headers=headers,
                timeout=(5, timeout),
                proxies=_rb_proxy_dict(),
            )
        r.raise_for_status()
        return r.json()
    # Fallback: urllib (no IPv4 forcing — best-effort)
    url = f'https://{host}{path}'
    pd = _rb_proxy_dict()
    if pd:
        handler = urllib.request.ProxyHandler(pd)
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _rb_request(path, timeout=8, retries=2):
    """GET radio-browser API with mirror failover + per-mirror retries.

    On slow/weak TLS handshake (common on ARM SBCs), the server may drop the
    connection. We retry each mirror a couple of times with short backoff
    before moving on to the next.
    """
    global _rb_mirror_cache
    mirrors = _rb_get_mirrors()
    if _rb_mirror_cache and _rb_mirror_cache in mirrors:
        mirrors.remove(_rb_mirror_cache)
        mirrors.insert(0, _rb_mirror_cache)

    last_err = None
    for host in mirrors:
        for attempt in range(1, retries + 1):
            try:
                data = _rb_request_one(host, path, timeout)
                with _rb_lock:
                    _rb_mirror_cache = host
                if attempt > 1:
                    log.info(f'radio-browser {host}: succeeded on attempt {attempt}')
                return data
            except Exception as e:
                last_err = e
                log.warning(f'radio-browser {host} (try {attempt}/{retries}): {e}')
                if attempt < retries:
                    time.sleep(0.4 * attempt)  # 0.4s, 0.8s
                continue
    raise RuntimeError(f'All radio-browser mirrors failed (last: {last_err})')


def _rb_click_track(stationuuid):
    """Fire-and-forget click tracking (fair-use etiquette)."""
    try:
        _rb_request(f'/json/url/{stationuuid}', timeout=5)
    except Exception as e:
        log.debug(f'rb click-track skipped: {e}')


# Log proxy config at startup
_proxy_env = os.environ.get('RADIOBOOK_PROXY') or os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
if _proxy_env:
    log.info(f'HTTP proxy for radio-browser: {_proxy_env}')


def _normalize_proxy_url(raw: str) -> str:
    """Normalize proxy URL: '127.0.0.1:3128' -> 'http://127.0.0.1:3128'."""
    if not raw:
        return ''
    raw = raw.strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'http://' + raw
    return raw


STREAM_PROXY = _normalize_proxy_url(CONFIG.get('stream_proxy', ''))
if STREAM_PROXY:
    log.info(f'Stream proxy configured: {STREAM_PROXY}')


def load_state():
    """Load persistent state from JSON file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                s = json.load(f)
                # Ensure new fields exist on old state files
                s.setdefault('hidden_stations', [])
                s.setdefault('radio_stations', [])
                s.setdefault('current_radio', None)
                s.setdefault('books', {})
                s.setdefault('current_book', None)
                s.setdefault('book_order', [])
                return s
        except Exception as e:
            log.error(f"Failed to load state: {e}")
    return {
        'current_book': None,
        'books': {},
        'book_order': [],  # ordered list of book IDs for shelf display & autoplay
        'radio_stations': [],
        'current_radio': None,
        'hidden_stations': [],  # ids of default stations the user removed
    }


def save_state(state):
    """Save state to JSON file atomically."""
    tmp = STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"Failed to save state: {e}")


state = load_state()
player = AudioPlayer()
amp_monitor = None

# --- Periodic position saver ---

_last_player_alive = False

def position_saver():
    """Save current playback position every 5 seconds."""
    global _last_player_alive
    while True:
        time.sleep(5)
        try:
            alive = player.is_playing() or player.is_paused()
            if player.is_playing() and not player.is_stream() and state['current_book']:
                book_id = state['current_book']
                if book_id in state['books']:
                    pos = player.get_position()
                    file_idx = player.get_current_file_index()
                    if pos is not None:
                        state['books'][book_id]['position'] = pos
                        state['books'][book_id]['current_file_index'] = file_idx
                        save_state(state)

            # Detect end-of-book: was playing a book, now player is dead
            if _last_player_alive and not alive and not player.is_stream() and state['current_book']:
                book_id = state['current_book']
                if book_id in state['books']:
                    book = state['books'][book_id]
                    fidx = book.get('current_file_index', 0)
                    last_idx = len(book['files']) - 1
                    # If we were on the last file, book is finished
                    if fidx >= last_idx:
                        book['current_file_index'] = 0
                        book['position'] = 0.0
                        save_state(state)
                        log.info(f"Book finished, reset to start: {book['title']}")
                        # Auto-play next book in shelf order
                        order = state.get('book_order', [])
                        if book_id in order:
                            idx = order.index(book_id)
                            if idx + 1 < len(order):
                                next_id = order[idx + 1]
                                if next_id in state['books']:
                                    next_book = state['books'][next_id]
                                    nfiles = [str(BOOKS_DIR / next_id / f) for f in next_book['files']]
                                    nfidx = next_book.get('current_file_index', 0)
                                    npos = next_book.get('position', 0.0)
                                    state['current_book'] = next_id
                                    save_state(state)
                                    player.play(nfiles, nfidx, npos)
                                    log.info(f"Autoplay next: {next_book['title']}")
                            else:
                                log.info("Last book on shelf — nothing to autoplay")

            _last_player_alive = alive
        except Exception as e:
            log.error(f"Position saver error: {e}")


saver_thread = threading.Thread(target=position_saver, daemon=True)
saver_thread.start()

# --- Amplifier state callback ---

def on_amp_state_change(amp_on: bool):
    """Called when amplifier is turned on/off."""
    log.info(f"Amplifier state changed: {'ON' if amp_on else 'OFF'}")
    if amp_on:
        # Resume what was last playing
        if state.get('current_radio'):
            # Resume radio
            station = _resolve_station(state['current_radio'])
            if station:
                proxy_url = STREAM_PROXY if _station_uses_proxy(station) else None
                player.play_stream(station['url'], proxy=proxy_url)
                log.info(f"Resumed radio: {station['name']}")
        else:
            # Resume book
            book_id = state['current_book']
            if book_id and book_id in state['books']:
                book = state['books'][book_id]
                files = [str(BOOKS_DIR / book_id / f) for f in book['files']]
                file_idx = book.get('current_file_index', 0)
                position = book.get('position', 0.0)
                # Rewind 5s for easier listening after resuming
                if position > 5:
                    position = max(0, position - 5)
                player.play(files, file_idx, position)
                log.info(f"Resumed: {book['title']} at {position:.1f}s (file {file_idx})")
    else:
        # Pause and save position
        if player.is_playing():
            if not player.is_stream() and state['current_book']:
                pos = player.get_position()
                file_idx = player.get_current_file_index()
                if state['current_book'] in state['books'] and pos is not None:
                    state['books'][state['current_book']]['position'] = pos
                    state['books'][state['current_book']]['current_file_index'] = file_idx
                    save_state(state)
            player.pause()
            log.info("Paused and saved position")


# --- Static files ---

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


# --- API: Books ---

@app.route('/api/books', methods=['GET'])
def get_books():
    """List all books in shelf order (book_order)."""
    # Build ordered list: book_order first, then any books not in order yet
    order = state.get('book_order', [])
    all_ids = set(state['books'].keys())
    ordered_ids = [bid for bid in order if bid in all_ids]
    # Append any books not yet in order (newly scanned, etc.) sorted by added time
    missing = all_ids - set(ordered_ids)
    if missing:
        missing_sorted = sorted(missing,
            key=lambda bid: state['books'][bid].get('added', 0))
        ordered_ids.extend(missing_sorted)
        # Persist the complete order
        state['book_order'] = ordered_ids
        save_state(state)

    books = []
    for book_id in ordered_ids:
        book = state['books'][book_id]
        try:
            total_duration = book.get('total_duration', 0)
            current_pos = book.get('position', 0)
            file_idx = book.get('current_file_index', 0)
            files_count = len(book.get('files', []))

            # Approximate total listened time:
            # assume average file length, count previous files as fully listened
            if files_count > 0 and total_duration > 0:
                avg_file_dur = total_duration / files_count
                total_listened = file_idx * avg_file_dur + current_pos
                progress_pct = min(100, (total_listened / total_duration) * 100)
            else:
                total_listened = 0
                progress_pct = 0

            books.append({
                'id': book_id,
                'title': book.get('title', book_id),
                'files_count': files_count,
                'current_file_index': file_idx,
                'position': current_pos,
                'total_listened': total_listened,
                'progress_pct': progress_pct,
                'total_duration': total_duration,
                'added': book.get('added', 0),
                'is_current': book_id == state['current_book'],
                'cover': f'/api/books/{book_id}/cover' if book.get('has_cover') else None
            })
        except Exception as e:
            log.error(f"Error reading book {book_id}: {e}")
    # Order is already correct from book_order — don't re-sort
    return jsonify(books)


@app.route('/api/books/reorder', methods=['POST'])
def reorder_books():
    """Set book shelf order. Body: {"order": ["id1", "id2", ...]}"""
    data = request.get_json() or {}
    new_order = data.get('order', [])
    if not isinstance(new_order, list):
        return jsonify({'error': 'order must be a list'}), 400
    # Validate: only keep IDs that actually exist
    valid = [bid for bid in new_order if bid in state['books']]
    # Append any books not mentioned
    mentioned = set(valid)
    for bid in state['books']:
        if bid not in mentioned:
            valid.append(bid)
    state['book_order'] = valid
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/books', methods=['POST'])
def upload_book():
    """Upload a new book (multipart form with audio files)."""
    title = request.form.get('title', '').strip()
    if not title:
        return jsonify({'error': 'Title is required'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    # Generate book ID
    book_id = secure_filename(title).lower() or f"book_{int(time.time())}"
    # Ensure unique
    base_id = book_id
    counter = 1
    while book_id in state['books']:
        book_id = f"{base_id}_{counter}"
        counter += 1

    book_dir = BOOKS_DIR / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    try:
        saved_files = []
        used_names = set()
        for idx, f in enumerate(files):
            original = f.filename or ''
            ext = Path(original).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                continue

            # Transliterate Cyrillic and sanitize
            filename = _safe_filename(original, fallback_idx=idx + 1)

            # Ensure uniqueness after sanitization
            base = filename[:-len(ext)] if filename.endswith(ext) else filename
            final = filename
            n = 1
            while final in used_names or (book_dir / final).exists():
                final = f"{base}_{n}{ext}"
                n += 1
            used_names.add(final)

            filepath = book_dir / final
            f.save(str(filepath))
            saved_files.append(final)
            log.info(f"Saved: {original!r} -> {final}")

        if not saved_files:
            book_dir.rmdir()
            return jsonify({'error': 'No valid audio files'}), 400

        # Sort files naturally
        saved_files.sort()

        # Register book immediately with duration=0; probe in background
        state['books'][book_id] = {
            'title': title,
            'files': saved_files,
            'position': 0.0,
            'current_file_index': 0,
            'total_duration': 0,
            'added': time.time(),
            'has_cover': False,
            'probing': True,  # UI can show "calculating duration..."
        }

        if state['current_book'] is None:
            state['current_book'] = book_id

        # Add to shelf order
        state.setdefault('book_order', []).append(book_id)

        save_state(state)
        log.info(f"Book added: {title} ({len(saved_files)} files); probing in background")

        # Probe durations in background
        def _probe_book_durations(bid, bdir, fnames):
            log.info(f"Probing durations for {bid}: {len(fnames)} files")
            total = 0
            for fname in fnames:
                dur = _get_duration(str(bdir / fname))
                total += dur
            if bid in state['books']:
                state['books'][bid]['total_duration'] = total
                state['books'][bid]['probing'] = False
                save_state(state)
                log.info(f"Probed {bid}: {total:.0f}s total")

        threading.Thread(
            target=_probe_book_durations,
            args=(book_id, book_dir, saved_files),
            daemon=True
        ).start()

        return jsonify({'id': book_id, 'title': title, 'files_count': len(saved_files)})

    except Exception as e:
        # Cleanup on failure
        import traceback
        log.error(f"Upload failed: {e}\n{traceback.format_exc()}")
        import shutil
        if book_dir.exists():
            shutil.rmtree(str(book_dir))
        if book_id in state['books']:
            del state['books'][book_id]
            save_state(state)
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


@app.route('/api/books/<book_id>/add_files', methods=['POST'])
def add_files_to_book(book_id):
    """Add more files to an existing book (for resuming interrupted uploads)."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files'}), 400

    book_dir = BOOKS_DIR / book_id
    book = state['books'][book_id]
    existing = set(book['files'])
    added = []
    used_names = set(existing)  # prevent collisions within this batch AND with existing

    for idx, f in enumerate(files):
        original = f.filename or ''
        ext = Path(original).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue

        # Transliterate Cyrillic and sanitize (same logic as upload_book)
        filename = _safe_filename(original, fallback_idx=len(existing) + idx + 1)

        # Ensure uniqueness against existing files and already-added-in-this-batch
        base = filename[:-len(ext)] if filename.endswith(ext) else filename
        final = filename
        n = 1
        while final in used_names or (book_dir / final).exists():
            final = f"{base}_{n}{ext}"
            n += 1
        used_names.add(final)

        filepath = book_dir / final
        f.save(str(filepath))
        added.append(final)
        log.info(f"Added to {book_id}: {original!r} -> {final}")

    if not added:
        return jsonify({'error': 'No new files added (duplicates?)'}), 400

    # Rebuild file list and re-sort
    all_files = list(existing) + added
    all_files.sort()
    book['files'] = all_files
    book['probing'] = True

    save_state(state)
    log.info(f"Added {len(added)} files to '{book['title']}'; re-probing in background")

    # Re-probe in background
    def _reprobe(bid, bdir, fnames):
        total = 0
        for fname in fnames:
            total += _get_duration(str(bdir / fname))
        if bid in state['books']:
            state['books'][bid]['total_duration'] = total
            state['books'][bid]['probing'] = False
            save_state(state)
            log.info(f"Re-probed {bid}: {total:.0f}s")

    threading.Thread(
        target=_reprobe,
        args=(book_id, book_dir, all_files),
        daemon=True
    ).start()

    return jsonify({'ok': True, 'added': len(added), 'total_files': len(all_files)})


@app.route('/api/scan', methods=['POST'])
def scan_books_dir():
    """Scan books directory for manually added books."""
    found = 0
    for subdir in BOOKS_DIR.iterdir():
        if not subdir.is_dir():
            continue
        book_id = subdir.name
        if book_id in state['books']:
            # Re-scan files for existing book (picks up manually added files)
            audio_files = sorted([
                f.name for f in subdir.iterdir()
                if f.suffix.lower() in ALLOWED_EXTENSIONS
            ])
            if audio_files and audio_files != state['books'][book_id]['files']:
                state['books'][book_id]['files'] = audio_files
                total_dur = sum(_get_duration(str(subdir / f)) for f in audio_files)
                state['books'][book_id]['total_duration'] = total_dur
                found += 1
            continue

        # New book found on disk
        audio_files = sorted([
            f.name for f in subdir.iterdir()
            if f.suffix.lower() in ALLOWED_EXTENSIONS
        ])
        if not audio_files:
            continue

        total_dur = sum(_get_duration(str(subdir / f)) for f in audio_files)
        title = book_id.replace('_', ' ').replace('-', ' ').title()
        has_cover = (subdir / 'cover.jpg').exists()

        state['books'][book_id] = {
            'title': title,
            'files': audio_files,
            'position': 0.0,
            'current_file_index': 0,
            'total_duration': total_dur,
            'added': time.time(),
            'has_cover': has_cover
        }
        # Add to shelf order
        state.setdefault('book_order', []).append(book_id)
        found += 1
        log.info(f"Scanned book: {title} ({len(audio_files)} files)")

    if found:
        save_state(state)
    return jsonify({'ok': True, 'found': found})


@app.route('/api/books/<book_id>/cover', methods=['POST'])
def upload_cover(book_id):
    """Upload cover image for a book."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404
    
    cover = request.files.get('cover')
    if not cover:
        return jsonify({'error': 'No cover file'}), 400

    book_dir = BOOKS_DIR / book_id
    cover_path = book_dir / 'cover.jpg'
    cover.save(str(cover_path))
    state['books'][book_id]['has_cover'] = True
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/books/<book_id>/cover', methods=['GET'])
def get_cover(book_id):
    """Get cover image."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404
    
    cover_path = BOOKS_DIR / book_id / 'cover.jpg'
    if cover_path.exists():
        return send_file(str(cover_path), mimetype='image/jpeg')
    return jsonify({'error': 'No cover'}), 404


@app.route('/api/books/<book_id>', methods=['DELETE'])
def delete_book(book_id):
    """Delete a book."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404

    # Stop if currently playing
    if state['current_book'] == book_id:
        player.stop()
        state['current_book'] = None

    # Remove files
    import shutil
    book_dir = BOOKS_DIR / book_id
    if book_dir.exists():
        shutil.rmtree(str(book_dir))

    del state['books'][book_id]
    # Remove from shelf order
    order = state.get('book_order', [])
    if book_id in order:
        order.remove(book_id)
        state['book_order'] = order
    save_state(state)
    log.info(f"Book deleted: {book_id}")

    return jsonify({'ok': True})


# --- API: Playback ---

@app.route('/api/select/<book_id>', methods=['POST'])
def select_book(book_id):
    """Select a book as current."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404

    # Save current position if playing or paused on a book (not radio)
    if (player.is_playing() or player.is_paused()) and not player.is_stream() and state['current_book']:
        pos = player.get_position()
        fidx = player.get_current_file_index()
        if state['current_book'] in state['books'] and pos is not None:
            state['books'][state['current_book']]['position'] = pos
            state['books'][state['current_book']]['current_file_index'] = fidx
    # Always stop — even if paused, so play() starts fresh with the new book
    if player.is_playing() or player.is_paused():
        player.stop()

    state['current_book'] = book_id
    state['current_radio'] = None
    save_state(state)
    log.info(f"Selected book: {state['books'][book_id]['title']}")

    return jsonify({'ok': True, 'book': book_id})


@app.route('/api/play', methods=['POST'])
def play():
    """Start/resume playback of current book."""
    # Block playback when amp is off
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    book_id = state['current_book']
    if not book_id or book_id not in state['books']:
        return jsonify({'error': 'No book selected'}), 400

    book = state['books'][book_id]
    files = [str(BOOKS_DIR / book_id / f) for f in book['files']]
    file_idx = book.get('current_file_index', 0)
    position = book.get('position', 0.0)

    # Clear radio mode
    state['current_radio'] = None

    if player.is_paused() and not player.is_stream():
        player.resume()
        # Rewind 5 seconds on resume for easier listening
        player.seek(-5)
    else:
        # Also rewind 5 sec when resuming from saved state (not start of book)
        if position > 5:
            position = max(0, position - 5)
        player.play(files, file_idx, position)

    save_state(state)
    return jsonify({'ok': True, 'position': position})


@app.route('/api/pause', methods=['POST'])
def pause():
    """Pause playback."""
    if player.is_playing():
        if not player.is_stream():
            pos = player.get_position()
            fidx = player.get_current_file_index()
            if state['current_book'] and state['current_book'] in state['books'] and pos is not None:
                state['books'][state['current_book']]['position'] = pos
                state['books'][state['current_book']]['current_file_index'] = fidx
                save_state(state)
        player.pause()
    return jsonify({'ok': True})


@app.route('/api/seek', methods=['POST'])
def seek():
    """Seek to position (in seconds relative, + or -)."""
    data = request.get_json() or {}
    offset = data.get('offset', 0)
    player.seek(offset)
    return jsonify({'ok': True})


@app.route('/api/next_file', methods=['POST'])
def next_file():
    """Skip to next file in book."""
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    book_id = state['current_book']
    if not book_id or book_id not in state['books']:
        return jsonify({'error': 'No book selected'}), 400

    book = state['books'][book_id]
    fidx = book.get('current_file_index', 0)
    if fidx >= len(book['files']) - 1:
        return jsonify({'ok': True, 'at_end': True})

    target = fidx + 1
    book['current_file_index'] = target
    book['position'] = 0.0
    save_state(state)

    # Always restart mpv from target file — playlist-next is unreliable
    # because mpv playlist only contains files from start_file_index onwards
    files = [str(BOOKS_DIR / book_id / f) for f in book['files']]
    player.play(files, target, 0.0)
    return jsonify({'ok': True, 'file_index': target})


@app.route('/api/prev_file', methods=['POST'])
def prev_file():
    """Go to previous file in book."""
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    book_id = state['current_book']
    if not book_id or book_id not in state['books']:
        return jsonify({'error': 'No book selected'}), 400

    book = state['books'][book_id]
    fidx = book.get('current_file_index', 0)
    target = max(0, fidx - 1)

    book['current_file_index'] = target
    book['position'] = 0.0
    save_state(state)

    # Always restart mpv from target file
    files = [str(BOOKS_DIR / book_id / f) for f in book['files']]
    player.play(files, target, 0.0)
    return jsonify({'ok': True, 'file_index': target})


@app.route('/api/status', methods=['GET'])
def status():
    """Get current playback status."""
    book_id = state['current_book']
    book_info = None
    is_radio = player.is_stream()

    if book_id and book_id in state['books']:
        book = state['books'][book_id]
        # Only query player for position if playing a BOOK (not radio)
        if not is_radio and (player.is_playing() or player.is_paused()):
            pos = player.get_position()
            fidx = player.get_current_file_index()
            file_dur = player.get_duration()
        else:
            pos = book.get('position', 0)
            fidx = book.get('current_file_index', 0)
            file_dur = None
        book_info = {
            'id': book_id,
            'title': book['title'],
            'current_file_index': fidx,
            'files_count': len(book['files']),
            'current_file': book['files'][fidx] if fidx < len(book['files']) else None,
            'position': pos or 0,
            'total_duration': book.get('total_duration', 0),
            'file_duration': file_dur or 0,
        }

    return jsonify({
        'playing': player.is_playing() and not is_radio,
        'paused': player.is_paused(),
        'book': book_info,
        'radio': state.get('current_radio'),
        'radio_playing': is_radio,
        'amp_on': amp_monitor.is_on() if amp_monitor else None
    })




@app.route('/api/books/<book_id>/rename', methods=['POST'])
def rename_book(book_id):
    """Rename a book."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'Title is required'}), 400
    state['books'][book_id]['title'] = title
    save_state(state)
    return jsonify({'ok': True})

@app.route('/api/reset_position/<book_id>', methods=['POST'])
def reset_position(book_id):
    """Reset book position to beginning."""
    if book_id not in state['books']:
        return jsonify({'error': 'Book not found'}), 404
    
    state['books'][book_id]['position'] = 0.0
    state['books'][book_id]['current_file_index'] = 0
    save_state(state)
    
    if state['current_book'] == book_id and (player.is_playing() or player.is_paused()):
        player.stop()
    
    return jsonify({'ok': True})



# --- API: Radio ---

# --- Radio helpers ---

def _resolve_station(sid):
    """Find a station by id in custom list or defaults. Returns dict or None."""
    for st in state.get('radio_stations', []):
        if st['id'] == sid:
            return st
    for st in DEFAULT_STATIONS:
        if st['id'] == sid:
            return st
    return None


def _station_uses_proxy(station):
    """Should this station be played through the stream proxy?"""
    return bool(station.get('proxy')) and bool(STREAM_PROXY)


@app.route('/api/radio/stations', methods=['GET'])
def get_radio_stations():
    hidden = set(state.get('hidden_stations', []))
    custom = state.get('radio_stations', [])
    custom_ids = {s['id'] for s in custom}
    all_st = []
    # Custom stations first, skip hidden
    for s in custom:
        if s['id'] in hidden:
            continue
        all_st.append(s)
    # Then defaults that aren't overridden by custom and aren't hidden
    for ds in DEFAULT_STATIONS:
        if ds['id'] in custom_ids or ds['id'] in hidden:
            continue
        all_st.append(dict(ds))
    cur = state.get('current_radio')
    for st in all_st:
        st['is_playing'] = (cur == st['id'] and player.is_playing())
    return jsonify(all_st)


@app.route('/api/radio/stations/hidden', methods=['GET'])
def list_hidden_stations():
    """Return the list of hidden default-station ids with names, so the UI
    can offer a 'restore' action."""
    hidden_ids = state.get('hidden_stations', [])
    out = []
    for sid in hidden_ids:
        for ds in DEFAULT_STATIONS:
            if ds['id'] == sid:
                out.append({'id': ds['id'], 'name': ds['name'], 'genre': ds.get('genre', '')})
                break
    return jsonify(out)


@app.route('/api/radio/stations/<sid>/restore', methods=['POST'])
def restore_hidden_station(sid):
    """Remove sid from hidden_stations list (un-hide a default station)."""
    hidden = state.get('hidden_stations', [])
    if sid in hidden:
        hidden.remove(sid)
        state['hidden_stations'] = hidden
        save_state(state)
    return jsonify({'ok': True})


@app.route('/api/radio/diag', methods=['GET'])
def radio_diag():
    """Diagnostic: show discovered mirrors, DNS and TCP reachability of each,
    from inside the Flask request thread. For troubleshooting only."""
    import socket as _s
    mirrors = _rb_get_mirrors()
    out = []
    for host in mirrors:
        entry = {'host': host}
        # Skip DNS test for IPs
        is_ip = all(c.isdigit() or c == '.' for c in host) or ':' in host
        if not is_ip:
            # IPv4-only DNS (what we actually use)
            t0 = time.time()
            ipv4 = _rb_resolve_ipv4(host, timeout=5)
            entry['dns_v4_ms'] = int((time.time() - t0) * 1000)
            entry['ips_v4'] = ipv4
            # Also try full DNS to detect IPv6-only resolution
            t0 = time.time()
            try:
                infos = _s.getaddrinfo(host, 443, type=_s.SOCK_STREAM)
                entry['dns_all_ms'] = int((time.time() - t0) * 1000)
                entry['ips_all'] = sorted({ai[4][0] for ai in infos})
            except Exception as e:
                entry['dns_all_ms'] = int((time.time() - t0) * 1000)
                entry['dns_error'] = f'{type(e).__name__}: {e}'
            if not ipv4:
                out.append(entry)
                continue
        # TCP connect test (IPv4)
        target = ipv4[0] if not is_ip and ipv4 else host
        t0 = time.time()
        try:
            sock = _s.create_connection((target, 443), timeout=5)
            sock.close()
            entry['tcp_ms'] = int((time.time() - t0) * 1000)
            entry['tcp'] = 'ok'
        except Exception as e:
            entry['tcp_ms'] = int((time.time() - t0) * 1000)
            entry['tcp_error'] = f'{type(e).__name__}: {e}'
        out.append(entry)
    # Catalog info
    cat_info = _catalog_info()
    return jsonify({
        'mirrors': out,
        'discovery_name': RB_DISCOVERY_NAME,
        'seed_mirrors': RB_SEED_MIRRORS,
        'cached_mirror': _rb_mirror_cache,
        'have_requests': _HAVE_REQUESTS,
        'catalog': cat_info,
        'catalog_path': CATALOG_PATH,
        'resolv_conf': _read_resolv_conf(),
    })


def _read_resolv_conf():
    try:
        with open('/etc/resolv.conf') as f:
            return f.read()
    except Exception as e:
        return f'<error: {e}>'


# --- Offline catalog (downloaded CSV snapshot) ---
# When the online API isn't reachable, we can search a pre-downloaded
# CSV dump of all stations. File lives at CATALOG_PATH, state tracks
# when it was downloaded.

CATALOG_PATH = os.path.join(os.path.dirname(STATE_FILE), 'catalog.csv')
_catalog_status = {'downloading': False, 'progress': 0, 'total': 0, 'error': None}
_catalog_status_lock = threading.Lock()


def _catalog_info():
    """Return {'exists': bool, 'size': int, 'mtime': float, 'path': str}"""
    if os.path.exists(CATALOG_PATH):
        st = os.stat(CATALOG_PATH)
        return {'exists': True, 'size': st.st_size, 'mtime': st.st_mtime, 'path': CATALOG_PATH}
    return {'exists': False, 'size': 0, 'mtime': 0, 'path': CATALOG_PATH}


def _catalog_search(name='', country='', tag='', language='', limit=30):
    """Search the local CSV catalog. Streams the file to keep memory low.

    Returns list of station dicts in the same shape as the online search.
    """
    import csv
    info = _catalog_info()
    if not info['exists']:
        raise RuntimeError('Каталог не загружен. Обновите его в окне поиска.')

    lname = name.lower()
    lcountry = country.lower()
    ltag = tag.lower()
    llang = language.lower()

    hits = []
    rows_read = 0
    rows_skipped_broken = 0
    rows_skipped_empty = 0
    with open(CATALOG_PATH, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        # Validate columns on first read
        if reader.fieldnames:
            required = {'name', 'url', 'lastcheckok'}
            missing = required - set(reader.fieldnames)
            if missing:
                raise RuntimeError(f'CSV: отсутствуют колонки {missing}. '
                                   f'Найдены: {reader.fieldnames[:5]}...')
        for row in reader:
            rows_read += 1
            # RB CSV marks broken stations with lastcheckok=0 → skip them
            if row.get('lastcheckok') == '0':
                rows_skipped_broken += 1
                continue
            n = (row.get('name') or '').strip()
            url = (row.get('url_resolved') or row.get('url') or '').strip()
            if not n or not url:
                rows_skipped_empty += 1
                continue
            if lname and lname not in n.lower():
                continue
            if lcountry:
                c = (row.get('country') or '').lower()
                cc = (row.get('countrycode') or '').lower()
                if lcountry not in c and lcountry not in cc:
                    continue
            if ltag and ltag not in (row.get('tags') or '').lower():
                continue
            if llang and llang not in (row.get('language') or '').lower():
                continue
            try:
                click = int(row.get('clickcount') or 0)
            except (TypeError, ValueError):
                click = 0
            try:
                bitrate = int(row.get('bitrate') or 0)
            except (TypeError, ValueError):
                bitrate = 0
            hits.append({
                'stationuuid': row.get('stationuuid', ''),
                'name': n,
                'url': url,
                'country': row.get('country', ''),
                'countrycode': row.get('countrycode', ''),
                'language': row.get('language', ''),
                'tags': row.get('tags', ''),
                'codec': row.get('codec', ''),
                'bitrate': bitrate,
                'favicon': row.get('favicon', ''),
                'clickcount': click,
            })
    log.debug(f'catalog search: read={rows_read}, broken={rows_skipped_broken}, '
              f'empty={rows_skipped_empty}, matched={len(hits)} '
              f'(query: name={name!r} country={country!r} tag={tag!r} lang={language!r})')
    if rows_read == 0:
        raise RuntimeError(f'CSV пуст или имеет неверный формат. Размер: {info["size"]} байт')
    # Sort by click count (popular first), then slice
    hits.sort(key=lambda x: x['clickcount'], reverse=True)
    return hits[:limit]


def _catalog_download_thread():
    """Background task: fetch the full CSV dump with per-mirror failover."""
    global _catalog_status
    path_tmp = CATALOG_PATH + '.tmp'
    # Keep existing catalog size — don't overwrite 30 MB with a truncated 1 MB
    old_size = 0
    if os.path.exists(CATALOG_PATH):
        old_size = os.path.getsize(CATALOG_PATH)
    mirrors = _rb_get_mirrors()
    last_err = None
    for host in mirrors:
        # Use /search with high limit — /csv/stations has a low default limit
        url = (f'https://{host}/csv/stations/search'
               f'?hidebroken=false&limit=100000&order=clickcount&reverse=true')
        log.info(f'catalog: downloading from {host}...')
        try:
            if _HAVE_REQUESTS:
                with _requests.get(url, stream=True, timeout=(10, 120),
                                   headers={'User-Agent': RB_USER_AGENT},
                                   proxies=_rb_proxy_dict()) as r:
                    r.raise_for_status()
                    total = int(r.headers.get('Content-Length') or 0)
                    with _catalog_status_lock:
                        _catalog_status['total'] = total
                        _catalog_status['progress'] = 0
                    done = 0
                    with open(path_tmp, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            with _catalog_status_lock:
                                _catalog_status['progress'] = done
            else:
                # urllib fallback
                pd = _rb_proxy_dict()
                if pd:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler(pd))
                else:
                    opener = urllib.request.build_opener()
                req = urllib.request.Request(url, headers={'User-Agent': RB_USER_AGENT})
                with opener.open(req, timeout=120) as resp:
                    total = int(resp.headers.get('Content-Length') or 0)
                    with _catalog_status_lock:
                        _catalog_status['total'] = total
                        _catalog_status['progress'] = 0
                    done = 0
                    with open(path_tmp, 'wb') as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            with _catalog_status_lock:
                                _catalog_status['progress'] = done
            # Sanity check: don't replace a large catalog with a tiny one
            min_ok = max(1_000_000, old_size // 2)  # at least 1 MB or half of old
            if done < min_ok:
                raise RuntimeError(
                    f'Downloaded file too small: {done} bytes '
                    f'(expected ≥{min_ok}). Server may have truncated response.')
            # atomic replace
            os.replace(path_tmp, CATALOG_PATH)
            log.info(f'catalog: downloaded {done} bytes from {host}')
            with _catalog_status_lock:
                _catalog_status['downloading'] = False
                _catalog_status['error'] = None
            return
        except Exception as e:
            last_err = e
            log.warning(f'catalog {host} failed: {e}')
            try:
                if os.path.exists(path_tmp):
                    os.remove(path_tmp)
            except OSError:
                pass
            continue

    # All mirrors failed
    with _catalog_status_lock:
        _catalog_status['downloading'] = False
        _catalog_status['error'] = f'All mirrors failed (last: {last_err})'
    log.error(f'catalog: all mirrors failed (last: {last_err})')


@app.route('/api/radio/catalog/status', methods=['GET'])
def catalog_status():
    info = _catalog_info()
    with _catalog_status_lock:
        status = dict(_catalog_status)
    return jsonify({
        'file': info,
        'downloading': status['downloading'],
        'progress': status['progress'],
        'total': status['total'],
        'error': status['error'],
    })


@app.route('/api/radio/catalog/download', methods=['POST'])
def catalog_download():
    """Kick off catalog download in background. Idempotent while running."""
    with _catalog_status_lock:
        if _catalog_status['downloading']:
            return jsonify({'ok': True, 'already_running': True})
        _catalog_status['downloading'] = True
        _catalog_status['progress'] = 0
        _catalog_status['total'] = 0
        _catalog_status['error'] = None
    threading.Thread(target=_catalog_download_thread, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/radio/catalog', methods=['DELETE'])
def catalog_delete():
    """Remove the local catalog file."""
    try:
        if os.path.exists(CATALOG_PATH):
            os.remove(CATALOG_PATH)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/radio/catalog/diag', methods=['GET'])
def catalog_diag():
    """Diagnostic: show catalog CSV health — columns, row counts, sample."""
    import csv
    info = _catalog_info()
    result = {'file': info, 'catalog_path': CATALOG_PATH}
    if not info['exists']:
        result['error'] = 'Файл не найден'
        return jsonify(result)
    try:
        with open(CATALOG_PATH, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            result['columns'] = reader.fieldnames
            result['column_count'] = len(reader.fieldnames) if reader.fieldnames else 0
            required = {'name', 'url', 'lastcheckok', 'url_resolved', 'stationuuid',
                        'country', 'tags', 'clickcount'}
            found = set(reader.fieldnames) if reader.fieldnames else set()
            result['missing_columns'] = sorted(required - found)
            total = 0
            ok = 0
            broken = 0
            no_url = 0
            sample = []
            for row in reader:
                total += 1
                if row.get('lastcheckok') == '0':
                    broken += 1
                    continue
                n = (row.get('name') or '').strip()
                url = (row.get('url_resolved') or row.get('url') or '').strip()
                if not n or not url:
                    no_url += 1
                    continue
                ok += 1
                if len(sample) < 3:
                    sample.append({'name': n, 'url': url[:80],
                                   'country': row.get('country', ''),
                                   'tags': row.get('tags', '')[:60]})
            result['rows_total'] = total
            result['rows_ok'] = ok
            result['rows_broken'] = broken
            result['rows_no_url'] = no_url
            result['sample'] = sample
    except Exception as e:
        result['error'] = str(e)
    return jsonify(result)


@app.route('/api/radio/search', methods=['GET'])
def radio_search():
    """Search radio-browser. Strategy:
      - If a local catalog exists, use it (fast, offline).
      - Otherwise, try online.
      - offline=1 → force local.
      - online=1  → force online (skip catalog even if present).
    """
    name = request.args.get('name', '').strip()
    country = request.args.get('country', '').strip()
    tag = request.args.get('tag', '').strip()
    language = request.args.get('language', '').strip()
    force_offline = request.args.get('offline') in ('1', 'true', 'yes')
    force_online = request.args.get('online') in ('1', 'true', 'yes')
    try:
        limit = max(1, min(int(request.args.get('limit', 30)), 100))
    except ValueError:
        limit = 30

    if not (name or country or tag or language):
        return jsonify({'error': 'Укажите хотя бы один критерий'}), 400

    have_catalog = _catalog_info()['exists']

    # Decide order of attempts
    # 1. If user forced something — respect that.
    # 2. If catalog exists — offline first (fast, doesn't waste 60s on timeouts).
    # 3. Else — online first.
    try_offline_first = force_offline or (have_catalog and not force_online)

    online_err = None

    def _try_offline():
        return _catalog_search(name=name, country=country, tag=tag,
                               language=language, limit=limit)

    def _try_online():
        params = {
            'limit': limit,
            'hidebroken': 'true',
            'order': 'clickcount',
            'reverse': 'true',
        }
        if name:     params['name'] = name
        if country:  params['country'] = country
        if tag:      params['tag'] = tag
        if language: params['language'] = language
        qs = urllib.parse.urlencode(params)
        data = _rb_request(f'/json/stations/search?{qs}')
        results = []
        for s in data:
            url = (s.get('url_resolved') or s.get('url') or '').strip()
            name_s = (s.get('name') or '').strip()
            if not url or not name_s:
                continue
            results.append({
                'stationuuid': s.get('stationuuid', ''),
                'name': name_s,
                'url': url,
                'country': s.get('country', ''),
                'countrycode': s.get('countrycode', ''),
                'language': s.get('language', ''),
                'tags': s.get('tags', ''),
                'codec': s.get('codec', ''),
                'bitrate': s.get('bitrate', 0),
                'favicon': s.get('favicon', ''),
                'clickcount': s.get('clickcount', 0),
            })
        return results

    if try_offline_first:
        try:
            results = _try_offline()
            info = _catalog_info()
            return jsonify({
                'source': 'offline',
                'catalog_mtime': info['mtime'],
                'results': results,
            })
        except Exception as e:
            offline_err = str(e)
            log.warning(f'offline search failed: {e}')
            if force_offline or have_catalog:
                # Catalog exists but parsing failed — return error immediately.
                # Don't cascade to online (90s timeout) for a local CSV problem.
                return jsonify({'error': f'Ошибка каталога: {e}'}), 502
            # No catalog at all → fall through to online

    # Online attempt
    try:
        results = _try_online()
        return jsonify({'source': 'online', 'results': results})
    except Exception as e:
        online_err = str(e)
        log.warning(f'online search failed: {e}')

    # Offline as last-ditch fallback (only if catalog exists but we started online)
    if have_catalog:
        try:
            results = _try_offline()
            info = _catalog_info()
            return jsonify({
                'source': 'offline',
                'catalog_mtime': info['mtime'],
                'online_error': online_err,
                'results': results,
            })
        except Exception as e:
            return jsonify({'error': f'Онлайн: {online_err}. Каталог: {e}'}), 502

    msg = f'Онлайн поиск: {online_err}'
    msg += '. Локальный каталог не загружен — скачайте через curl и scp.'
    return jsonify({'error': msg}), 502



@app.route('/api/radio/stations', methods=['POST'])
def add_radio_station():
    data = request.get_json() or {}
    name = data.get('name','').strip()
    url = data.get('url','').strip()
    genre = data.get('genre','').strip() or 'Custom'
    if not name or not url:
        return jsonify({'error':'Name and URL required'}), 400
    sid = secure_filename(name).lower() or ('st_'+str(int(time.time())))
    existing = {s['id'] for s in state.get('radio_stations',[])}
    base = sid; c = 1
    while sid in existing: sid = base+'_'+str(c); c += 1
    station = {'id':sid,'name':name,'url':url,'genre':genre,'custom':True}
    # Optional proxy flag — stream will be fetched via STREAM_PROXY
    if data.get('proxy'):
        station['proxy'] = True
    # Optional metadata from radio-browser (kept if provided, used for click-tracking)
    for k in ('stationuuid', 'country', 'countrycode', 'codec', 'bitrate', 'favicon'):
        v = data.get(k)
        if v:
            station[k] = v
    state.setdefault('radio_stations',[]).append(station)
    save_state(state)
    return jsonify(station)


@app.route('/api/radio/stations/<sid>/proxy', methods=['POST'])
def toggle_station_proxy(sid):
    """Toggle or set the 'proxy' flag on a station.

    For custom stations — flag is stored directly. For default stations —
    we materialize an override into radio_stations so the flag sticks.
    Body: {"proxy": true|false}
    """
    data = request.get_json() or {}
    want = bool(data.get('proxy', True))

    # Try custom first
    for s in state.get('radio_stations', []):
        if s['id'] == sid:
            if want:
                s['proxy'] = True
            else:
                s.pop('proxy', None)
            save_state(state)
            # If currently playing this station, re-start with new proxy setting
            if state.get('current_radio') == sid and player.is_playing():
                proxy_url = STREAM_PROXY if _station_uses_proxy(s) else None
                player.play_stream(s['url'], proxy=proxy_url)
            return jsonify({'ok': True, 'proxy': want})

    # Default station — materialize a copy in radio_stations with proxy flag
    for ds in DEFAULT_STATIONS:
        if ds['id'] == sid:
            override = dict(ds)
            override['custom'] = False  # mark as default-override, not user-added
            if want:
                override['proxy'] = True
            else:
                override.pop('proxy', None)
            state.setdefault('radio_stations', []).append(override)
            save_state(state)
            if state.get('current_radio') == sid and player.is_playing():
                proxy_url = STREAM_PROXY if _station_uses_proxy(override) else None
                player.play_stream(override['url'], proxy=proxy_url)
            return jsonify({'ok': True, 'proxy': want})

    return jsonify({'error': 'Not found'}), 404

@app.route('/api/radio/stations/<sid>', methods=['DELETE'])
def delete_radio_station(sid):
    # If currently playing this station, stop it
    if state.get('current_radio') == sid:
        player.stop()
        state['current_radio'] = None

    # Is it a custom station?
    customs = state.get('radio_stations', [])
    was_custom = any(s['id'] == sid for s in customs)

    if was_custom:
        state['radio_stations'] = [s for s in customs if s['id'] != sid]
    else:
        # Check whether it's a default one — then hide it
        is_default = any(d['id'] == sid for d in DEFAULT_STATIONS)
        if is_default:
            hidden = state.get('hidden_stations', [])
            if sid not in hidden:
                hidden.append(sid)
                state['hidden_stations'] = hidden
        else:
            return jsonify({'error': 'Not found'}), 404

    save_state(state)
    return jsonify({'ok': True})

@app.route('/api/radio/play/<sid>', methods=['POST'])
def play_radio(sid):
    # Block playback when amp is off
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    station = _resolve_station(sid)
    if not station:
        return jsonify({'error':'Not found'}), 404
    # Save book position before switching to radio
    if player.is_playing() and not player.is_stream() and state.get('current_book'):
        pos = player.get_position()
        fidx = player.get_current_file_index()
        bid = state['current_book']
        if bid in state['books'] and pos is not None:
            state['books'][bid]['position'] = pos
            state['books'][bid]['current_file_index'] = fidx

    proxy_url = STREAM_PROXY if _station_uses_proxy(station) else None
    player.play_stream(station['url'], proxy=proxy_url)
    state['current_radio'] = sid
    save_state(state)
    log.info("Radio: " + station['name'] + (' (proxy)' if proxy_url else ''))

    # Fire click tracking for radio-browser stations (fair-use etiquette, non-blocking)
    uuid = station.get('stationuuid')
    if uuid:
        threading.Thread(target=_rb_click_track, args=(uuid,), daemon=True).start()

    return jsonify({'ok':True,'station':station['name']})

@app.route('/api/radio/stop', methods=['POST'])
def stop_radio():
    player.stop()
    state['current_radio'] = None
    save_state(state)
    return jsonify({'ok':True})


# --- Radio preview (listen before adding) ---
# Holds what was playing before preview so we can restore it.
# Shape: None or {'kind':'radio','sid':...} or {'kind':'book','book_id':..,'pos':..,'fidx':..,'paused':bool}
_preview_backup = None
_preview_lock = threading.Lock()


@app.route('/api/radio/preview', methods=['POST'])
def preview_stream():
    """Play an arbitrary URL for listening (without adding it to the list).

    Body: {"url": "...", "proxy": true|false}

    Saves whatever was playing beforehand so /api/radio/preview/stop can
    restore it. Amp must be on.
    """
    global _preview_backup
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    use_proxy = bool(data.get('proxy'))
    if not url:
        return jsonify({'error': 'URL required'}), 400

    with _preview_lock:
        # If this is the first preview in a row, remember what was playing.
        # Subsequent previews just switch stream — backup isn't overwritten.
        if _preview_backup is None:
            if player.is_stream() and state.get('current_radio'):
                _preview_backup = {'kind': 'radio', 'sid': state['current_radio']}
            elif (player.is_playing() or player.is_paused()) and state.get('current_book'):
                bid = state['current_book']
                pos = player.get_position()
                fidx = player.get_current_file_index()
                if bid in state['books'] and pos is not None:
                    state['books'][bid]['position'] = pos
                    state['books'][bid]['current_file_index'] = fidx
                    save_state(state)
                _preview_backup = {
                    'kind': 'book',
                    'book_id': bid,
                    'pos': pos or state['books'].get(bid, {}).get('position', 0.0),
                    'fidx': fidx if fidx is not None else state['books'].get(bid, {}).get('current_file_index', 0),
                    'paused': player.is_paused(),
                }
            else:
                _preview_backup = {'kind': 'nothing'}

        proxy_url = STREAM_PROXY if (use_proxy and STREAM_PROXY) else None
        player.play_stream(url, proxy=proxy_url)
        # Don't touch state['current_radio'] — this is transient.
    log.info(f"Preview stream: {url}{' (proxy)' if proxy_url else ''}")
    return jsonify({'ok': True, 'proxy': bool(proxy_url)})


@app.route('/api/radio/preview/stop', methods=['POST'])
def preview_stop():
    """Stop preview and restore what was playing before."""
    global _preview_backup
    with _preview_lock:
        backup = _preview_backup
        _preview_backup = None

    player.stop()

    if not backup or backup.get('kind') == 'nothing':
        return jsonify({'ok': True})

    if backup['kind'] == 'radio':
        station = _resolve_station(backup['sid'])
        if station:
            proxy_url = STREAM_PROXY if _station_uses_proxy(station) else None
            player.play_stream(station['url'], proxy=proxy_url)
            state['current_radio'] = backup['sid']
            save_state(state)
        return jsonify({'ok': True, 'restored': 'radio'})

    if backup['kind'] == 'book':
        bid = backup['book_id']
        if bid and bid in state['books']:
            book = state['books'][bid]
            files = [str(BOOKS_DIR / bid / f) for f in book['files']]
            fidx = backup.get('fidx', 0) or 0
            pos = backup.get('pos', 0.0) or 0.0
            # Mild rewind so the transition feels natural
            if pos > 5:
                pos = max(0, pos - 5)
            player.play(files, fidx, pos)
            if backup.get('paused'):
                # was paused when preview started — re-pause
                time.sleep(0.3)
                player.pause()
        return jsonify({'ok': True, 'restored': 'book'})

    return jsonify({'ok': True})

# --- Helpers ---

def _safe_filename(original: str, fallback_idx: int = 1) -> str:
    """Sanitize filename, transliterating Cyrillic to preserve meaning.

    secure_filename() from werkzeug strips all non-ASCII, which turns
    "Часть 1.m4b" into "1.m4b" and "книга.m4b" into "m4b". When multiple
    files share the same sanitized name, they overwrite each other.
    """
    _CYR_MAP = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh',
        'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
        'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
        'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu',
        'я':'ya',
    }
    # Path(".m4b").suffix is '' because Python treats it as hidden file.
    # Extract ext manually from original lowercased string.
    low = original.lower()
    if '.' in low:
        ext = '.' + low.rsplit('.', 1)[1]
        stem_raw = low[:-len(ext)]
    else:
        ext = ''
        stem_raw = low
    translit = ''.join(_CYR_MAP.get(c, c) for c in stem_raw)
    safe_stem = secure_filename(translit)
    # If stem is empty or invalid, use fallback
    if not safe_stem or safe_stem in ('.', '..'):
        return f"track_{fallback_idx:03d}{ext}"
    return f"{safe_stem}{ext}"


def _get_duration(filepath):
    """Get audio file duration using ffprobe. Uses fast probe for large files."""
    import subprocess
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet',
             '-probesize', '5000000',        # 5MB probe only
             '-analyzeduration', '5000000',   # don't scan whole file
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, timeout=180  # 3 min per file
        )
        out = result.stdout.strip()
        return float(out) if out else 0.0
    except subprocess.TimeoutExpired:
        log.warning(f"ffprobe timeout on {filepath}")
        return 0.0
    except Exception as e:
        log.warning(f"ffprobe error on {filepath}: {e}")
        return 0.0


# --- Main ---

if __name__ == '__main__':
    # Start amplifier monitor if GPIO is available
    if CONFIG.get('gpio_enabled', False):
        try:
            amp_monitor = AmpMonitor(
                pin=CONFIG['amp_gpio_pin'],
                callback=on_amp_state_change,
                active_low=CONFIG.get('amp_active_low', True),
            )
            amp_monitor.start()
            log.info(f"Amplifier monitor started on GPIO {CONFIG['amp_gpio_pin']} (active_low={CONFIG.get('amp_active_low', True)})")
        except Exception as e:
            log.warning(f"GPIO not available: {e}. Running without amp detection.")
    else:
        log.info("GPIO disabled. Use web controls for playback.")

    log.info(f"RadioBook server starting on port {CONFIG['port']}")
    app.run(
        host='0.0.0.0',
        port=CONFIG['port'],
        debug=False,
        threaded=True
    )
