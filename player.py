#!/usr/bin/env python3
"""
AudioPlayer — mpv-based audio player with position tracking.
Uses mpv via subprocess + IPC socket for lightweight operation on Pi Zero.
"""

import subprocess
import socket
import json
import time
import os
import threading
import logging
from typing import Optional

log = logging.getLogger('radiobook.player')

MPV_SOCKET = '/tmp/radiobook_mpv.sock'


class AudioPlayer:
    def __init__(self):
        self._process = None
        self._files = []
        self._start_file_index = 0   # offset: first file in mpv playlist
        self._current_file_index = 0  # absolute index of current file
        self._playing = False
        self._paused = False
        self._is_stream = False       # True when playing radio stream
        self._mpv_log = None          # stderr log file for mpv diagnostics
        self._lock = threading.Lock()

    def play(self, files: list, file_index: int = 0, position: float = 0.0):
        """Start playing from a specific file and position."""
        with self._lock:
            self.stop()
            
            if not files or file_index >= len(files):
                log.error("No files to play or invalid index")
                return

            self._files = files
            self._start_file_index = file_index
            self._current_file_index = file_index
            self._is_stream = False
            
            current_file = files[file_index]
            
            # Remove old socket
            if os.path.exists(MPV_SOCKET):
                os.remove(MPV_SOCKET)

            # Build mpv command
            cmd = [
                'mpv',
                '--no-video',
                '--no-terminal',
                '--audio-channels=mono',
                f'--input-ipc-server={MPV_SOCKET}',
                '--idle=no',
                '--keep-open=no',
            ]
            
            if position > 0:
                cmd.append(f'--start={position}')
            
            # Add current file and remaining files
            for i in range(file_index, len(files)):
                cmd.append(files[i])

            log.info(f"Starting mpv: file={os.path.basename(current_file)}, pos={position:.1f}s")
            
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self._playing = True
                self._paused = False
                
                # Wait for socket to be ready
                for _ in range(20):
                    if os.path.exists(MPV_SOCKET):
                        break
                    time.sleep(0.1)
                    
            except FileNotFoundError:
                log.error("mpv not found! Install with: sudo apt install mpv")
                self._playing = False

    def play_stream(self, url, proxy=None):
        """Play an internet radio stream.

        If proxy is set (e.g. 'http://127.0.0.1:3128'), mpv is launched with
        http_proxy/https_proxy env vars so it fetches the stream through it.
        """
        with self._lock:
            self.stop()
            if os.path.exists(MPV_SOCKET):
                os.remove(MPV_SOCKET)
            self._files = []
            self._start_file_index = 0
            self._current_file_index = 0
            self._is_stream = True
            cmd = ['mpv', '--no-video', '--no-terminal',
                   '--input-ipc-server=' + MPV_SOCKET,
                   '--audio-channels=mono',
                   # Buffering for streams — Pi Zero needs headroom
                   '--cache=yes',
                   '--demuxer-max-bytes=2097152',  # 2 MB demuxer buffer
                   '--cache-secs=10',
                   '--log-file=/tmp/radiobook_mpv.log',
                   url]

            env = None
            if proxy:
                env = os.environ.copy()
                env['http_proxy'] = proxy
                env['https_proxy'] = proxy
                env['HTTP_PROXY'] = proxy
                env['HTTPS_PROXY'] = proxy
                log.info("Stream (via proxy %s): %s", proxy, url)
            else:
                log.info("Stream: " + url)

            try:
                # Log mpv stderr for diagnostics (instead of DEVNULL)
                self._mpv_log = open('/tmp/radiobook_mpv.log', 'w')
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=self._mpv_log,
                    env=env,
                )
                self._playing = True
                self._paused = False
                for _ in range(20):
                    if os.path.exists(MPV_SOCKET): break
                    time.sleep(0.1)
            except FileNotFoundError:
                log.error("mpv not found")
                self._playing = False

    def pause(self):
        """Pause playback."""
        with self._lock:
            if self._playing and not self._paused:
                self._send_command({'command': ['set_property', 'pause', True]})
                self._paused = True

    def resume(self):
        """Resume playback."""
        with self._lock:
            if self._paused:
                self._send_command({'command': ['set_property', 'pause', False]})
                self._paused = False
                self._playing = True

    def stop(self):
        """Stop playback completely."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        if self._mpv_log:
            try:
                self._mpv_log.close()
            except Exception:
                pass
            self._mpv_log = None
        self._playing = False
        self._paused = False
        self._is_stream = False

    def seek(self, offset: float):
        """Seek relative (seconds)."""
        self._send_command({'command': ['seek', offset, 'relative']})

    def next_file(self):
        """Skip to next file in playlist."""
        self._send_command({'command': ['playlist-next']})
        if self._current_file_index < len(self._files) - 1:
            self._current_file_index += 1

    def prev_file(self):
        """Go to previous file."""
        self._send_command({'command': ['playlist-prev']})
        if self._current_file_index > self._start_file_index:
            self._current_file_index -= 1

    def get_position(self) -> Optional[float]:
        """Get current playback position in seconds."""
        resp = self._send_command({
            'command': ['get_property', 'time-pos']
        })
        if resp and 'data' in resp:
            return resp['data']
        return None

    def get_current_file_index(self) -> int:
        """Get current file index in playlist."""
        resp = self._send_command({
            'command': ['get_property', 'playlist-pos']
        })
        if resp and 'data' in resp and resp['data'] is not None:
            ppos = resp['data']
            if ppos >= 0:
                idx = self._start_file_index + ppos
                self._current_file_index = idx  # keep fallback in sync
                return idx
        return self._current_file_index

    def get_duration(self) -> Optional[float]:
        """Get duration of current file."""
        resp = self._send_command({
            'command': ['get_property', 'duration']
        })
        if resp and 'data' in resp:
            return resp['data']
        return None

    def get_media_title(self) -> Optional[str]:
        """Currently announced track for a stream (ICY metadata).

        mpv exposes the ICY title as `media-title`, but before the first
        metadata packet arrives that property just mirrors the URL — so a
        URL-looking value is treated as 'nothing yet'. Falls back to the
        `icy-title` entry of the `metadata` property, which some streams
        populate while media-title stays empty."""
        resp = self._send_command({
            'command': ['get_property', 'media-title']
        })
        title = resp.get('data') if resp else None
        if isinstance(title, str):
            title = title.strip()
            if title and not title.startswith(('http://', 'https://')):
                return title

        resp = self._send_command({
            'command': ['get_property', 'metadata']
        })
        meta = resp.get('data') if resp else None
        if isinstance(meta, dict):
            for key in ('icy-title', 'icy-name', 'title'):
                val = meta.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return None

    def is_playing(self) -> bool:
        """Check if actively playing (not paused)."""
        if self._process and self._process.poll() is not None:
            self._playing = False
            self._paused = False
            self._process = None
        return self._playing and not self._paused

    def is_paused(self) -> bool:
        """Check if paused."""
        if self._process and self._process.poll() is not None:
            self._playing = False
            self._paused = False
            self._process = None
        return self._paused

    def is_stream(self) -> bool:
        """Check if currently playing a radio stream (not a book)."""
        return self._is_stream and self._playing

    def _send_command(self, cmd: dict) -> Optional[dict]:
        """Send JSON IPC command to mpv."""
        if not os.path.exists(MPV_SOCKET):
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(MPV_SOCKET)
            msg = json.dumps(cmd) + '\n'
            sock.sendall(msg.encode())
            data = sock.recv(4096)
            sock.close()
            if data:
                # mpv may return multiple lines
                for line in data.decode().strip().split('\n'):
                    try:
                        resp = json.loads(line)
                        if 'error' in resp and resp['error'] == 'success':
                            return resp
                        if 'data' in resp:
                            return resp
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            log.debug(f"IPC error: {e}")
        return None
