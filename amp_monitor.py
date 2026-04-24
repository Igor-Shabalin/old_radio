#!/usr/bin/env python3
"""AmpMonitor — sysfs GPIO for Banana Pi M2 Zero.

When wired through an optocoupler (PC817), the logic is inverted:
  amp ON  → LED lit → transistor open  → GPIO pulled to GND → reads 0
  amp OFF → LED off → transistor closed → pull-up holds GPIO → reads 1

Set active_low=True (default) to interpret GPIO=0 as "amp on".
Set active_low=False for direct-drive logic (GPIO=1 means "amp on").
"""
import threading, logging, time, os, subprocess

log = logging.getLogger('radiobook.amp')

class AmpMonitor:
    def __init__(self, pin, callback, debounce_ms=500, active_low=True):
        self._pin = pin
        self._callback = callback
        self._debounce = debounce_ms / 1000.0
        self._active_low = bool(active_low)
        self._running = False
        self._thread = None
        self._amp_on = False
        self._gpio_path = '/sys/class/gpio/gpio' + str(pin)
        if not os.path.exists(self._gpio_path):
            subprocess.run(['sh','-c','echo %d > /sys/class/gpio/export' % pin], check=True)
            time.sleep(0.1)
        subprocess.run(['sh','-c','echo in > %s/direction' % self._gpio_path], check=True)
        log.info("GPIO %d ready (sysfs), active_low=%s" % (pin, self._active_low))

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread: self._thread.join(timeout=2)

    def is_on(self):
        return self._amp_on

    def _read(self):
        """Return True if amplifier is considered ON, honoring active_low."""
        try:
            with open(self._gpio_path + '/value') as f:
                raw = f.read().strip() == '1'  # True if pin reads logic 1
        except Exception:
            return False
        return (not raw) if self._active_low else raw

    def _loop(self):
        while self._running:
            try:
                cur = self._read()
                if cur != self._amp_on:
                    time.sleep(self._debounce)
                    if self._read() == cur:
                        self._amp_on = cur
                        log.info("Amp: %s" % ('ON' if cur else 'OFF'))
                        try: self._callback(cur)
                        except Exception as e: log.error("Callback: %s" % e)
            except Exception as e:
                log.error("Monitor: %s" % e)
            time.sleep(0.1)

