#!/usr/bin/env python3
"""
RadioBook — Audiobook player for Raspberry Pi Zero
Main server: Flask API + static file serving
"""

import os
import subprocess
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
# Music albums live alongside books, in a sibling directory
MUSIC_DIR = Path(CONFIG.get('music_dir') or (BOOKS_DIR.parent / 'music'))
RADIO_COVERS = BOOKS_DIR.parent / 'radio_covers'
MUSIC_DIR.mkdir(parents=True, exist_ok=True)

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
                s.setdefault('station_order', [])  # manual order of station ids
                s.setdefault('current_radio', None)
                s.setdefault('books', {})
                s.setdefault('current_book', None)
                s.setdefault('book_order', [])
                s.setdefault('music', {})
                s.setdefault('current_music', None)
                s.setdefault('music_order', [])
                s.setdefault('current_kind', 'book')  # 'book' or 'music' — what player is on
                s.setdefault('was_playing', False)  # True = audio should auto-resume (after amp-on / power-on)
                s.setdefault('network_mode', 'wifi')  # 'wifi' or 'hotspot' — restored on boot
                s.setdefault('ap_ssid', None)  # AP name override (None = from config)
                s.setdefault('ap_password', None)  # AP password override
                s.setdefault('ap_fallback', True)  # raise AP when wifi is unreachable
                return s
        except Exception as e:
            log.error(f"Failed to load state: {e}")
    return {
        'current_book': None,
        'books': {},
        'book_order': [],  # ordered list of book IDs for shelf display & autoplay
        'music': {},
        'current_music': None,
        'music_order': [],
        'current_kind': 'book',
        'was_playing': False,
        'network_mode': 'wifi',
        'ap_ssid': None,
        'ap_password': None,
        'ap_fallback': True,
        'radio_stations': [],
        'current_radio': None,
        'hidden_stations': [],  # ids of default stations the user removed
        'station_order': [],    # manual order of station ids
    }


def _collection(kind):
    """Return (items_dict, directory, order_key, current_key) for a kind."""
    if kind == 'music':
        return state['music'], MUSIC_DIR, 'music_order', 'current_music'
    return state['books'], BOOKS_DIR, 'book_order', 'current_book'


def _active_kind():
    return state.get('current_kind') or 'book'


def _prune_missing_files(item, item_dir, item_id):
    """Drop files that no longer exist on disk from the item's file list.
    Keeps state in sync when tracks are deleted manually from the folder.
    Returns True if anything changed."""
    files = item.get('files', [])
    if not files:
        return False
    base = item_dir / item_id
    present = [f for f in files if (base / f).exists()]
    if len(present) == len(files):
        return False
    removed = [f for f in files if f not in present]
    item['files'] = present
    # Fix shuffle order and current index
    if item.get('shuffle_order'):
        item['shuffle_order'] = [f for f in item['shuffle_order'] if f in present]
    cur = item.get('current_file_index', 0)
    if cur >= len(present):
        item['current_file_index'] = 0
        item['position'] = 0.0
    log.info(f"Pruned {len(removed)} missing file(s) from {item_id}: {removed}")
    save_state(state)
    return True


def _ordered_filenames(item):
    """Return the item's file names in playback order.
    For music albums with shuffle=True, returns a stored shuffled order
    (regenerated if missing or stale). Books/non-shuffle are unchanged."""
    files = list(item.get('files', []))
    if not item.get('shuffle'):
        return files
    order = item.get('shuffle_order')
    # Regenerate if missing or doesn't match current file set
    if not order or sorted(order) != sorted(files):
        import random
        order = files[:]
        random.shuffle(order)
        item['shuffle_order'] = order
    return order


def _ordered_paths(item, item_dir, item_id):
    """Absolute paths in playback order (honours shuffle)."""
    return [str(item_dir / item_id / f) for f in _ordered_filenames(item)]


