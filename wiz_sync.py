"""WiZ Sync — syncs WiZ light colors to PC audio output (WASAPI loopback).

Run: python wiz_sync.py
"""
import colorsys
import json
import math
import os
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
import pyaudiowpatch as pyaudio

WIZ_PORT = 38899
LIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "lights.json")
CHUNK = 1024  # ~21ms at 48kHz — keeps light latency low
SEND_HZ = 15

# ---------------------------------------------------------------- WiZ (UDP)

def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def discover_lights(timeout=2.0):
    """Broadcast getPilot, return list of responder IPs."""
    targets = {"255.255.255.255"}
    ip = _local_ip()
    if ip:
        # directed-broadcast fallbacks; stdlib can't read the netmask, so try
        # /24 and /16 shapes — covers nearly all home LANs
        parts = ip.split(".")
        targets.add(".".join(parts[:3]) + ".255")
        targets.add(".".join(parts[:2]) + ".255.255")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if ip:  # bind so broadcasts leave on the LAN interface, not e.g. a VPN
        try:
            s.bind((ip, 0))
        except OSError:
            pass
    s.settimeout(0.3)
    # some firmwares only answer registration broadcasts, others only getPilot
    msgs = [
        b'{"method":"registration","params":{"phoneMac":"AAAAAAAAAAAA",'
        b'"register":false,"phoneIp":"1.2.3.4","id":"1"}}',
        b'{"method":"getPilot","params":{}}',
    ]
    found = set()
    deadline = time.time() + timeout
    for t in targets:
        for msg in msgs:
            try:
                s.sendto(msg, (t, WIZ_PORT))
            except OSError:
                pass
    while time.time() < deadline:
        try:
            _, addr = s.recvfrom(1024)
            found.add(addr[0])
        except socket.timeout:
            pass
    s.close()
    return sorted(found)


class WizSender:
    def __init__(self, ips):
        self.ips = list(ips)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def set_color(self, r, g, b, dimming):
        payload = json.dumps({"method": "setPilot", "params": {
            "r": int(r), "g": int(g), "b": int(b),
            "dimming": max(10, min(100, int(dimming)))}}).encode()
        for ip in self.ips:
            try:
                self.sock.sendto(payload, (ip, WIZ_PORT))
            except OSError:
                pass

    def restore_white(self):
        payload = json.dumps({"method": "setPilot", "params": {
            "temp": 2700, "dimming": 80}}).encode()
        for ip in self.ips:
            try:
                self.sock.sendto(payload, (ip, WIZ_PORT))
            except OSError:
                pass

    def close(self):
        self.sock.close()


# ------------------------------------------------------- audio -> color

N_BARS = 20  # spectrum-bar count for the visualiser

# Two-layer model: COLOR follows the loudness envelope (mode-independent heat
# gradient blue->red), BRIGHTNESS pulses on beats scaled by beat strength.
# Modes are "feel" presets tuning the throb: `flash` scales the beat punch,
# `smooth` sets the between-beat decay (higher = longer throb), `floor` is the
# dim level between beats, `sens` the beat trigger ease. `color`: "gradient"
# (heat map) for all but Disco, which "cycle"s the hue `jump` degrees per beat.
MODES = {
    "Rave":    dict(color="gradient", smooth=0.05, sens=1.5, flash=1.2, floor=8),
    "Techno":  dict(color="gradient", smooth=0.15, sens=1.4, flash=1.1, floor=12),
    "Disco":   dict(color="cycle",    smooth=0.45, sens=1.1, flash=1.0, floor=20,
                    jump=60),
    "Party":   dict(color="gradient", smooth=0.30, sens=1.3, flash=1.05, floor=15),
    "Pop":     dict(color="gradient", smooth=0.45, sens=1.1, flash=1.0, floor=20),
    "Concert": dict(color="gradient", smooth=0.65, sens=1.2, flash=1.0, floor=25),
    "Jazz":    dict(color="gradient", smooth=0.80, sens=0.8, flash=0.6, floor=35),
}
DEFAULT_MODE = "Party"

_BAR_EDGES = np.logspace(np.log10(30), np.log10(8000), N_BARS + 1)
SILENCE_DB = -60.0  # below this the loudness floor isn't tracked (pauses, gaps)


