#!/usr/bin/env python3
"""RadioBook configuration."""

import os

_BASE = os.environ.get('RADIOBOOK_HOME', '/home/pi/radiobook')

# Additional directories to scan for books (e.g. network mounts).
# Comma-separated list in RADIOBOOK_EXTRA_DIRS env var.
# Example: RADIOBOOK_EXTRA_DIRS=/mnt/nas/audiobooks,/media/usb/books
_extra = os.environ.get('RADIOBOOK_EXTRA_DIRS', '')
_extra_dirs = [p.strip() for p in _extra.split(',') if p.strip()]

CONFIG = {
    # Server
    'port': int(os.environ.get('RADIOBOOK_PORT', 8080)),

    # Paths
    'books_dir': os.environ.get('RADIOBOOK_BOOKS', os.path.join(_BASE, 'books')),
    'extra_books_dirs': _extra_dirs,
    'state_file': os.environ.get('RADIOBOOK_STATE', os.path.join(_BASE, 'state.json')),
    'tmp_dir': os.path.join(_BASE, 'tmp'),
    # Music albums dir (optional; server defaults to <books_dir>/../music)
    'music_dir': os.environ.get('RADIOBOOK_MUSIC') or None,

    # GPIO for amplifier detection (optocoupler PC817 on BCM 17 = physical pin 11)
    'gpio_enabled': os.environ.get('RADIOBOOK_GPIO', 'true').lower() == 'true',
    'amp_gpio_pin': int(os.environ.get('RADIOBOOK_GPIO_PIN', 17)),  # BCM numbering
    # Inverted-logic (optocoupler PC817 etc). True = amp-ON when pin reads LOW.
    # Direct-wired 3.3V/5V signal → set to false.
    'amp_active_low': os.environ.get('RADIOBOOK_AMP_ACTIVE_LOW', 'true').lower() == 'true',

    # Audio
    'default_volume': 100,  # mpv volume (amp controls actual volume)

    # Hotspot (AP) mode — the network toggle in web settings.
    # Password must be at least 8 characters (WPA2 requirement).
    'hotspot_ssid': os.environ.get('RADIOBOOK_AP_SSID', 'RadioBook'),
    'hotspot_password': os.environ.get('RADIOBOOK_AP_PASS', 'radiobook'),

    # Radio stream proxy. Used when a station has proxy=True (e.g. geoblocked
    # or locally unreachable streams). Accepts "host:port" or full URL.
    # Default is a local Squid/Privoxy on 127.0.0.1:3128.
    'stream_proxy': os.environ.get('RADIOBOOK_STREAM_PROXY', '127.0.0.1:8888'),
}