def _active_item():
    """Resolve what the player is currently on.
    Returns dict: kind, items, dir, order_key, current_key, item_id, item (or None)."""
    kind = _active_kind()
    items, d, ok, ck = _collection(kind)
    iid = state.get(ck)
    return {
        'kind': kind, 'items': items, 'dir': d, 'order_key': ok,
        'current_key': ck, 'item_id': iid,
        'item': items.get(iid) if iid else None,
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
_last_file_idx = None


def _start_play(files, file_idx, position):
    """Start playback and sync the per-track index tracker so the position
    saver won't mistake this deliberate start for an auto-advance."""
    global _last_file_idx
    _last_file_idx = file_idx
    player.play(files, file_idx, position)

def position_saver():
    """Save current playback position every 5 seconds (book or music album).
    Position is tracked per *track*: when mpv advances to the next file inside
    a playlist, we reset the saved position to 0 so it can't leak across tracks."""
    global _last_player_alive, _last_file_idx
    while True:
        time.sleep(5)
        try:
            alive = player.is_playing() or player.is_paused()
            a = _active_item()
            if player.is_playing() and not player.is_stream() and a['item']:
                pos = player.get_position()
                file_idx = player.get_current_file_index()
                is_music = (a['kind'] == 'music')
                if file_idx is not None and file_idx != _last_file_idx:
                    # Track changed (mpv auto-advanced) — new track starts at 0
                    _last_file_idx = file_idx
                    a['item']['current_file_index'] = file_idx
                    a['item']['position'] = 0.0
                    save_state(state)
                elif is_music:
                    # Music: never persist a within-track position — only the
                    # track index. This makes position leaking impossible:
                    # each track always starts from 0.
                    if file_idx is not None and a['item'].get('current_file_index') != file_idx:
                        a['item']['current_file_index'] = file_idx
                    if a['item'].get('position'):
                        a['item']['position'] = 0.0
                        save_state(state)
                elif pos is not None:
                    a['item']['position'] = pos
                    a['item']['current_file_index'] = file_idx
                    save_state(state)

            # Detect end-of-item: was playing, now player is dead
            if _last_player_alive and not alive and not player.is_stream() and a['item']:
                item = a['item']
                fidx = item.get('current_file_index', 0)
                last_idx = len(item['files']) - 1
                if fidx >= last_idx:
                    item['current_file_index'] = 0
                    item['position'] = 0.0
                    save_state(state)
                    log.info(f"Finished, reset to start: {item.get('title')}")
                    if a['kind'] == 'music' and item.get('loop') and item.get('files'):
                        # Loop: restart this album instead of moving on.
                        # With shuffle on, deal a fresh order for each cycle.
                        if item.get('shuffle'):
                            import random
                            new_order = list(item.get('files', []))
                            random.shuffle(new_order)
                            item['shuffle_order'] = new_order
                        state['was_playing'] = True
                        save_state(state)
                        nfiles = _ordered_paths(item, a['dir'], a['item_id'])
                        _start_play(nfiles, 0, 0.0)
                        log.info(f"Loop: restarting album {item.get('title')!r}"
                                 + (" (new shuffle order)" if item.get('shuffle') else ""))
                    else:
                        # Auto-play next item in the same collection's order
                        order = state.get(a['order_key'], [])
                        iid = a['item_id']
                        if iid in order:
                            idx = order.index(iid)
                            if idx + 1 < len(order):
                                next_id = order[idx + 1]
                                if next_id in a['items']:
                                    nxt = a['items'][next_id]
                                    nfiles = _ordered_paths(nxt, a['dir'], next_id)
                                    nfidx = nxt.get('current_file_index', 0)
                                    npos = nxt.get('position', 0.0)
                                    if a['kind'] == 'music':
                                        nfidx = 0
                                        npos = 0.0
                                    state[a['current_key']] = next_id
                                    save_state(state)
                                    _start_play(nfiles, nfidx, npos)
                                    log.info(f"Autoplay next: {nxt.get('title')}")
                            else:
                                state['was_playing'] = False
                                save_state(state)
                                log.info("Last item — nothing to autoplay")

            _last_player_alive = alive
        except Exception as e:
            log.error(f"Position saver error: {e}")


saver_thread = threading.Thread(target=position_saver, daemon=True)
saver_thread.start()

# --- Amplifier state callback / resume logic ---

def resume_last_playback(reason=''):
    """Resume whatever was playing last: radio, book or music album.
    Returns True if something was started."""
    if state.get('current_radio'):
        station = _resolve_station(state['current_radio'])
        if station:
            proxy_url = STREAM_PROXY if _station_uses_proxy(station) else None
            player.play_stream(station['url'], proxy=proxy_url)
            log.info(f"Resumed radio: {station['name']} {reason}")
            return True
    else:
        a = _active_item()
        if a['item']:
            item = a['item']
            files = _ordered_paths(item, a['dir'], a['item_id'])
            file_idx = item.get('current_file_index', 0)
            position = item.get('position', 0.0)
            if a['kind'] == 'music':
                position = 0.0
            # Rewind 5s for easier listening after resuming
            if position > 5:
                position = max(0, position - 5)
            _start_play(files, file_idx, position)
            log.info(f"Resumed: {item.get('title')} at {position:.1f}s (file {file_idx}) {reason}")
            return True
    return False


def on_amp_state_change(amp_on: bool):
    """Called when amplifier is turned on/off."""
    log.info(f"Amplifier state changed: {'ON' if amp_on else 'OFF'}")
    if amp_on:
        # Resume only if something was actually interrupted (not manually paused/stopped)
        if state.get('was_playing'):
            resume_last_playback('(amp on)')
        else:
            log.info("Amp on: nothing to resume (was not playing)")
    else:
        # Pause and save position. Keep was_playing=True — it was *interrupted*,
        # so amp-on / next power-on should resume it.
        if player.is_playing():
            a = _active_item()
            if not player.is_stream() and a['item']:
                pos = player.get_position()
                file_idx = player.get_current_file_index()
                if pos is not None:
                    a['item']['position'] = pos
                    a['item']['current_file_index'] = file_idx
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
    # Dedupe order while preserving first occurrence — duplicates here would
    # produce duplicate book elements with same DOM id, breaking menu/rename/delete
    seen = set()
    ordered_ids = []
    for bid in order:
        if bid in all_ids and bid not in seen:
            ordered_ids.append(bid)
            seen.add(bid)
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


# === Music album endpoints (mirror books, separate collection) ===

@app.route('/api/music', methods=['GET'])
def get_music():
    """List all music albums in shelf order."""
    order = state.get('music_order', [])
    all_ids = set(state['music'].keys())
    seen = set()
    ordered_ids = []
    for aid in order:
        if aid in all_ids and aid not in seen:
            ordered_ids.append(aid); seen.add(aid)
    missing = all_ids - set(ordered_ids)
    if missing:
        ordered_ids.extend(sorted(missing, key=lambda a: state['music'][a].get('added', 0)))
        state['music_order'] = ordered_ids
        save_state(state)

    out = []
    for aid in ordered_ids:
        al = state['music'][aid]
        out.append({
            'id': aid,
            'title': al.get('title', aid),
            'artist': al.get('artist', ''),
            'files_count': len(al.get('files', [])),
            'current_file_index': al.get('current_file_index', 0),
            'position': al.get('position', 0),
            'total_duration': al.get('total_duration', 0),
            'added': al.get('added', 0),
            'is_current': aid == state['current_music'],
            'shuffle': al.get('shuffle', False),
            'loop': al.get('loop', False),
            'cover': f'/api/music/{aid}/cover' if al.get('has_cover') else None,
        })
    return jsonify(out)


@app.route('/api/music/reorder', methods=['POST'])
def reorder_music():
    data = request.get_json() or {}
    new_order = data.get('order', [])
    if not isinstance(new_order, list):
        return jsonify({'error': 'order must be a list'}), 400
    valid = [a for a in new_order if a in state['music']]
    mentioned = set(valid)
    for a in state['music']:
        if a not in mentioned:
            valid.append(a)
    state['music_order'] = valid
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/music/<album_id>', methods=['DELETE'])
def delete_music(album_id):
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    if state['current_music'] == album_id:
        player.stop()
        state['current_music'] = None
    import shutil
    d = MUSIC_DIR / album_id
    if d.exists():
        shutil.rmtree(str(d))
    del state['music'][album_id]
    order = state.get('music_order', [])
    if album_id in order:
        order.remove(album_id)
        state['music_order'] = order
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/music/<album_id>/rename', methods=['POST'])
def rename_music(album_id):
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    artist = data.get('artist', None)
    if not title and artist is None:
        return jsonify({'error': 'Nothing to update'}), 400
    if title:
        state['music'][album_id]['title'] = title
    if artist is not None:
        state['music'][album_id]['artist'] = artist.strip()
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/music/<album_id>/track/delete', methods=['POST'])
def delete_music_track(album_id):
    """Delete a single track from an album (by filename), incl. the file on disk."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    data = request.get_json(silent=True) or {}
    fname = data.get('file')
    album = state['music'][album_id]
    files = album.get('files', [])
    if not fname or fname not in files:
        return jsonify({'error': 'Track not found'}), 404

    was_current = (state.get('current_music') == album_id and _active_kind() == 'music')
    cur_idx = album.get('current_file_index', 0)
    ordered = _ordered_filenames(album)
    playing_name = ordered[cur_idx] if cur_idx < len(ordered) else None

    # Remove file from disk
    fpath = MUSIC_DIR / album_id / fname
    try:
        if fpath.exists():
            fpath.unlink()
    except Exception as e:
        log.error(f"Failed to delete track file {fpath}: {e}")
        return jsonify({'error': 'Не удалось удалить файл'}), 500

    # Update state
    album['files'] = [f for f in files if f != fname]
    if album.get('shuffle_order'):
        album['shuffle_order'] = [f for f in album['shuffle_order'] if f != fname]
    log.info(f"Deleted track {fname!r} from album {album_id}")

    # Recompute duration in the background
    def _reprobe():
        total = sum(_get_duration(str(MUSIC_DIR / album_id / fn))
                    for fn in album.get('files', []))
        if album_id in state['music']:
            state['music'][album_id]['total_duration'] = total
            save_state(state)
    threading.Thread(target=_reprobe, daemon=True).start()

    # If we deleted the track that's playing now, stop and reset to a safe index
    if not album.get('files'):
        album['current_file_index'] = 0
        album['position'] = 0.0
        if was_current and (player.is_playing() or player.is_paused()):
            player.stop()
    elif was_current and playing_name == fname:
        new_order = _ordered_filenames(album)
        new_idx = min(cur_idx, len(new_order) - 1)
        album['current_file_index'] = new_idx
        album['position'] = 0.0
        if player.is_playing() or player.is_paused():
            files_paths = _ordered_paths(album, MUSIC_DIR, album_id)
            _start_play(files_paths, new_idx, 0.0)
    else:
        # Keep current track playing; just fix the stored index to match
        new_order = _ordered_filenames(album)
        if playing_name and playing_name in new_order:
            album['current_file_index'] = new_order.index(playing_name)

    save_state(state)
    return jsonify({'ok': True, 'remaining': len(album.get('files', []))})


@app.route('/api/music/<album_id>/play_track', methods=['POST'])
def play_music_track(album_id):
    """Play one specific track of an album (used by the tracks list).
    Makes the album current and starts at that file, keeping the album's
    own order (shuffle included) so Next carries on normally."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    data = request.get_json(silent=True) or {}
    fname = data.get('file')
    album = state['music'][album_id]
    if not fname or fname not in album.get('files', []):
        return jsonify({'error': 'Track not found'}), 404

    # Save the position of whatever was playing before switching away
    if (player.is_playing() or player.is_paused()) and not player.is_stream():
        a = _active_item()
        if a['item']:
            pos = player.get_position()
            fidx = player.get_current_file_index()
            if pos is not None:
                a['item']['position'] = pos
                a['item']['current_file_index'] = fidx
    if player.is_playing() or player.is_paused():
        player.stop()

    state['current_music'] = album_id
    state['current_kind'] = 'music'
    state['current_radio'] = None

    ordered = _ordered_filenames(album)
    idx = ordered.index(fname) if fname in ordered else 0
    album['current_file_index'] = idx
    album['position'] = 0.0
    state['was_playing'] = True
    save_state(state)

    files = _ordered_paths(album, MUSIC_DIR, album_id)
    _start_play(files, idx, 0.0)
    log.info("Play track %r from album %s", fname, album_id)
    return jsonify({'ok': True, 'file': fname, 'index': idx})


@app.route('/api/music/<album_id>/tracks', methods=['GET'])
def list_music_tracks(album_id):
    """List tracks of an album (filenames in natural order) for the UI."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    album = state['music'][album_id]
    _prune_missing_files(album, MUSIC_DIR, album_id)
    playing = None
    if state.get('current_music') == album_id and _active_kind() == 'music':
        ordered = _ordered_filenames(album)
        cur = album.get('current_file_index', 0)
        if 0 <= cur < len(ordered):
            playing = ordered[cur]
    return jsonify({'ok': True, 'files': album.get('files', []),
                    'playing': playing,
                    'is_playing': bool(player.is_playing() and not player.is_stream()
                                       and state.get('current_music') == album_id
                                       and _active_kind() == 'music')})


@app.route('/api/music/<album_id>/shuffle', methods=['POST'])
def toggle_music_shuffle(album_id):
    """Toggle shuffle (random, no-repeat) playback for an album."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    album = state['music'][album_id]
    data = request.get_json(silent=True) or {}
    new_val = data.get('shuffle')
    if new_val is None:
        new_val = not album.get('shuffle', False)
    album['shuffle'] = bool(new_val)
    if album['shuffle']:
        # Build a fresh shuffled order starting from the current track
        import random
        files = list(album.get('files', []))
        cur_name = None
        # current_file_index points into the *previous* order; we only need a
        # fresh shuffle, so just reshuffle and reset to position 0 of new order
        random.shuffle(files)
        album['shuffle_order'] = files
    else:
        album.pop('shuffle_order', None)
    album['current_file_index'] = 0
    album['position'] = 0.0
    save_state(state)

    # If this album is playing right now, restart with the new order
    if state.get('current_music') == album_id and _active_kind() == 'music' \
            and (player.is_playing() or player.is_paused()):
        a = _active_item()
        files = _ordered_paths(album, a['dir'], album_id)
        _start_play(files, 0, 0.0)
        state['was_playing'] = True
        save_state(state)

    return jsonify({'ok': True, 'shuffle': album['shuffle']})


@app.route('/api/music/<album_id>/loop', methods=['POST'])
def toggle_music_loop(album_id):
    """Toggle looping for an album: when it ends, it starts over.
    With shuffle also on, every new cycle gets a freshly dealt order."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    album = state['music'][album_id]
    data = request.get_json(silent=True) or {}
    new_val = data.get('loop')
    if new_val is None:
        new_val = not album.get('loop', False)
    album['loop'] = bool(new_val)
    save_state(state)
    return jsonify({'ok': True, 'loop': album['loop']})


@app.route('/api/music/<album_id>/reset_position', methods=['POST'])
def reset_music_position(album_id):
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    state['music'][album_id]['position'] = 0.0
    state['music'][album_id]['current_file_index'] = 0
    save_state(state)
    if state['current_music'] == album_id and (player.is_playing() or player.is_paused()):
        player.stop()
    return jsonify({'ok': True})


@app.route('/api/music/<album_id>/add_files', methods=['POST'])
def add_files_to_music(album_id):
    """Add more tracks to an existing album (build your own compilation)."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files'}), 400

    album_dir = MUSIC_DIR / album_id
    album_dir.mkdir(parents=True, exist_ok=True)
    album = state['music'][album_id]
    existing = list(album.get('files', []))
    added = []
    used_names = set(existing)

    for idx, f in enumerate(files):
        original = f.filename or ''
        ext = Path(original).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        filename = _safe_filename(original, fallback_idx=len(existing) + idx + 1)
        base = filename[:-len(ext)] if filename.endswith(ext) else filename
        final = filename
        n = 1
        while final in used_names or (album_dir / final).exists():
            final = f"{base}_{n}{ext}"
            n += 1
        used_names.add(final)
        f.save(str(album_dir / final))
        added.append(final)
        log.info(f"Added to album {album_id}: {original!r} -> {final}")

    if not added:
        return jsonify({'error': 'No new files added (duplicates?)'}), 400

    all_files = sorted(existing + added)
    album['files'] = all_files
    if album.get('shuffle'):
        # Otherwise shuffle_order goes stale and the lazy regen in
        # _ordered_filenames reshuffles at an unpredictable moment
        import random
        new_order = all_files[:]
        random.shuffle(new_order)
        album['shuffle_order'] = new_order
    save_state(state)

    def _reprobe(aid, adir, fnames):
        total = sum(_get_duration(str(adir / fn)) for fn in fnames)
        if aid in state['music']:
            state['music'][aid]['total_duration'] = total
            save_state(state)
            log.info(f"Re-probed album {aid}: {total:.0f}s")

    threading.Thread(target=_reprobe, args=(album_id, album_dir, all_files),
                     daemon=True).start()

    return jsonify({'ok': True, 'added': len(added), 'total_files': len(all_files)})


@app.route('/api/music/<album_id>/cover', methods=['GET'])
def get_music_cover(album_id):
    cover = MUSIC_DIR / album_id / 'cover.jpg'
    if cover.exists():
        return send_file(str(cover), mimetype='image/jpeg')
    return jsonify({'error': 'No cover'}), 404


@app.route('/api/music/<album_id>/cover', methods=['POST'])
def set_music_cover(album_id):
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404
    f = request.files.get('cover')
    if not f:
        return jsonify({'error': 'No cover file'}), 400
    cover_path = MUSIC_DIR / album_id / 'cover.jpg'
    f.save(str(cover_path))
    state['music'][album_id]['has_cover'] = True
    save_state(state)
    return jsonify({'ok': True})


def _sync_album_files(aid, album, subdir):
    """Sync one album with what's actually on disk.

    Picks up files copied straight into the folder (scp/USB/network share) and
    drops entries whose file is gone. Only new files get probed with ffprobe —
    re-probing a 700-track album on every scan would take minutes.
    Returns (added_count, removed_count)."""
    on_disk = sorted([f.name for f in subdir.iterdir()
                      if f.suffix.lower() in ALLOWED_EXTENSIONS])
    known = list(album.get('files', []))
    added = [f for f in on_disk if f not in known]
    removed = [f for f in known if f not in on_disk]
    if not added and not removed:
        if not album.get('has_cover') and (subdir / 'cover.jpg').exists():
            album['has_cover'] = True
        return 0, 0

    # Remember what's playing so its index survives the reshuffle below
    playing_name = None
    if state.get('current_music') == aid and _active_kind() == 'music':
        ordered = _ordered_filenames(album)
        cur = album.get('current_file_index', 0)
        if 0 <= cur < len(ordered):
            playing_name = ordered[cur]

    album['files'] = on_disk

    if album.get('shuffle'):
        # Keep the established order, append the newcomers shuffled at the end
        import random
        order = [f for f in album.get('shuffle_order', []) if f in on_disk]
        fresh = [f for f in on_disk if f not in order]
        random.shuffle(fresh)
        album['shuffle_order'] = order + fresh

    if removed:
        # A vanished file's duration is unknown, so the total has to be redone
        album['total_duration'] = sum(
            _get_duration(str(subdir / fn)) for fn in on_disk)
    else:
        delta = sum(_get_duration(str(subdir / fn)) for fn in added)
        album['total_duration'] = album.get('total_duration', 0.0) + delta

    new_order = _ordered_filenames(album)
    if playing_name and playing_name in new_order:
        album['current_file_index'] = new_order.index(playing_name)
    elif album.get('current_file_index', 0) >= len(new_order):
        album['current_file_index'] = 0
        album['position'] = 0.0

    if not album.get('has_cover') and (subdir / 'cover.jpg').exists():
        album['has_cover'] = True

    log.info("Synced album %s: +%d -%d (now %d tracks)",
             aid, len(added), len(removed), len(on_disk))
    return len(added), len(removed)


@app.route('/api/music/scan', methods=['POST'])
def scan_music_dir():
    """Scan MUSIC_DIR: register new album folders AND sync files inside
    albums that are already known."""
    found = added = removed = 0
    for subdir in MUSIC_DIR.iterdir():
        if not subdir.is_dir():
            continue
        aid = subdir.name

        if aid in state['music']:
            a, r = _sync_album_files(aid, state['music'][aid], subdir)
            added += a
            removed += r
            continue

        audio = sorted([f.name for f in subdir.iterdir()
                        if f.suffix.lower() in ALLOWED_EXTENSIONS])
        if not audio:
            continue
        has_cover = (subdir / 'cover.jpg').exists()
        total = 0
        for fn in audio:
            total += _get_duration(str(subdir / fn))
        state['music'][aid] = {
            'title': aid.replace('_', ' ').title(),
            'artist': '',
            'files': audio,
            'position': 0.0,
            'current_file_index': 0,
            'total_duration': total,
            'added': time.time(),
            'has_cover': has_cover,
        }
        state.setdefault('music_order', []).append(aid)
        found += 1
        log.info(f"Scanned album: {aid} ({len(audio)} tracks)")
    save_state(state)
    return jsonify({'ok': True, 'found': found,
                    'added_tracks': added, 'removed_tracks': removed})


@app.route('/api/books', methods=['POST'])
def upload_book():
    """Upload a new book or music album (multipart form with audio files).
    Form field 'destination' = 'book' (default) or 'music'.
    For music, optional 'artist' field."""
    title = request.form.get('title', '').strip()
    if not title:
        return jsonify({'error': 'Title is required'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    destination = request.form.get('destination', 'book')
    is_music = (destination == 'music')
    artist = request.form.get('artist', '').strip()
    items, target_dir, order_key, current_key = _collection('music' if is_music else 'book')
    prefix = 'album' if is_music else 'book'

    # Generate item ID
    item_id = secure_filename(title).lower() or f"{prefix}_{int(time.time())}"
    base_id = item_id
    counter = 1
    while item_id in items:
        item_id = f"{base_id}_{counter}"
        counter += 1

    item_dir = target_dir / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    try:
        saved_files = []
        used_names = set()
        for idx, f in enumerate(files):
            original = f.filename or ''
            ext = Path(original).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                continue
            filename = _safe_filename(original, fallback_idx=idx + 1)
            base = filename[:-len(ext)] if filename.endswith(ext) else filename
            final = filename
            n = 1
            while final in used_names or (item_dir / final).exists():
                final = f"{base}_{n}{ext}"
                n += 1
            used_names.add(final)
            filepath = item_dir / final
            f.save(str(filepath))
            saved_files.append(final)
            log.info(f"Saved: {original!r} -> {final}")

        if not saved_files:
            item_dir.rmdir()
            return jsonify({'error': 'No valid audio files'}), 400

        saved_files.sort()

        rec = {
            'title': title,
            'files': saved_files,
            'position': 0.0,
            'current_file_index': 0,
            'total_duration': 0,
            'added': time.time(),
            'has_cover': False,
            'probing': True,
        }
        if is_music:
            rec['artist'] = artist
        items[item_id] = rec

        if state[current_key] is None:
            state[current_key] = item_id

        state.setdefault(order_key, []).append(item_id)
        save_state(state)
        log.info(f"{prefix.capitalize()} added: {title} ({len(saved_files)} files); probing")

        def _probe_durations(iid, idir, fnames):
            total = 0
            for fname in fnames:
                total += _get_duration(str(idir / fname))
            if iid in items:
                items[iid]['total_duration'] = total
                items[iid]['probing'] = False
                save_state(state)
                log.info(f"Probed {iid}: {total:.0f}s total")

        threading.Thread(target=_probe_durations,
                         args=(item_id, item_dir, saved_files), daemon=True).start()

        return jsonify({'id': item_id, 'title': title, 'files_count': len(saved_files),
                        'destination': destination})

    except Exception as e:
        # Cleanup on failure
        import traceback
        log.error(f"Upload failed: {e}\n{traceback.format_exc()}")
        import shutil
        if item_dir.exists():
            shutil.rmtree(str(item_dir))
        if item_id in items:
            del items[item_id]
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

    # Save position of whatever is currently playing (book or music)
    if (player.is_playing() or player.is_paused()) and not player.is_stream():
        a = _active_item()
        if a['item']:
            pos = player.get_position()
            fidx = player.get_current_file_index()
            if pos is not None:
                a['item']['position'] = pos
                a['item']['current_file_index'] = fidx
    # Always stop — even if paused, so play() starts fresh with the new book
    if player.is_playing() or player.is_paused():
        player.stop()

    state['current_book'] = book_id
    state['current_kind'] = 'book'
    state['current_radio'] = None
    save_state(state)
    log.info(f"Selected book: {state['books'][book_id]['title']}")

    return jsonify({'ok': True, 'book': book_id})


@app.route('/api/music/select/<album_id>', methods=['POST'])
def select_music(album_id):
    """Select a music album as current."""
    if album_id not in state['music']:
        return jsonify({'error': 'Album not found'}), 404

    if (player.is_playing() or player.is_paused()) and not player.is_stream():
        a = _active_item()
        if a['item']:
            pos = player.get_position()
            fidx = player.get_current_file_index()
            if pos is not None:
                a['item']['position'] = pos
                a['item']['current_file_index'] = fidx
    if player.is_playing() or player.is_paused():
        player.stop()

    state['current_music'] = album_id
    state['current_kind'] = 'music'
    state['current_radio'] = None
    save_state(state)
    log.info(f"Selected album: {state['music'][album_id]['title']}")

    return jsonify({'ok': True, 'music': album_id})


@app.route('/api/play', methods=['POST'])
def play():
    """Start/resume playback of the current book or music album."""
    # Block playback when amp is off
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    a = _active_item()
    if not a['item']:
        return jsonify({'error': 'Nothing selected'}), 400

    item = a['item']
    _prune_missing_files(item, a['dir'], a['item_id'])
    if not item.get('files'):
        return jsonify({'error': 'Нет файлов для воспроизведения'}), 400
    files = _ordered_paths(item, a['dir'], a['item_id'])
    file_idx = item.get('current_file_index', 0)
    position = item.get('position', 0.0)
    # Music tracks always start from the beginning — no per-track resume,
    # which also makes cross-track position leaking impossible.
    if a['kind'] == 'music':
        position = 0.0

    # Clear radio mode
    state['current_radio'] = None

    if player.is_paused() and not player.is_stream():
        player.resume()
        # Rewind 5 seconds on resume for easier listening
        player.seek(-5)
    else:
        # Also rewind 5 sec when resuming from saved state
        if position > 5:
            position = max(0, position - 5)
        _start_play(files, file_idx, position)

    state['was_playing'] = True
    save_state(state)
    return jsonify({'ok': True, 'position': position})


@app.route('/api/pause', methods=['POST'])
def pause():
    """Pause playback."""
    if player.is_playing():
        if not player.is_stream():
            a = _active_item()
            pos = player.get_position()
            fidx = player.get_current_file_index()
            if a['item'] and pos is not None:
                a['item']['position'] = pos
                a['item']['current_file_index'] = fidx
        player.pause()
    # Manual pause — don't auto-resume on amp-on / power-on
    state['was_playing'] = False
    save_state(state)
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
    """Skip to next file/track in the active book or album."""
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    a = _active_item()
    if not a['item']:
        return jsonify({'error': 'Nothing selected'}), 400
    item = a['item']
    fidx = item.get('current_file_index', 0)
    if fidx >= len(item['files']) - 1:
        return jsonify({'ok': True, 'at_end': True})

    target = fidx + 1
    item['current_file_index'] = target
    item['position'] = 0.0
    state['was_playing'] = True
    save_state(state)

    files = _ordered_paths(item, a['dir'], a['item_id'])
    _start_play(files, target, 0.0)
    return jsonify({'ok': True, 'file_index': target})


@app.route('/api/prev_file', methods=['POST'])
def prev_file():
    """Go to previous file/track in the active book or album."""
    if amp_monitor and not amp_monitor.is_on():
        return jsonify({'error': 'Усилитель выключен', 'amp_off': True}), 409

    a = _active_item()
    if not a['item']:
        return jsonify({'error': 'Nothing selected'}), 400
    item = a['item']
    fidx = item.get('current_file_index', 0)
    target = max(0, fidx - 1)

    item['current_file_index'] = target
    item['position'] = 0.0
    state['was_playing'] = True
    save_state(state)

    # Always restart mpv from target file
    files = _ordered_paths(item, a['dir'], a['item_id'])
    _start_play(files, target, 0.0)
    return jsonify({'ok': True, 'file_index': target})


@app.route('/api/status', methods=['GET'])
def status():
    """Get current playback status (book or music album)."""
    is_radio = player.is_stream()
    kind = _active_kind()

    def build_info(items, item_id):
        if not item_id or item_id not in items:
            return None
        it = items[item_id]
        # Query live player position only if this collection is the active one
        if (not is_radio and kind_matches and (player.is_playing() or player.is_paused())):
            pos = player.get_position()
            fidx = player.get_current_file_index()
            file_dur = player.get_duration()
        else:
            pos = it.get('position', 0)
            fidx = it.get('current_file_index', 0)
            file_dur = None
        ordered = _ordered_filenames(it) if it.get('shuffle') else it.get('files', [])
        return {
            'id': item_id,
            'title': it['title'],
            'artist': it.get('artist', ''),
            'current_file_index': fidx,
            'files_count': len(it['files']),
            'current_file': ordered[fidx] if fidx < len(ordered) else None,
            'position': pos or 0,
            'total_duration': it.get('total_duration', 0),
            'file_duration': file_dur or 0,
            'has_cover': it.get('has_cover', False),
            'shuffle': it.get('shuffle', False),
        }

    kind_matches = (kind == 'book')
    book_info = build_info(state['books'], state['current_book'])
    kind_matches = (kind == 'music')
    music_info = build_info(state['music'], state['current_music'])

    radio_title = None
    radio_name = None
    if is_radio:
        sid = state.get('current_radio')
        st = _resolve_station(sid) if sid else None
        if st:
            radio_name = st.get('name')
        try:
            radio_title = player.get_media_title()
        except Exception:
            radio_title = None
        # Some streams announce their own name as the "track" — that adds
        # nothing next to the station name we already show
        if radio_title and radio_name and radio_title.strip() == radio_name.strip():
            radio_title = None

    return jsonify({
        'playing': player.is_playing() and not is_radio,
        'paused': player.is_paused(),
        'kind': kind,
        'book': book_info,
        'music': music_info,
        'radio': state.get('current_radio'),
        'radio_playing': is_radio,
        'radio_title': radio_title,
        'radio_name': radio_name,
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



# --- API: Network (hotspot + joining foreign wifi) ---

AP_IP = '10.42.0.1'          # NetworkManager's default shared-mode gateway
_ap_fallback_active = False  # AP raised automatically because wifi was missing


def _nmcli(*args, timeout=20):
    """Run nmcli; retry with `sudo -n` on permission errors.
    Returns (CompletedProcess|None, error_str|None)."""
    cmd = ['nmcli'] + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, 'nmcli не найден (нужен NetworkManager)'
    except subprocess.TimeoutExpired:
        return None, 'nmcli: таймаут'
    err = (r.stderr or '').lower()
    # Error text may be localised, so retry on any permission-ish failure
    if r.returncode != 0 and ('permission' in err or 'not authorized' in err
                              or 'insufficient privileges' in err
                              or 'authoriz' in err or 'отказано' in err
                              or 'недостаточно' in err or 'прав' in err):
        try:
            r = subprocess.run(['sudo', '-n'] + cmd, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, 'sudo nmcli: таймаут'
    return r, None


def _nm_split(line):
    """Split a terse (-t) nmcli line, honouring backslash-escaped colons."""
    out, cur, esc = [], '', False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == '\\':
            esc = True
        elif ch == ':':
            out.append(cur)
            cur = ''
        else:
            cur += ch
    out.append(cur)
    return out


def _ap_settings():
    """AP credentials: state override wins over config defaults."""
    ssid = state.get('ap_ssid') or CONFIG.get('hotspot_ssid', 'RadioBook')
    password = state.get('ap_password') or CONFIG.get('hotspot_password', 'radiobook')
    return ssid, password


def _ap_url():
    return 'http://%s:%d' % (AP_IP, CONFIG.get('port', 8080))


def _wifi_device():
    """Name of the first wifi device (wlan0 etc), or None."""
    r, err = _nmcli('-t', '-f', 'DEVICE,TYPE', 'device', 'status')
    if err or r is None or r.returncode != 0:
        return None
    for line in r.stdout.strip().splitlines():
        p = _nm_split(line)
        if len(p) >= 2 and p[1] == 'wifi':
            return p[0]
    return None


def _device_ip(dev):
    """Current IPv4 address of a device, without the /prefix."""
    if not dev:
        return None
    r, err = _nmcli('-t', '-f', 'IP4.ADDRESS', 'device', 'show', dev)
    if err or r is None or r.returncode != 0:
        return None
    for line in r.stdout.strip().splitlines():
        if ':' in line:
            val = line.split(':', 1)[1].strip()
            if val:
                return val.split('/')[0]
    return None


def _active_wifi_connections():
    """List active wifi connections as (name, mode) tuples."""
    r, err = _nmcli('-t', '-f', 'NAME,TYPE', 'connection', 'show', '--active')
    if err or r is None or r.returncode != 0:
        return None, err or (r.stderr.strip() if r else 'nmcli failed')
    result = []
    for line in r.stdout.strip().splitlines():
        parts = _nm_split(line)
        if len(parts) >= 2 and parts[1] == '802-11-wireless':
            name = parts[0]
            r2, _ = _nmcli('-t', '-f', '802-11-wireless.mode',
                           'connection', 'show', name)
            mode = ''
            if r2 is not None and r2.returncode == 0:
                mode = r2.stdout.strip().split(':')[-1]
            result.append((name, mode))
    return result, None


def _ap_active():
    """True if an access point is currently up. None if nmcli is unavailable."""
    cons, err = _active_wifi_connections()
    if cons is None:
        return None
    return any(mode == 'ap' for _, mode in cons)


def _ap_down():
    """Tear down every active AP connection. Returns (ok, error)."""
    cons, err = _active_wifi_connections()
    if cons is None:
        return False, err
    ap_names = [name for name, mode in cons if mode == 'ap']
    for name in ap_names:
        r, err = _nmcli('connection', 'down', name, timeout=40)
        if err or r is None or r.returncode != 0:
            return False, err or (r.stderr.strip() if r else 'не удалось выключить точку доступа')
    return True, None


def _hotspot_up(verify=25):
    """Raise the AP and verify it actually came up.

    nmcli exits 0 as soon as activation *starts*, so a failure in the later
    stages (AP mode refused, dnsmasq missing) looks like success. We re-read
    the device state afterwards to report the truth.
    Returns (ok, error)."""
    ssid, password = _ap_settings()
    dev = _wifi_device()
    args = ['device', 'wifi', 'hotspot']
    if dev:
        args += ['ifname', dev]
    args += ['ssid', ssid, 'password', password]
    log.info("Raising hotspot (ssid=%s, dev=%s)", ssid, dev or '?')
    r, err = _nmcli(*args, timeout=60)
    if err or r is None or r.returncode != 0:
        msg = err or (r.stderr.strip() if r else 'не удалось включить точку доступа')
        log.error("Hotspot command failed: %s", msg)
        return False, msg
    deadline = time.time() + verify
    while time.time() < deadline:
        time.sleep(2)
        if _ap_active():
            log.info("Hotspot '%s' is up", ssid)
            return True, None
    log.error("Hotspot activation did not complete")
    return False, ('точка доступа не поднялась — активация оборвалась. '
                   'Подробности: journalctl -u NetworkManager')


@app.route('/api/network/status')
def network_status():
    ssid, password = _ap_settings()
    port = CONFIG.get('port', 8080)
    base = {
        'ap_ssid': ssid,
        'ap_password': password,
        'ap_url': _ap_url(),
        'ap_fallback': state.get('ap_fallback', True),
        'fallback_active': _ap_fallback_active,
        'hostname_url': 'http://%s.local:%d' % (os.uname().nodename, port),
    }
    cons, err = _active_wifi_connections()
    if cons is None:
        base.update({'available': False, 'error': err})
        return jsonify(base)
    dev = _wifi_device()
    ip = _device_ip(dev)
    hotspot = any(mode == 'ap' for _, mode in cons)
    wifi = next((name for name, mode in cons if mode != 'ap'), None)
    base.update({
        'available': True,
        'hotspot': hotspot,
        'wifi': wifi,
        'device': dev,
        'ip': ip,
        'url': ('http://%s:%d' % (ip, port)) if ip else None,
    })
    with _net_task_lock:
        base['task'] = dict(_net_task)
    return jsonify(base)


@app.route('/api/network/hotspot', methods=['POST'])
def network_hotspot_on():
    """Switch wifi to access-point mode (no reboot needed)."""
    global _ap_fallback_active
    ok, err = _hotspot_up()
    if not ok:
        return jsonify({'error': err}), 500
    _ap_fallback_active = False
    state['network_mode'] = 'hotspot'
    save_state(state)
    ssid, password = _ap_settings()
    return jsonify({'ok': True, 'ssid': ssid, 'password': password,
                    'url': _ap_url()})


@app.route('/api/network/wifi', methods=['POST'])
def network_hotspot_off():
    """Tear down the AP; NetworkManager auto-reconnects to known wifi."""
    global _ap_fallback_active
    active = _ap_active()
    if active is None:
        return jsonify({'error': 'nmcli недоступен'}), 500
    if not active:
        state['network_mode'] = 'wifi'
        save_state(state)
        return jsonify({'ok': True, 'note': 'точка доступа и так выключена'})
    ok, err = _ap_down()
    if not ok:
        return jsonify({'error': err}), 500
    _ap_fallback_active = False
    state['network_mode'] = 'wifi'
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/network/ap', methods=['POST'])
def network_ap_settings():
    """Change the AP name/password. Re-raises the AP if it is currently live."""
    data = request.get_json(silent=True) or {}
    ssid = (data.get('ssid') or '').strip()
    password = (data.get('password') or '').strip()
    if not ssid:
        return jsonify({'error': 'Имя сети не может быть пустым'}), 400
    if len(ssid) > 32:
        return jsonify({'error': 'Имя сети — не длиннее 32 символов'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Пароль должен быть не короче 8 символов (требование WPA2)'}), 400
    state['ap_ssid'] = ssid
    state['ap_password'] = password
    save_state(state)
    log.info("AP settings changed: ssid=%s", ssid)
    if _ap_active():
        # Re-raise in the background: the reply would never reach a client
        # that is connected through the AP we are about to restart.
        threading.Thread(target=_hotspot_up, daemon=True, name='ap-restart').start()
        return jsonify({'ok': True, 'restarted': True, 'ssid': ssid,
                        'password': password, 'url': _ap_url()})
    return jsonify({'ok': True, 'restarted': False, 'ssid': ssid,
                    'password': password, 'url': _ap_url()})


@app.route('/api/network/fallback', methods=['POST'])
def network_fallback_toggle():
    data = request.get_json(silent=True) or {}
    state['ap_fallback'] = bool(data.get('enabled', True))
    save_state(state)
    return jsonify({'ok': True, 'enabled': state['ap_fallback']})


@app.route('/api/network/scan')
def network_scan():
    """List visible wifi networks.

    Note: while the AP is up, many drivers (brcmfmac included) refuse to scan.
    The UI falls back to manual SSID entry when this returns an error."""
    dev = _wifi_device()
    args = ['-t', '-f', 'SSID,SIGNAL,SECURITY,IN-USE', 'device', 'wifi', 'list']
    if dev:
        args += ['ifname', dev]
    args += ['--rescan', 'no' if request.args.get('rescan') == '0' else 'yes']
    r, err = _nmcli(*args, timeout=45)
    if err or r is None or r.returncode != 0:
        msg = err or (r.stderr.strip() if r else 'сканирование не удалось')
        return jsonify({'networks': [], 'error': msg})
    best = {}
    for line in r.stdout.strip().splitlines():
        p = _nm_split(line)
        if len(p) < 3:
            continue
        ssid = p[0].strip()
        if not ssid:
            continue  # hidden network — nothing to show
        try:
            signal = int(p[1])
        except ValueError:
            signal = 0
        entry = {
            'ssid': ssid,
            'signal': signal,
            'security': p[2].strip(),
            'open': not p[2].strip(),
            'in_use': len(p) > 3 and p[3].strip() == '*',
        }
        prev = best.get(ssid)
        if prev is None or signal > prev['signal']:
            best[ssid] = entry
    nets = sorted(best.values(), key=lambda n: -n['signal'])
    return jsonify({'networks': nets, 'scanned_in_ap': bool(_ap_active())})


@app.route('/api/network/saved')
def network_saved():
    """Known wifi profiles, so the user can forget a guest network later."""
    r, err = _nmcli('-t', '-f', 'NAME,TYPE', 'connection', 'show')
    if err or r is None or r.returncode != 0:
        return jsonify({'saved': [], 'error': err or 'nmcli failed'})
    cons, _ = _active_wifi_connections()
    active_names = [n for n, _m in (cons or [])]
    out = []
    for line in r.stdout.strip().splitlines():
        p = _nm_split(line)
        if len(p) >= 2 and p[1] == '802-11-wireless':
            name = p[0]
            # A saved profile may be our own AP even while it is inactive,
            # so read its mode rather than relying on the active list.
            r2, _ = _nmcli('-t', '-f', '802-11-wireless.mode',
                           'connection', 'show', name)
            mode = ''
            if r2 is not None and r2.returncode == 0:
                mode = r2.stdout.strip().split(':')[-1]
            out.append({
                'name': name,
                'active': name in active_names,
                'is_ap': mode == 'ap',
            })
    return jsonify({'saved': out})


@app.route('/api/network/saved/<path:name>', methods=['DELETE'])
def network_forget(name):
    r, err = _nmcli('connection', 'delete', name, timeout=30)
    if err or r is None or r.returncode != 0:
        return jsonify({'error': err or (r.stderr.strip() if r else 'не удалось удалить')}), 500
    log.info("Forgot wifi profile '%s'", name)
    return jsonify({'ok': True})


# --- Joining a wifi network (runs in background: our own link drops) ---

_net_task = {'state': 'idle', 'ssid': None, 'error': None, 'ip': None,
             'ap_restored': False, 'ts': 0}
_net_task_lock = threading.Lock()


def _set_task(**kw):
    with _net_task_lock:
        _net_task.update(kw)
        _net_task['ts'] = time.time()


def _connect_worker(ssid, password, hidden):
    """Join a wifi network, restoring the AP if it does not work out.

    The client that asked for this is very likely talking to us *through* the
    AP we are about to drop, so nothing here can be reported synchronously —
    the result lands in _net_task for /api/network/connect/status."""
    global _ap_fallback_active
    _set_task(state='working', ssid=ssid, error=None, ip=None, ap_restored=False)
    was_ap = bool(_ap_active())
    if was_ap:
        log.info("Wifi connect: dropping AP before joining '%s'", ssid)
        _ap_down()
        time.sleep(3)
    dev = _wifi_device()
    args = ['device', 'wifi', 'connect', ssid]
    if password:
        args += ['password', password]
    if hidden:
        args += ['hidden', 'yes']
    if dev:
        args += ['ifname', dev]
    log.info("Wifi connect: trying '%s'", ssid)
    r, err = _nmcli(*args, timeout=75)
    ok = (r is not None and r.returncode == 0 and not err)
    msg = err or (r.stderr.strip() if r else 'не удалось подключиться')
    if ok:
        ip = None
        for _ in range(10):
            time.sleep(2)
            ip = _device_ip(dev)
            if ip:
                break
        if ip:
            _ap_fallback_active = False
            state['network_mode'] = 'wifi'
            save_state(state)
            _set_task(state='ok', ip=ip, error=None)
            log.info("Wifi connect: '%s' joined, ip=%s", ssid, ip)
            return
        ok = False
        msg = 'подключение установлено, но адрес не получен (нет DHCP?)'
    log.warning("Wifi connect '%s' failed: %s", ssid, msg)
    # Drop the half-made profile so it does not fight autoconnect later
    _nmcli('connection', 'delete', ssid, timeout=20)
    if was_ap:
        ap_ok, ap_err = _hotspot_up()
        _set_task(state='failed', error=msg, ap_restored=bool(ap_ok))
        if ap_ok:
            state['network_mode'] = 'hotspot'
            save_state(state)
        else:
            log.error("Could not restore AP after failed connect: %s", ap_err)
    else:
        # We were on normal wifi; NetworkManager reconnects to a known net
        _set_task(state='failed', error=msg, ap_restored=False)


@app.route('/api/network/connect', methods=['POST'])
def network_connect():
    data = request.get_json(silent=True) or {}
    ssid = (data.get('ssid') or '').strip()
    password = data.get('password') or ''
    hidden = bool(data.get('hidden'))
    if not ssid:
        return jsonify({'error': 'Укажите имя сети'}), 400
    if password and len(password) < 8:
        return jsonify({'error': 'Пароль Wi-Fi короче 8 символов — проверьте'}), 400
    with _net_task_lock:
        if _net_task['state'] == 'working':
            return jsonify({'error': 'Подключение уже выполняется'}), 409
    threading.Thread(target=_connect_worker, args=(ssid, password, hidden),
                     daemon=True, name='wifi-connect').start()
    return jsonify({'ok': True, 'ssid': ssid})


@app.route('/api/network/connect/status')
def network_connect_status():
    with _net_task_lock:
        return jsonify(dict(_net_task))

# --- API: Power (shutdown / reboot from settings) ---

def _power_command(args, what):
    try:
        subprocess.Popen(['sudo', '-n'] + args)
    except FileNotFoundError:
        try:
            subprocess.Popen(args)  # in case server runs as root
        except Exception as e:
            log.error(f"{what} failed: {e}")
            return False, str(e)
    except Exception as e:
        log.error(f"{what} failed: {e}")
        return False, str(e)
    log.info(f"{what} requested via web")
    return True, None


@app.route('/api/power/shutdown', methods=['POST'])
def power_shutdown():
    # Save state before going down
    try:
        save_state(state)
    except Exception:
        pass
    ok, err = _power_command(['shutdown', '-h', 'now'], 'Shutdown')
    if not ok:
        return jsonify({'error': err or 'не удалось выключить'}), 500
    return jsonify({'ok': True})


@app.route('/api/power/reboot', methods=['POST'])
def power_reboot():
    try:
        save_state(state)
    except Exception:
        pass
    ok, err = _power_command(['reboot'], 'Reboot')
    if not ok:
        return jsonify({'error': err or 'не удалось перезагрузить'}), 500
    return jsonify({'ok': True})


def _network_boot():
    """Sort out networking after boot.

    Two jobs:
      1. If the user left the speaker in hotspot mode, put it back.
      2. Otherwise wait for wifi and, if it never arrives, raise the AP as a
         fallback so the speaker is still reachable from a phone.

    The fallback deliberately does NOT write network_mode='hotspot': on the
    next boot we try the real wifi first again, so bringing the speaker home
    from a trip fixes itself."""
    global _ap_fallback_active

    if state.get('network_mode') == 'hotspot':
        log.info("Network restore: hotspot mode was active before reboot")
        for attempt in range(3):
            time.sleep(8 if attempt == 0 else 12)
            active = _ap_active()
            if active is None:
                log.warning("Network restore: nmcli not ready yet")
                continue
            if active:
                log.info("Network restore: hotspot already active")
                return
            ok, err = _hotspot_up()
            if ok:
                return
            log.warning("Network restore attempt %d failed: %s", attempt + 1, err)
        log.error("Network restore: giving up — staying on normal wifi")
        return

    if not state.get('ap_fallback', True):
        return

    # Give NetworkManager a fair chance to associate and get a lease
    deadline = time.time() + 75
    while time.time() < deadline:
        time.sleep(10)
        active = _ap_active()
        if active is None:
            continue      # nmcli not up yet
        if active:
            return        # something already raised an AP — leave it alone
        if _device_ip(_wifi_device()):
            log.info("Network: wifi is up, no fallback needed")
            return

    log.warning("Network: no wifi after 75s — raising fallback hotspot")
    ok, err = _hotspot_up()
    if ok:
        _ap_fallback_active = True
        ssid, password = _ap_settings()
        log.info("Network: fallback hotspot '%s' is up, connect and open %s",
                 ssid, _ap_url())
    else:
        log.error("Network: fallback hotspot failed: %s", err)

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


def _station_cover_path(sid):
    """Where a station's cover image lives. Covers are keyed by id only, so
    default stations can have one without being materialized into state."""
    safe = secure_filename(sid) or 'station'
    return RADIO_COVERS / (safe + '.jpg')


def _materialize_station(sid):
    """Return a station dict that lives in state and can be edited.

    Custom stations are already there. A default station gets copied into
    radio_stations with custom=False — the same trick the proxy flag uses,
    so an edited default keeps behaving like a default (delete = hide).
    Returns None if the id is unknown."""
    for s in state.get('radio_stations', []):
        if s['id'] == sid:
            return s
    for ds in DEFAULT_STATIONS:
        if ds['id'] == sid:
            override = dict(ds)
            override['custom'] = False
            state.setdefault('radio_stations', []).append(override)
            return override
    return None


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
    # Apply the user's manual order; anything not listed keeps its place at
    # the end (sort is stable, so newly added stations stay where they were)
    order = state.get('station_order', [])
    if order:
        pos = {sid: i for i, sid in enumerate(order)}
        tail = len(order)
        all_st.sort(key=lambda s: pos.get(s['id'], tail))
    cur = state.get('current_radio')
    for st in all_st:
        st['is_playing'] = (cur == st['id'] and player.is_playing())
        st['has_cover'] = _station_cover_path(st['id']).exists()
    return jsonify(all_st)


@app.route('/api/radio/reorder', methods=['POST'])
def reorder_radio_stations():
    data = request.get_json() or {}
    new_order = data.get('order', [])
    if not isinstance(new_order, list):
        return jsonify({'error': 'order must be a list'}), 400
    known = {s['id'] for s in state.get('radio_stations', [])}
    known.update(d['id'] for d in DEFAULT_STATIONS)
    valid = [sid for sid in new_order if sid in known]
    seen = set(valid)
    # Keep ids we already had an opinion about, so a partial list doesn't
    # wipe the rest of the ordering
    for sid in state.get('station_order', []):
        if sid in known and sid not in seen:
            valid.append(sid)
            seen.add(sid)
    state['station_order'] = valid
    save_state(state)
    return jsonify({'ok': True})


@app.route('/api/radio/stations/<sid>/rename', methods=['POST'])
def rename_radio_station(sid):
    """Edit a station: display name, genre and stream URL."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Название не может быть пустым'}), 400
    st = _materialize_station(sid)
    if st is None:
        return jsonify({'error': 'Not found'}), 404

    new_url = None
    if 'url' in data:
        new_url = (data.get('url') or '').strip()
        if not new_url:
            return jsonify({'error': 'Ссылка на поток не может быть пустой'}), 400
        if not new_url.startswith(('http://', 'https://')):
            return jsonify({'error': 'Ссылка должна начинаться с http:// или https://'}), 400

    st['name'] = name
    if 'genre' in data:
        st['genre'] = (data.get('genre') or '').strip()
    url_changed = bool(new_url and new_url != st.get('url'))
    if new_url:
        st['url'] = new_url
    save_state(state)
    log.info("Edited station %s -> %r%s", sid, name,
             " (new url)" if url_changed else "")

    # Re-open the stream so the change takes effect without a manual restart
    if url_changed and state.get('current_radio') == sid and player.is_playing():
        proxy_url = STREAM_PROXY if _station_uses_proxy(st) else None
        player.play_stream(st['url'], proxy=proxy_url)

    return jsonify({'ok': True, 'name': st['name'],
                    'genre': st.get('genre', ''), 'url': st.get('url', '')})


@app.route('/api/radio/stations/<sid>/cover', methods=['GET'])
def get_station_cover(sid):
    cover = _station_cover_path(sid)
    if cover.exists():
        return send_file(str(cover), mimetype='image/jpeg')
    return jsonify({'error': 'No cover'}), 404


@app.route('/api/radio/stations/<sid>/cover', methods=['POST'])
def set_station_cover(sid):
    if _resolve_station(sid) is None:
        return jsonify({'error': 'Not found'}), 404
    f = request.files.get('cover')
    if not f:
        return jsonify({'error': 'No cover file'}), 400
    RADIO_COVERS.mkdir(parents=True, exist_ok=True)
    f.save(str(_station_cover_path(sid)))
    log.info("Cover set for station %s", sid)
    return jsonify({'ok': True})


@app.route('/api/radio/stations/<sid>/cover', methods=['DELETE'])
def delete_station_cover(sid):
    cover = _station_cover_path(sid)
    if cover.exists():
        try:
            cover.unlink()
        except OSError as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


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

    # Is it a custom station? A materialized default (rename/proxy override)
    # also lives in radio_stations, but carries custom=False and must be
    # HIDDEN rather than merely un-overridden — otherwise it would come back
    # with its original name.
    customs = state.get('radio_stations', [])
    entry = next((s for s in customs if s['id'] == sid), None)
    is_default = any(d['id'] == sid for d in DEFAULT_STATIONS)
    was_custom = entry is not None and entry.get('custom', True)

    if entry is not None:
        state['radio_stations'] = [s for s in customs if s['id'] != sid]

    if was_custom:
        # User-added station is gone for good — drop its cover too
        cover = _station_cover_path(sid)
        if cover.exists():
            try:
                cover.unlink()
            except OSError:
                pass
    elif is_default:
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
    state['was_playing'] = True
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
    state['was_playing'] = False
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
            _start_play(files, fidx, pos)
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

    # --- Network: restore hotspot mode / fall back to AP if no wifi ---
    threading.Thread(target=_network_boot, daemon=True,
                     name='net-boot').start()

    # --- Boot auto-resume: continue what was playing before power-off ---
    if state.get('was_playing'):
        amp_ok = (amp_monitor is None) or amp_monitor.is_on()
        if amp_ok:
            try:
                if not (player.is_playing() or player.is_paused()):
                    resume_last_playback('(boot auto-resume)')
            except Exception as e:
                log.error(f"Boot auto-resume failed: {e}")
        else:
            # Amp is off — keep the flag, on_amp_state_change will resume when it turns on
            log.info("Boot: amp is off — will auto-resume when amp turns on")

    log.info(f"RadioBook server starting on port {CONFIG['port']}")
    app.run(
        host='0.0.0.0',
        port=CONFIG['port'],
        debug=False,
        threaded=True
    )