def new_map_state():
    # db_lo starts at the top so it snaps down to the first real level
    return {"env": 1e-6, "db_lo": 0.0, "db_hi": SILENCE_DB, "env_norm": 0.0,
            "rgb": np.zeros(3), "dim": 10.0, "onset": False, "hue": 0.0,
            "prev_chunk": np.zeros(0), "prev_kick": 0.0, "flux_hist": [],
            "flux_peak": 1e-6, "since_onset": 999,
            "spec_peak": np.full(N_BARS, 1e-6), "spectrum": np.zeros(N_BARS)}


def audio_to_color(mono, rate, state, sensitivity=1.0, smooth=None, mode=None,
                   floor=None, flash=None):
    """Map one mono float chunk (-1..1) to (r, g, b, dimming). Mutates state.

    COLOR = loudness envelope -> heat gradient (or hue cycle in Disco).
    BRIGHTNESS = beat flash (strength-scaled) decaying to the mode's floor.
    Sets state["onset"] on a beat, state["env_norm"]/state["spectrum"] for the
    visualiser.
    """
    mode = mode or MODES[DEFAULT_MODE]
    floor = mode["floor"] if floor is None else floor
    flash = mode["flash"] if flash is None else flash
    smooth = mode["smooth"] if smooth is None else smooth
    hop = len(mono)
    # widen the analysis window to 2 hops (~43ms) for finer low-freq resolution
    # while still updating every hop; the kick band gets ~2x the bins
    prev = state["prev_chunk"]
    win = np.concatenate([prev, mono]) if len(prev) == hop else mono
    state["prev_chunk"] = mono
    n = len(win)
    mag = np.abs(np.fft.rfft(win * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / rate)

    # ---- loudness envelope (fast attack, slow release) -> COLOR
    rms = float(np.sqrt(np.mean(mono ** 2)))
    env = state["env"]
    env += (0.6 if rms > env else 0.08) * (rms - env)
    state["env"] = env
    # normalise in dB (perceived loudness) between a tracked floor and ceiling.
    # silence is gated out of the floor, else a track gap drags it to -120dB
    # and all music afterwards reads near-max (red) for minutes
    db = 20.0 * math.log10(max(env, 1e-9))
    if db > SILENCE_DB:
        if db < state["db_lo"]:
            state["db_lo"] = db
        else:
            state["db_lo"] += (db - state["db_lo"]) * 0.0005  # creep up slowly
    state["db_hi"] = max(db, state["db_hi"] - 0.004)  # ~0.2 dB/s fall
    rng = max(state["db_hi"] - state["db_lo"], 6.0)
    norm = float(np.clip((db - state["db_lo"]) / rng, 0.0, 1.0))
    state["env_norm"] = norm

    # ---- beat detection: kick-band (30-150Hz) energy flux vs adaptive
    # threshold + refractory gap. beat strength scales the flash.
    kick = float(mag[(freqs >= 30) & (freqs < 150)].mean())
    flux = max(0.0, kick - state["prev_kick"])
    state["prev_kick"] = kick
    hist = state["flux_hist"]
    state["flux_peak"] = max(flux, state["flux_peak"] * 0.995, 1e-6)
    state["since_onset"] += 1
    k = float(np.clip(2.0 / max(sensitivity, 0.1), 0.5, 4.0))  # lower k = easier
    refractory = max(1, int(0.12 * rate / hop))
    # threshold from past frames only — a spike mustn't raise its own bar
    onset = (len(hist) > 10
             and flux > np.mean(hist) + k * np.std(hist)
             and flux > state["flux_peak"] * 0.1
             and state["since_onset"] >= refractory)
    hist.append(flux)
    if len(hist) > max(20, int(rate / hop)):
        hist.pop(0)
    if onset:
        state["since_onset"] = 0
    state["onset"] = onset

    # ---- spectrum for the visualiser bars (log-spaced, auto-gained).
    # sum into buckets so narrow low-freq buckets with no bins read 0 (not NaN);
    # only bins inside [30,8000) count so DC and >8kHz don't pile on the edges
    in_range = (freqs >= _BAR_EDGES[0]) & (freqs < _BAR_EDGES[-1])
    idx = np.searchsorted(_BAR_EDGES, freqs[in_range], side="right") - 1
    spec = np.zeros(N_BARS)
    np.add.at(spec, idx, mag[in_range])
    state["spec_peak"] = np.maximum(spec, state["spec_peak"] * 0.99)
    state["spectrum"] = spec / np.maximum(state["spec_peak"], 1e-6)

    # ---- COLOR
    if mode["color"] == "cycle":
        if onset:
            state["hue"] = (state["hue"] + mode["jump"]) % 360.0
        rgb_raw = np.array(colorsys.hsv_to_rgb(state["hue"] / 360.0, 1, 1)) * 255.0
        if onset:  # snap on beat
            state["rgb"] = rgb_raw
        else:
            state["rgb"] += 0.3 * (rgb_raw - state["rgb"])
    else:  # heat gradient: quiet=blue (hue 240) -> loud=red (hue 0)
        rgb_raw = np.array(colorsys.hsv_to_rgb((1 - norm) * 240 / 360, 1, 1)) * 255.0
        state["rgb"] += 0.3 * (rgb_raw - state["rgb"])

    # ---- BRIGHTNESS: beat flash decaying to the mode's floor
    # smooth -> flash half-life in seconds (30ms..600ms), so the throb is
    # independent of hop size and long enough for bulbs fed at SEND_HZ to show
    smooth = min(max(smooth, 0.0), 0.95)
    half_life = 0.03 + 0.6 * smooth
    decay = 1.0 - 0.5 ** ((hop / rate) / half_life)
    if onset:
        # a detected beat already cleared the adaptive threshold, so it always
        # pops; strength only modulates the top half (soft beat 50% -> hard 100%)
        strength = float(np.clip(flux / max(state["flux_peak"], 1e-6), 0.0, 1.0))
        level = floor + (100.0 - floor) * flash * (0.5 + 0.5 * strength)
        state["dim"] = min(100.0, max(state["dim"], level))
    else:
        state["dim"] += decay * (floor - state["dim"])
    state["dim"] = float(np.clip(state["dim"], floor, 100.0))

    r, g, b = np.clip(state["rgb"], 0, 255)
    return int(r), int(g), int(b), int(state["dim"])


# ---------------------------------------------------------------- audio in

def open_loopback(pa):
    """Return an open input stream on the default output's loopback + its info."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    dev = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if not dev.get("isLoopbackDevice"):
        for lb in pa.get_loopback_device_info_generator():
            if dev["name"] in lb["name"]:
                dev = lb
                break
        else:
            raise RuntimeError("No loopback device found for default output")
    stream = pa.open(format=pyaudio.paInt16,
                     channels=dev["maxInputChannels"],
                     rate=int(dev["defaultSampleRate"]),
                     frames_per_buffer=CHUNK,
                     input=True,
                     input_device_index=dev["index"])
    return stream, dev


# --------------------------------------------------------------------- GUI
# Marshall-amp look: black tolex body, brass control plate, gold-numbered
# knobs with cream pointers, a glowing pilot jewel. Knobs are drawn on a Canvas
# (tkinter has no rotary widget) and drag vertically to turn.

TOLEX, TOLEX2 = "#0c0c0e", "#161417"
GOLD, GOLD_HI, GOLD_LO = "#c8962a", "#f0c869", "#7d5a15"
PLATE_INK = "#241a08"            # dark text printed on the brass plate
CREAM = "#f3ead3"                # white script + knob pointers
LINE = "#2a2622"
KNOB_EDGE, KNOB_TOP, KNOB_HI = "#0e0e10", "#1d1d21", "#4a4a52"
LAMP_OFF = "#4a1410"
RED, RED_HI, RED_LO = "#a52a1c", "#d4553f", "#5e1509"  # tactile button face
BAR_REST = "#5c4718"
SCRIPT = ("Brush Script MT", 34, "bold italic")
BADGE = ("Consolas", 8, "bold")
KNOB_LBL = ("Arial Narrow", 9, "bold")
KNOB_VAL = ("Consolas", 9, "bold")
FONT = ("Segoe UI", 10)
FONT_SM = ("Segoe UI", 9)
FONT_SEC = ("Segoe UI", 8, "bold")

HEAD_W, HEAD_H = 900, 250


class Knob:
    """A drawn rotary control on a shared canvas. Grab and turn it like a real
    knob; writes through a tk var so the rest of the app reads it unchanged."""
    START, SWEEP = -135.0, 270.0  # degrees clockwise from 12 o'clock
    DEAD = 0.35   # ignore drags within this fraction of r (angle jitters at hub)
    FINE = 0.25   # Shift-drag speed multiplier

    def __init__(self, canvas, cx, cy, r, label, var, lo=0.0, hi=1.0,
                 choices=None, fmt=None):
        self.c, self.cx, self.cy, self.r = canvas, cx, cy, r
        self.var, self.lo, self.hi = var, lo, hi
        self.choices, self.fmt = choices, fmt
        self.tag = f"k{id(self)}"
        self._draw_ticks()
        self._draw_body()
        canvas.create_text(cx, cy + r + 15, text=label.upper(), fill=PLATE_INK,
                           font=KNOB_LBL)
        self.valtext = canvas.create_text(cx, cy + r + 29, fill=PLATE_INK,
                                          font=KNOB_VAL)
        self.pointer = canvas.create_line(cx, cy, cx, cy - r, fill=CREAM,
                                          width=3, capstyle="round",
                                          tags=self.tag)
        canvas.tag_bind(self.tag, "<Button-1>", self._press)
        canvas.tag_bind(self.tag, "<B1-Motion>", self._drag)
        var.trace_add("write", lambda *_: self._render())
        self._render()

    def _n(self):
        return len(self.choices) if self.choices else 11

    def _frac(self):
        if self.choices:
            try:
                return self.choices.index(self.var.get()) / (len(self.choices) - 1)
            except ValueError:
                return 0.0
        return min(max((self.var.get() - self.lo) / (self.hi - self.lo), 0), 1)

    def _vec(self, frac, rad):
        th = math.radians(self.START + frac * self.SWEEP)
        return self.cx + math.sin(th) * rad, self.cy - math.cos(th) * rad

    def _draw_ticks(self):
        for i in range(self._n()):
            f = i / (self._n() - 1)
            x0, y0 = self._vec(f, self.r + 4)
            x1, y1 = self._vec(f, self.r + 10)
            self.c.create_line(x0, y0, x1, y1, fill=PLATE_INK, width=2)
        if not self.choices:  # min / max printed at the ends of the scale
            for frac, val, anc in ((0.0, self.lo, "e"), (1.0, self.hi, "w")):
                x, y = self._vec(frac, self.r + 14)
                self.c.create_text(x, y, text=self.fmt(val), anchor=anc,
                                   fill=PLATE_INK, font=("Consolas", 7))

    def _draw_body(self):
        r, cx, cy = self.r, self.cx, self.cy
        self.c.create_oval(cx - r + 2, cy - r + 4, cx + r + 2, cy + r + 4,
                           fill="#000000", outline="")               # shadow
        self.c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=KNOB_EDGE,
                           outline=GOLD_LO, width=1, tags=self.tag)   # skirt
        for a in range(0, 360, 12):                                   # knurl
            th = math.radians(a)
            self.c.create_line(cx + math.cos(th) * r * 0.82,
                               cy + math.sin(th) * r * 0.82,
                               cx + math.cos(th) * r, cy + math.sin(th) * r,
                               fill="#000000", tags=self.tag)
        self.c.create_oval(cx - r * 0.78, cy - r * 0.78, cx + r * 0.78,
                           cy + r * 0.78, fill=KNOB_TOP, outline="",
                           tags=self.tag)                             # cap
        self.c.create_arc(cx - r * 0.6, cy - r * 0.6, cx + r * 0.6, cy + r * 0.6,
                          start=55, extent=80, style="arc", outline=KNOB_HI,
                          width=2, tags=self.tag)                     # highlight
        self.c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=GOLD,
                           outline="", tags=self.tag)                 # axle cap

    def _render(self):
        x1, y1 = self._vec(self._frac(), self.r * 0.30)
        x2, y2 = self._vec(self._frac(), self.r * 0.86)
        self.c.coords(self.pointer, x1, y1, x2, y2)
        self.c.tag_raise(self.pointer)
        txt = self.var.get() if self.choices else self.fmt(self.var.get())
        self.c.itemconfig(self.valtext, text=txt)

    def _apply(self, f):
        f = min(max(f, 0.0), 1.0)
        if self.choices:
            self.var.set(self.choices[int(round(f * (len(self.choices) - 1)))])
        else:
            self.var.set(self.lo + f * (self.hi - self.lo))

    def _angle(self, e):  # degrees clockwise from 12 o'clock, matching _vec
        return math.degrees(math.atan2(e.x - self.cx, -(e.y - self.cy)))

    def _press(self, e):
        self._base = self._frac()       # value locked in at grab
        self._prev = self._angle(e)
        self._turn = 0.0                # signed degrees turned since grab

    def _drag(self, e):
        a = self._angle(e)
        step = (a - self._prev + 180) % 360 - 180  # shortest signed turn
        self._prev = a
        if math.hypot(e.x - self.cx, e.y - self.cy) < self.r * self.DEAD:
            return  # dead zone: keep tracking angle, but don't turn the knob
        if e.state & 0x0001:            # Shift held → fine adjust
            step *= self.FINE
        lo, hi = -self._base * self.SWEEP, (1 - self._base) * self.SWEEP
        self._turn = min(max(self._turn + step, lo), hi)  # clamp to 270° endstops
        self._apply(self._base + self._turn / self.SWEEP)


class App:
    def __init__(self, root):
        self.root = root
        root.title("WiZ Sync")
        root.configure(bg=TOLEX)
        root.resizable(False, False)
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.current = (0, 0, 0, 10)  # last computed color, read by GUI poll
        self.env_hist = np.zeros(140)  # scrolling loudness envelope (0..1)
        self.beat_hist = np.zeros(140)  # 1.0 where a beat fired, else 0
        self.spectrum = np.zeros(N_BARS)  # latest spectrum bars (0..1)
        self.status_text = "Idle"
        self.mode = MODES[DEFAULT_MODE]
        self.viz_w, self.viz_h, self.top_h = 400, 120, 64
        self._pwr_off, self._pwr_pressed = 0, False  # power-switch depth state

        self._setup_style()
        self.mode_name = tk.StringVar(value=DEFAULT_MODE)
        self.sensitivity = tk.DoubleVar(value=self.mode["sens"])
        self.max_bright = tk.DoubleVar(value=100)
        self.smooth = tk.DoubleVar(value=self.mode["smooth"])
        self.floor = tk.DoubleVar(value=self.mode["floor"])
        self.flash = tk.DoubleVar(value=self.mode["flash"])
        # the worker reads this plain snapshot, never the tk vars: a tk call
        # from another thread blocks until the GUI thread services it, which
        # stalls audio reads and makes the lights lag the beat
        for v in (self.sensitivity, self.max_bright, self.smooth, self.floor,
                  self.flash):
            v.trace_add("write", self._snap_params)
        self._snap_params()

        self._build_head(root)
        self._build_panel(root)

        self.mode_name.trace_add("write", self.on_mode)
        self.load_lights()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    # ---- the amp head: logo, brass plate, knobs, pilot jewel
    def _build_head(self, root):
        c = tk.Canvas(root, width=HEAD_W, height=HEAD_H, bg=TOLEX,
                      highlightthickness=0)
        c.pack(fill="x")
        self.head = c
        # levant-grain tolex: two sparse stipple layers over the black body
        c.create_rectangle(0, 0, HEAD_W, HEAD_H, fill="#231f1b", stipple="gray12",
                           outline="")                   # grain highlights
        c.create_rectangle(0, 0, HEAD_W, HEAD_H, fill="#000000", stipple="gray25",
                           outline="")                   # grain shadows
        c.create_rectangle(6, 6, HEAD_W - 6, HEAD_H - 6, outline=GOLD_LO,
                           width=2)                      # gold piping
        c.create_text(30, 26, text="WiZ Sync", anchor="w", fill=CREAM,
                      font=SCRIPT)
        c.create_text(33, 56, text="A U D I O - R E A C T I V E   A M P L I F I E R",
                      anchor="w", fill=GOLD, font=BADGE)
        # brass control plate
        px0, py0, px1, py1 = 18, 74, HEAD_W - 18, 214
        c.create_rectangle(px0, py0, px1, py1, fill=GOLD, outline=GOLD_LO,
                           width=2)
        c.create_line(px0 + 2, py0 + 2, px1 - 2, py0 + 2, fill=GOLD_HI)  # bevel
        c.create_line(px0 + 2, py1 - 2, px1 - 2, py1 - 2, fill=GOLD_LO)
        for sx, sy in ((px0 + 12, py0 + 12), (px1 - 12, py0 + 12),
                       (px0 + 12, py1 - 12), (px1 - 12, py1 - 12)):
            c.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, fill=GOLD_LO,
                          outline=PLATE_INK)             # panel screws

        cy, r = 138, 32
        xs = [80, 204, 327, 451, 575, 698]
        Knob(c, xs[0], cy, r, "Mode", self.mode_name, choices=list(MODES))
        Knob(c, xs[1], cy, r, "Sens", self.sensitivity, 0.2, 3.0,
             fmt=lambda v: f"{v:.2f}")
        Knob(c, xs[2], cy, r, "Bright", self.max_bright, 10, 100,
             fmt=lambda v: f"{int(round(v))}")
        Knob(c, xs[3], cy, r, "Smooth", self.smooth, 0.0, 0.95,
             fmt=lambda v: f"{v:.2f}")
        Knob(c, xs[4], cy, r, "Floor", self.floor, 0, 60,
             fmt=lambda v: f"{int(round(v))}")
        Knob(c, xs[5], cy, r, "Flash", self.flash, 0.2, 1.5,
             fmt=lambda v: f"{v:.2f}")

        # pilot jewel = latching push-switch: sits out when off, sinks in + lights
        # when on, with a momentary press-down on click. Cluster tagged "powerbtn"
        # (moves together); "power" is the click target. The socket/well stays put.
        lx, ly = 826, 128
        c.create_oval(lx - 24, ly - 22, lx + 24, ly + 26, fill="#050505",
                      outline=GOLD_LO, width=2)          # switch well (static)
        pb = ("power", "powerbtn")
        c.create_oval(lx - 22, ly - 22, lx + 22, ly + 22, fill=KNOB_EDGE,
                      outline=GOLD_HI, width=2, tags=pb)  # chrome bezel
        self.glow = c.create_oval(lx - 19, ly - 19, lx + 19, ly + 19,
                                  fill=LAMP_OFF, outline="", tags=pb)
        self.lamp = c.create_oval(lx - 13, ly - 13, lx + 13, ly + 13,
                                  fill=LAMP_OFF, outline="", tags=pb)
        c.create_oval(lx - 8, ly - 9, lx - 2, ly - 3, fill=CREAM,
                      outline="", tags=pb)               # jewel glint
        c.create_text(lx, ly + 40, text="POWER", fill=PLATE_INK, font=KNOB_LBL)
        self.power_txt = c.create_text(lx, ly + 53, text="START", fill=PLATE_INK,
                                       font=("Consolas", 10, "bold"))
        c.tag_bind("power", "<ButtonPress-1>", self._power_press)
        c.tag_bind("power", "<ButtonRelease-1>", self._power_release)
        c.tag_bind("power", "<Leave>", self._power_leave)

    # ---- back panel: light management + monitor
    def _build_panel(self, root):
        panel = ttk.Frame(root, padding=(16, 12, 16, 12))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)

        lf = ttk.Frame(panel)
        lf.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        ttk.Label(lf, text="LIGHTS", style="Sec.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.listbox = tk.Listbox(
            lf, selectmode=tk.MULTIPLE, height=4, width=26, exportselection=False,
            bg=TOLEX2, fg=CREAM, selectbackground=GOLD, selectforeground=TOLEX,
            highlightthickness=1, highlightbackground=LINE, highlightcolor=GOLD,
            borderwidth=0, relief="flat", activestyle="none", font=FONT)
        self.listbox.grid(row=1, column=0, rowspan=3, sticky="nw")
        ttk.Button(lf, text="Discover", command=self.discover).grid(
            row=1, column=1, sticky="we", padx=(8, 0))
        ttk.Button(lf, text="Save", command=self.save_lights).grid(
            row=2, column=1, sticky="we", padx=(8, 0), pady=4)
        ttk.Button(lf, text="Remove", command=self.remove_selected).grid(
            row=3, column=1, sticky="we", padx=(8, 0))
        self.ip_entry = ttk.Entry(lf, width=18)
        self.ip_entry.grid(row=4, column=0, sticky="we", pady=(8, 0))
        ttk.Button(lf, text="Add IP", command=self.add_ip).grid(
            row=4, column=1, sticky="we", padx=(8, 0), pady=(8, 0))

        mf = ttk.Frame(panel)
        mf.grid(row=0, column=1, sticky="ne")
        ttk.Label(mf, text="MONITOR", style="Sec.TLabel").pack(anchor="w",
                                                               pady=(0, 6))
        self.viz = tk.Canvas(mf, width=self.viz_w, height=self.viz_h, bg="#08080a",
                             highlightthickness=1, highlightbackground=GOLD_LO)
        self.viz.pack()
        self.env_line = self.viz.create_line(0, self.top_h, self.viz_w,
                                             self.top_h, fill=LINE, width=2)
        bw = self.viz_w / N_BARS
        self.bars = [self.viz.create_rectangle(
            j * bw, self.viz_h, (j + 1) * bw - 1, self.viz_h,
            fill=BAR_REST, width=0) for j in range(N_BARS)]

        self.status = ttk.Label(panel, style="Status.TLabel",
                                wraplength=HEAD_W - 40, justify="left")
        self.status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _setup_style(self):
        s = ttk.Style()
        s.theme_use("clam")  # only built-in theme that honors custom colors
        s.configure(".", background=TOLEX, foreground=CREAM, font=FONT,
                    borderwidth=0, focuscolor=TOLEX)
        s.configure("TFrame", background=TOLEX)
        s.configure("TLabel", background=TOLEX, foreground=CREAM)
        s.configure("Sec.TLabel", background=TOLEX, foreground=GOLD, font=FONT_SEC)
        s.configure("Status.TLabel", background=TOLEX, foreground="#9a8f76",
                    font=FONT_SM)
        # beveled brass buttons that visibly sink when pressed
        s.configure("TButton", background=GOLD, foreground="#20160a",
                    font=("Segoe UI", 9, "bold"), padding=(10, 7),
                    borderwidth=3, relief="raised",
                    bordercolor=GOLD_LO, lightcolor=GOLD_HI, darkcolor=GOLD_LO)
        s.map("TButton",
              background=[("pressed", "#b3841f"), ("active", GOLD_HI)],
              relief=[("pressed", "sunken"), ("!pressed", "raised")],
              lightcolor=[("pressed", GOLD_LO)],   # flip the bevel so it looks
              darkcolor=[("pressed", GOLD_HI)],    # pushed in, not popped out
              foreground=[("pressed", "#000000")])
        s.configure("TEntry", fieldbackground=TOLEX2, foreground=CREAM,
                    insertcolor=CREAM, padding=6)

    # ---- power switch: latch in when running, momentary depress on click
    def _pwr_shift(self, px):
        d = px - self._pwr_off
        if d:
            self.head.move("powerbtn", 0, d)
            self._pwr_off = px

    def _recompute_pwr(self):
        running = bool(self.worker_thread and self.worker_thread.is_alive())
        self._pwr_shift((3 if running else 0) + (3 if self._pwr_pressed else 0))

    def _power_press(self, _e):
        self._pwr_pressed = True
        self._recompute_pwr()

    def _power_release(self, _e):
        self._pwr_pressed = False
        self._recompute_pwr()
        self.toggle()

    def _power_leave(self, _e):
        if self._pwr_pressed:  # dragged off before releasing — pop back, no toggle
            self._pwr_pressed = False
            self._recompute_pwr()

    def _snap_params(self, *_):
        self.params = dict(sensitivity=self.sensitivity.get(),
                           smooth=self.smooth.get(), floor=self.floor.get(),
                           flash=self.flash.get(),
                           max_bright=self.max_bright.get())

    def on_mode(self, *_):
        self.mode = MODES[self.mode_name.get()]
        self.smooth.set(self.mode["smooth"])
        self.sensitivity.set(self.mode["sens"])
        self.floor.set(self.mode["floor"])
        self.flash.set(self.mode["flash"])

    # ---- lights list
    def discover(self):
        self.status_text = "Discovering..."
        def run():
            ips = discover_lights()
            self.root.after(0, lambda: self._fill_lights(ips))
        threading.Thread(target=run, daemon=True).start()

    def _fill_lights(self, ips):
        existing = set(self.listbox.get(0, tk.END))
        for ip in ips:
            if ip not in existing:
                self.listbox.insert(tk.END, ip)
        self.listbox.selection_set(0, tk.END)
        self.status_text = f"Found {len(ips)} light(s)"

    def add_ip(self):
        ip = self.ip_entry.get().strip()
        if ip and ip not in self.listbox.get(0, tk.END):
            self.listbox.insert(tk.END, ip)
            self.listbox.selection_set(tk.END)
            self.ip_entry.delete(0, tk.END)

    def selected_ips(self):
        return [self.listbox.get(i) for i in self.listbox.curselection()]

    def save_lights(self):
        data = {"lights": list(self.listbox.get(0, tk.END)),
                "selected": self.selected_ips()}
        try:
            with open(LIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.status_text = f"Saved {len(data['lights'])} light(s)"
        except OSError as e:
            self.status_text = f"Save failed: {e}"

    def load_lights(self):
        try:
            with open(LIGHTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        selected = set(data.get("selected", []))
        for ip in data.get("lights", []):
            self.listbox.insert(tk.END, ip)
            if ip in selected:
                self.listbox.selection_set(tk.END)
        if data.get("lights"):
            self.status_text = f"Loaded {len(data['lights'])} saved light(s)"

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)

    # ---- start/stop
    def toggle(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            return
        ips = self.selected_ips()
        if not ips:
            self.status_text = "No lights selected"
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self.worker, args=(ips,),
                                              daemon=True)
        self.worker_thread.start()

    def worker(self, ips):
        sender = WizSender(ips)
        pa = pyaudio.PyAudio()
        try:
            stream, dev = open_loopback(pa)
        except Exception as e:
            self.status_text = f"Audio error: {e}"
            pa.terminate()
            sender.close()
            return
        self.status_text = f"Syncing {len(ips)} light(s) from: {dev['name']}"
        rate = int(dev["defaultSampleRate"])
        ch = dev["maxInputChannels"]
        state = new_map_state()
        last_sent = (0, 0, 0, 0)
        last_send_t = 0.0
        last_audio_t = time.time()
        try:
            while not self.stop_event.is_set():
                avail = stream.get_read_available()
                if avail >= CHUNK:
                    if avail >= 3 * CHUNK:
                        # fell behind: drop stale audio, else every beat
                        # lands late by the whole backlog from then on
                        stream.read(avail - CHUNK, exception_on_overflow=False)
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    mono = np.frombuffer(data, np.int16).reshape(-1, ch) \
                        .mean(axis=1) / 32768.0
                    last_audio_t = time.time()
                elif time.time() - last_audio_t > 0.1:
                    # loopback delivers nothing while the PC is silent; feed
                    # silence so lights settle and Stop stays responsive
                    time.sleep(CHUNK / rate)
                    mono = np.zeros(CHUNK)
                else:
                    time.sleep(0.002)
                    continue
                p = self.params
                r, g, b, dim = audio_to_color(
                    mono, rate, state,
                    sensitivity=p["sensitivity"],
                    smooth=p["smooth"],
                    mode=self.mode,
                    floor=p["floor"],
                    flash=p["flash"])
                dim = int(dim * p["max_bright"] / 100.0)
                self.current = (r, g, b, dim)
                # scroll visualiser buffers (whole-array reassign = atomic read)
                self.env_hist = np.append(self.env_hist[1:], state["env_norm"])
                self.beat_hist = np.append(self.beat_hist[1:],
                                           1.0 if state["onset"] else 0.0)
                self.spectrum = state["spectrum"]
                now = time.time()
                changed = max(abs(a - b_) for a, b_ in
                              zip((r, g, b, dim), last_sent)) >= 4
                # beats always go out at once, at full flash; the onset
                # refractory (120ms) already caps them at ~8/s
                due = state["onset"] \
                    or (now - last_send_t >= 1.0 / SEND_HZ
                        and (changed or now - last_send_t >= 1.0))
                if due:
                    sender.set_color(r, g, b, dim)
                    last_sent = (r, g, b, dim)
                    last_send_t = now
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
            sender.restore_white()
            sender.close()
            self.status_text = "Stopped (lights restored to warm white)"

    # ---- GUI poll
    def poll(self):
        r, g, b, _ = self.current
        running = bool(self.worker_thread and self.worker_thread.is_alive())
        color = f"#{r:02x}{g:02x}{b:02x}"
        w, top = self.viz_w, self.top_h

        # pilot jewel glows the live light colour while syncing
        lit = color if running and max(r, g, b) > 12 else LAMP_OFF
        self.head.itemconfig(self.lamp, fill=lit)
        self.head.itemconfig(self.glow, fill=lit if running else LAMP_OFF)
        self.head.itemconfig(self.power_txt, text="STOP" if running else "START")
        self._recompute_pwr()  # latch the switch in/out to match run state

        # top: scrolling loudness-envelope hills
        env = self.env_hist
        n = len(env)
        pts = []
        for i in range(n):
            pts += (i * w / (n - 1), top - env[i] * (top - 2))
        self.viz.coords(self.env_line, *pts)
        self.viz.itemconfig(self.env_line, fill=GOLD)

        # beat ticks over the envelope
        self.viz.delete("beat")
        for i in np.nonzero(self.beat_hist)[0]:
            x = i * w / (n - 1)
            self.viz.create_line(x, 0, x, top, fill=CREAM, tags="beat")

        # bottom: spectrum bars
        spec = self.spectrum
        for j, bar in enumerate(self.bars):
            v = spec[j] if j < len(spec) else 0.0
            y0 = self.viz_h - float(np.clip(v, 0, 1)) * (self.viz_h - top)
            x0, _, x1, _ = self.viz.coords(bar)
            self.viz.coords(bar, x0, y0, x1, self.viz_h)
            self.viz.itemconfig(bar, fill=GOLD)

        self.status.config(text=self.status_text)
        self.root.after(50, self.poll)

    def on_close(self):
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
