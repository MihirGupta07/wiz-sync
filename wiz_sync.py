"""WiZ Sync — syncs WiZ light colors to PC audio output (WASAPI loopback).

Run: python wiz_sync.py
"""
import colorsys
import functools
import json
import logging
import math
import os
import shutil
import socket
import sys
import threading
from collections import deque
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
import pyaudiowpatch as pyaudio

__version__ = "1.0.1"
WIZ_PORT = 38899
# saved lights + debug log: next to the script when run from source; when
# installed, in %LOCALAPPDATA%\WiZ Sync (the install folder may be read-only)
if getattr(sys, "frozen", False):
    _BASE = os.path.join(os.environ.get("LOCALAPPDATA")
                         or os.path.expanduser("~"), "WiZ Sync")
    os.makedirs(_BASE, exist_ok=True)
    _old = os.path.join(os.path.dirname(sys.executable), "lights.json")
    if os.path.exists(_old) and not os.path.exists(
            os.path.join(_BASE, "lights.json")):  # carry over pre-1.0 saves
        shutil.copy(_old, _BASE)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
LIGHTS_FILE = os.path.join(_BASE, "lights.json")
CHUNK = 512  # ~11ms hop at 44.1/48kHz; the FFT window stays WIN
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

# Two-layer model: COLOR follows the bass level (mode-independent heat
# gradient blue->red), BRIGHTNESS pulses on kicks scaled by beat strength and
# bass level, so builds pulse softer and a DROP (bass slams back) hits hardest.
# On top sit DJ-style section effects: builds whiten and pulse on snare rolls,
# the gap a build cuts to goes dark, and a drop lands as a held white impact.
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

WIN = 2048  # FFT window: ~21 Hz bins (5-6 in the kick band) at 44.1/48 kHz
_HANN = np.hanning(WIN)
_BAR_EDGES = np.logspace(np.log10(30), np.log10(8000), N_BARS + 1)
SILENCE_DB = -60.0  # below this nothing feeds the adaptive baselines

# ---- tuning. times in seconds, levels in dB. `python wiz_sync.py --replay
# song.mp3` prints every beat/drop/near-miss with these numbers to tune by.
KICK_HZ = (30.0, 150.0)      # kick + bass band
SNARE_HZ = (150.0, 8000.0)   # broadband transients: snares, claps, rolls
FLUX_LAG = 2                 # flux vs the window 2 hops back (windows overlap)
ONSET_HIST_S = 1.5           # adaptive-threshold history
ONSET_RISE_S = 0.05          # flash may keep rising this long after a kick
KICK_GAP_S = 0.2            # kick refractory: kills double triggers, keeps
                             # half-beat bass pumps (no tempo grid)
SNARE_GAP_S = 0.06
DROP_HIST_S = 8.0            # bass/energy history a drop is measured against
DROP_RECENT_S = 1.0          # newest span kept out of that baseline
DROP_BASS_DB = 12.0          # bass rise over baseline that scores full
DROP_ENERGY_DB = 6.0         # broadband rise over baseline that scores full
DROP_MIN_BASS = 0.6          # gate: bass score needed (~7 dB) - energy alone
                             # (risers, snare rolls) never makes a drop
DROP_SCORE = 0.6             # trigger: 0.6*bass + 0.4*energy + 0.2*rhythm
DROP_CONFIRM_S = 0.15        # score held this long with no kick -> fire anyway
DROP_GAP_S = 10.0            # min time between drops, and from a song's start
DROP_HOLD_S = 4.0            # half-life of the post-drop punch boost
IMPACT_HOLD_S = 0.30         # white impact held so slow-fading bulbs show it
BUILD_RAMP_S = 8.0           # build tension ramps 0->1 over this
BLACKOUT = True              # go dark in the gap a build cuts to before a drop
BLACKOUT_DB = 12.0           # energy dip below its recent level = that gap
                             # (riser pauses dip ~8 dB; a real cut-out more)
BLACKOUT_MAX_S = 1.0         # release the blackout if no drop follows

log = logging.getLogger("wiz_sync")


@functools.lru_cache(maxsize=4)
def _bands(rate):
    """Bin masks + visualiser bucket map for one sample rate (cached)."""
    f = np.fft.rfftfreq(WIN, 1.0 / rate)
    # only bins inside [30,8000) feed the bars so DC and >8kHz don't pile up
    in_bars = (f >= _BAR_EDGES[0]) & (f < _BAR_EDGES[-1])
    return {"kick": (f >= KICK_HZ[0]) & (f < KICK_HZ[1]),
            "snare": (f >= SNARE_HZ[0]) & (f < SNARE_HZ[1]),
            "energy": (f >= 30) & (f < 16000), "bars": in_bars,
            "bar_idx": np.searchsorted(_BAR_EDGES, f[in_bars], side="right") - 1}


def _detector(n_hist):
    return {"hist": deque(maxlen=n_hist), "peak": 1e-6, "since": math.inf}


def _detect(d, flux, k, gap_hops, peak_decay, abs_floor=0.05):
    """Adaptive onset: flux above median + k robust sigmas (MAD) of the recent
    past, which the beats themselves barely move. Returns strength 0..1 vs the
    peak *before* this frame (so a new loudest beat reads 1.0), or None."""
    d["since"] += 1
    hit = None
    if len(d["hist"]) >= 10:
        h = np.fromiter(d["hist"], float)
        med = float(np.median(h))
        sigma = 1.4826 * float(np.median(np.abs(h - med))) + 1e-6
        if (flux > med + k * sigma and flux > abs_floor
                and flux > 0.1 * d["peak"] and d["since"] >= gap_hops):
            hit = float(np.clip(flux / d["peak"], 0.0, 1.0))
            d["since"] = 0
    d["peak"] = max(flux, d["peak"] * peak_decay, 1e-6)
    d["hist"].append(flux)
    return hit


def new_map_state():
    return {"t": 0.0, "buf": np.zeros(WIN), "logs": deque(maxlen=FLUX_LAG + 1),
            "kick": None, "snare": None, "kicks": deque(), "snares": deque(),
            "hist_bass": None, "hist_energy": None,  # sized on first chunk
            # bass_lo starts at +inf / bass_hi at -inf so both snap to level 1
            "bass": None, "bass_lo": math.inf, "bass_hi": -math.inf,
            "bass_p": None, "energy_p": None, "energy_hi": -math.inf,
            "env_norm": 0.0, "drop": 0.0, "drop_hit": False, "drop_score": 0.0,
            "since_drop": 0.0, "quiet_t": 0.0, "confirm_t": 0.0, "miss_t": 0.0,
            "tension": 0.0, "blackout_t": None, "blackout_e": 0.0, "impact_until": -math.inf,
            "event": None, "onset": False, "kick_t": -math.inf, "kick_s": 0.0,
            "rgb": np.zeros(3), "dim": 10.0, "hue": 0.0,
            "spec_peak": np.full(N_BARS, 1e-6), "spectrum": np.zeros(N_BARS)}


def audio_to_color(mono, rate, state, sensitivity=1.0, smooth=None, mode=None,
                   floor=None, flash=None):
    """Map one mono float chunk (-1..1) to (r, g, b, dimming). Mutates state.

    COLOR = bass level -> heat gradient (or hue cycle in Disco), whitened by
    build tension. BRIGHTNESS = kick flash (strength- and bass-scaled) decaying
    to the floor; snare pulses during builds; dark in a pre-drop gap; a held
    white impact on the drop, then full flashes over darker gaps.
    Sets state["event"] ("drop" | "beat" | None) for send priority,
    state["onset"]/["drop_hit"], and env_norm/spectrum for the visualiser.
    """
    mode = mode or MODES[DEFAULT_MODE]
    floor = mode["floor"] if floor is None else floor
    flash = mode["flash"] if flash is None else flash
    smooth = mode["smooth"] if smooth is None else smooth
    hop = len(mono)
    dt = hop / rate
    state["t"] += dt
    t = state["t"]
    b = _bands(rate)
    if state["kick"] is None:
        n = max(10, int(ONSET_HIST_S / dt))
        state["kick"], state["snare"] = _detector(n), _detector(n)
        state["hist_bass"] = deque(maxlen=int(DROP_HIST_S / dt))
        state["hist_energy"] = deque(maxlen=int(DROP_HIST_S / dt))

    # slide a fixed WIN window by each hop: fine low-freq resolution at any hop
    buf = np.concatenate([state["buf"], mono])[-WIN:]
    state["buf"] = buf
    mag = np.abs(np.fft.rfft(buf * _HANN)) * (4.0 / WIN)  # ~= sine amplitude

    # loudness only gates silence (a mastered track is about as loud in the
    # build as in the drop, so it can't drive the show). per-chunk, not
    # smoothed: a lagging gate lets the first ~0.5s of silence drag baselines
    rms = float(np.sqrt(np.mean(mono ** 2)))
    audible = 20.0 * math.log10(max(rms, 1e-9)) > SILENCE_DB
    state["quiet_t"] = 0.0 if audible else state["quiet_t"] + dt

    # ---- onsets: log-magnitude spectral flux, half-wave rectified per bin
    # (a rising bin can't be cancelled by a falling one), robust threshold
    logm = np.log1p(100.0 * mag)
    logs = state["logs"]
    prev = logs[0] if len(logs) == logs.maxlen else None
    logs.append(logm)
    kick_flux = snare_flux = 0.0
    if prev is not None:
        rise = np.maximum(logm - prev, 0.0)
        kick_flux = float(rise[b["kick"]].mean())
        snare_flux = float(rise[b["snare"]].mean())
    k = float(np.clip(4.0 / max(sensitivity, 0.1), 1.5, 10.0))  # lower = easier
    peak_decay = 0.5 ** (dt / 3.0)
    kick_s = _detect(state["kick"], kick_flux, k, KICK_GAP_S / dt, peak_decay)
    snare_s = _detect(state["snare"], snare_flux, k, SNARE_GAP_S / dt,
                      peak_decay)
    onset = kick_s is not None
    kicks, snares = state["kicks"], state["snares"]
    if onset:
        kicks.append(t)
        state["kick_t"], state["kick_s"] = t, kick_s
    if snare_s is not None:
        snares.append(t)
    for q in (kicks, snares):
        while q and q[0] < t - 2.0:
            q.popleft()
    kick_rate, snare_rate = len(kicks) / 2.0, len(snares) / 2.0

    # ---- bass level (peak-hold, dB) -> COLOR + flash headroom. instant
    # attack catches each kick (a gradual one would crawl up from silence
    # through fake "quiet" levels), 0.5s release holds between them
    bass_pow = float(np.mean(mag[b["kick"]] ** 2))
    energy_pow = float(np.sum(mag[b["energy"]] ** 2))
    bdb = 10.0 * math.log10(bass_pow + 1e-12)
    if state["bass"] is None or bdb > state["bass"]:
        state["bass"] = bdb
    else:
        state["bass"] += (1 - 0.5 ** (dt / 0.5)) * (bdb - state["bass"])
    bass = state["bass"]
    # floor/ceiling skip silence, else a track gap drags them down and the
    # next song reads as max bass for minutes
    if audible:
        if bass < state["bass_lo"]:
            state["bass_lo"] = bass
        else:
            state["bass_lo"] += (bass - state["bass_lo"]) * dt / 40.0  # creep
    state["bass_hi"] = max(bass, state["bass_hi"] - 0.2 * dt)  # 0.2 dB/s fall
    rng = max(state["bass_hi"] - state["bass_lo"], 6.0)
    norm = float(np.clip((bass - state["bass_lo"]) / rng, 0.0, 1.0))
    state["env_norm"] = norm

    # ---- section level: 0.2s-smoothed bass + broadband energy vs the last
    # DROP_HIST_S (newest second excluded, 25th pct so kicks don't lift it)
    a = 1 - 0.5 ** (dt / 0.2)
    for key, pw in (("bass_p", bass_pow), ("energy_p", energy_pow)):
        state[key] = pw if state[key] is None else state[key] + a * (pw - state[key])
    bass_db = 10.0 * math.log10(state["bass_p"] + 1e-12)
    energy_db = 10.0 * math.log10(state["energy_p"] + 1e-12)
    inst_energy_db = 10.0 * math.log10(energy_pow + 1e-12)
    hb, he = state["hist_bass"], state["hist_energy"]
    if audible:
        hb.append(bass_db)
        he.append(energy_db)
        state["energy_hi"] = max(energy_db, state["energy_hi"] - 0.2 * dt)
    recent = int(DROP_RECENT_S / dt)
    d_bass = d_energy = s_bass = score = 0.0
    if len(hb) > recent + int(2.0 / dt):  # need 2s+ of audible baseline
        base_b = float(np.percentile(np.fromiter(hb, float)[:-recent], 25))
        base_e = float(np.percentile(np.fromiter(he, float)[:-recent], 25))
        d_bass, d_energy = bass_db - base_b, energy_db - base_e
        s_bass = float(np.clip(d_bass / DROP_BASS_DB, 0.0, 1.0))
        s_energy = float(np.clip(d_energy / DROP_ENERGY_DB, 0.0, 1.0))
        s_rhythm = float(np.clip(max(kick_rate, 0.5 * snare_rate) / 1.5, 0, 1))
        score = 0.6 * s_bass + 0.4 * s_energy + 0.2 * s_rhythm
    state["drop_score"] = score

    # ---- DROP: bass (+ energy, rhythm) jumps over the recent section. fires
    # on a kick, or after DROP_CONFIRM_S if the kick detector missed it
    # a new song (>2s silence) restarts the clock: its first kick isn't a drop
    state["since_drop"] = 0.0 if state["quiet_t"] > 2.0 \
        else state["since_drop"] + dt
    state["drop"] *= 0.5 ** (dt / DROP_HOLD_S)
    armed = (audible and s_bass >= DROP_MIN_BASS and norm >= 0.6
             and score >= DROP_SCORE and state["since_drop"] >= DROP_GAP_S)
    state["confirm_t"] = state["confirm_t"] + dt if armed else 0.0
    hit = armed and (onset or state["confirm_t"] >= DROP_CONFIRM_S)

    # ---- BUILD tension: bass well under the song's ceiling while the mix
    # stays full and rhythmic (snare roll, synth riser) -> ramps up
    bass_mid_norm = (bass_db - state["bass_lo"]) / rng
    building = (audible and bass_mid_norm < 0.45
                and energy_db > state["energy_hi"] - 8.0
                and (snare_rate >= 1.0 or kick_rate >= 1.0))
    if building:
        state["tension"] = min(1.0, state["tension"] + dt / BUILD_RAMP_S)
    else:
        state["tension"] *= 0.5 ** (dt / 1.0)
    tension = state["tension"]

    # ---- BLACKOUT: a built-up mix suddenly cutting out = the pre-drop gap
    if (BLACKOUT and state["blackout_t"] is None and tension >= 0.4
            and inst_energy_db < energy_db - BLACKOUT_DB):
        state["blackout_t"], state["blackout_e"] = t, energy_db
        log.debug("%8.3f blackout tension=%.2f dip=%.1fdB", t, tension,
                  energy_db - inst_energy_db)
    # released by the drop, by the music coming back without one, or timeout
    if state["blackout_t"] is not None and (
            hit or t - state["blackout_t"] > BLACKOUT_MAX_S
            or inst_energy_db > state["blackout_e"] - 3.0):
        state["blackout_t"] = None
    blackout = state["blackout_t"] is not None

    if hit:
        state.update(drop=1.0, since_drop=0.0, confirm_t=0.0, tension=0.0,
                     impact_until=t + IMPACT_HOLD_S)
    state["onset"], state["drop_hit"] = onset, hit
    state["event"] = "drop" if hit else ("beat" if onset else None)
    drop = state["drop"]

    if log.isEnabledFor(logging.DEBUG):
        if hit:
            log.debug("%8.3f DROP via=%s score=%.2f bass=%.1fdB dBass=%+.1f "
                      "dEnergy=%+.1f kicks/s=%.1f", t,
                      "kick" if onset else "confirm", score, bass_db, d_bass,
                      d_energy, kick_rate)
        elif score >= 0.45 and t - state["miss_t"] >= 0.5:
            state["miss_t"] = t
            why = ("gap" if state["since_drop"] < DROP_GAP_S else
                   "bass<min" if s_bass < DROP_MIN_BASS else
                   "ceiling" if norm < 0.6 else
                   "score" if score < DROP_SCORE else "waiting")
            log.debug("%8.3f near-miss(%s) score=%.2f dBass=%+.1f dEnergy=%+.1f",
                      t, why, score, d_bass, d_energy)
        if onset:
            log.debug("%8.3f beat strength=%.2f bass=%.2f score=%.2f "
                      "tension=%.2f", t, kick_s, norm, score, tension)

    # ---- spectrum for the visualiser bars (log-spaced, auto-gained).
    # sum into buckets so narrow low-freq buckets with no bins read 0 (not NaN)
    spec = np.zeros(N_BARS)
    np.add.at(spec, b["bar_idx"], mag[b["bars"]])
    state["spec_peak"] = np.maximum(spec, state["spec_peak"] * 0.5 ** (dt / 1.5))
    state["spectrum"] = spec / np.maximum(state["spec_peak"], 1e-6)

    # ---- COLOR
    ease = 1 - 0.5 ** (dt / 0.04)  # ~40ms colour glide
    if mode["color"] == "cycle":
        if onset:
            state["hue"] = (state["hue"] + mode["jump"]) % 360.0
        rgb_raw = np.array(colorsys.hsv_to_rgb(state["hue"] / 360.0, 1, 1)) * 255.0
        if onset:  # snap on beat
            state["rgb"] = rgb_raw
        else:
            state["rgb"] += ease * (rgb_raw - state["rgb"])
    else:  # heat gradient: no bass=blue (hue 240) -> full bass=red (hue 0)
        rgb_raw = np.array(colorsys.hsv_to_rgb((1 - norm) * 240 / 360, 1, 1)) * 255.0
        state["rgb"] += ease * (rgb_raw - state["rgb"])
    if hit:  # white impact, easing back to colour once the hold ends
        state["rgb"] = np.full(3, 255.0)
    rgb = state["rgb"]
    if t < state["impact_until"]:
        rgb = np.full(3, 255.0)
    elif tension > 0:  # builds wash toward white
        rgb = rgb + 0.6 * tension * (255.0 - rgb)

    # ---- BRIGHTNESS: beat flash decaying to the floor
    # smooth -> flash half-life in seconds (30ms..600ms), so the throb is
    # independent of hop size and long enough for bulbs fed at SEND_HZ to show
    smooth = min(max(smooth, 0.0), 0.95)
    half_life = 0.03 + 0.6 * smooth
    decay = 1.0 - 0.5 ** (dt / half_life)
    # after a drop the gaps go darker for contrast; a build lifts them
    floor_now = 0.0 if blackout else \
        min(60.0, floor * (1.0 - 0.7 * drop) + 30.0 * tension)
    if hit or t < state["impact_until"]:
        state["dim"] = 100.0
    elif blackout:
        state["dim"] = 0.0
    elif t - state["kick_t"] < ONSET_RISE_S:
        # a detected beat already cleared the adaptive threshold, so it always
        # pops; strength modulates the top half (soft beat 50% -> hard 100%) and
        # bass level sets the headroom (bass-less build ~35% -> drop 100%)
        # right after a drop every beat hits full, dynamics return as it fades
        boost = min(1.0, 2.0 * drop)  # full for one half-life, then fades
        headroom = max(boost, 0.35 + 0.65 * norm)
        level = floor_now + (100.0 - floor_now) * flash \
            * max(0.5 + 0.5 * state["kick_s"], boost) * headroom
        prev = state["dim"]
        state["dim"] = min(100.0, max(prev, level))
        # the window's newest hop sits in the Hann taper, so the bass level
        # (headroom) catches up a hop or two after the onset: let the flash
        # keep rising, and send each rise straight away like the beat itself
        if state["event"] is None and state["dim"] >= prev + 4:
            state["event"] = "beat"
    elif snare_s is not None and tension >= 0.2:
        # snare rolls pulse harder as the build tightens
        level = floor_now + (100.0 - floor_now) * flash * 0.7 * tension \
            * (0.5 + 0.5 * snare_s)
        state["dim"] = min(100.0, max(state["dim"], level))
    else:
        state["dim"] += decay * (floor_now - state["dim"])
    state["dim"] = float(np.clip(state["dim"], floor_now, 100.0))

    r, g, b_ = np.clip(rgb, 0, 255)
    return int(r), int(g), int(b_), int(state["dim"])


def replay(path, mode, rate=44100):
    """Run the live detector over an audio file in real-time-sized chunks (no
    bulbs) and log every beat/drop/near-miss: tune the constants against it.
    Decodes with ffmpeg, which must be on PATH."""
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1",
                          "-ar", str(rate), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.float32).astype(float)
    state = new_map_state()
    drops = []
    for i in range(len(x) // CHUNK):
        audio_to_color(x[i * CHUNK:(i + 1) * CHUNK], rate, state,
                       sensitivity=mode["sens"], mode=mode)
        if state["drop_hit"]:
            drops.append(round(state["t"], 1))
    print(f"{len(drops)} drop(s) at {drops}s")


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
        ico = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(
            os.path.abspath(__file__))), "wiz-sync.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)
        root.configure(bg=TOLEX)
        root.resizable(False, False)
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.current = (0, 0, 0, 10)  # last computed color, read by GUI poll
        self.env_hist = np.zeros(280)  # scrolling bass level (0..1), ~3s
        self.beat_hist = np.zeros(280)  # 1.0 where a beat fired, else 0
        self.spectrum = np.zeros(N_BARS)  # latest spectrum bars (0..1)
        self.status_text = "Idle"
        self._found = None  # discovery result, handed to the GUI thread by poll
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
        self.delay_ms = tk.IntVar(value=0)  # hold lights back to match speakers
        # the worker reads this plain snapshot, never the tk vars or self.mode:
        # a tk call from another thread blocks until the GUI thread services
        # it, which stalls audio reads and makes the lights lag the beat. the
        # dict is replaced whole, so the worker never sees a half-update
        for v in (self.sensitivity, self.max_bright, self.smooth, self.floor,
                  self.flash, self.delay_ms):
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
        # Bluetooth speakers play ~150-300ms late; WiZ bulbs react in ~50-100.
        # if the lights land before the beat you hear, raise this
        ttk.Label(lf, text="Light delay (ms)").grid(row=5, column=0, sticky="w",
                                                    pady=(8, 0))
        ttk.Spinbox(lf, from_=0, to=500, increment=10, width=6,
                    textvariable=self.delay_ms).grid(
            row=5, column=1, sticky="we", padx=(8, 0), pady=(8, 0))

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
        try:  # a half-typed spinbox value reads as an error
            delay = max(0, min(500, int(self.delay_ms.get())))
        except (tk.TclError, ValueError):
            delay = self.params["delay"] if hasattr(self, "params") else 0
        self.params = dict(sensitivity=self.sensitivity.get(),
                           smooth=self.smooth.get(), floor=self.floor.get(),
                           flash=self.flash.get(),
                           max_bright=self.max_bright.get(),
                           mode=self.mode, delay=delay / 1000.0)

    def on_mode(self, *_):
        self.mode = MODES[self.mode_name.get()]
        self.smooth.set(self.mode["smooth"])
        self.sensitivity.set(self.mode["sens"])
        self.floor.set(self.mode["floor"])
        self.flash.set(self.mode["flash"])
        self._snap_params()

    # ---- lights list
    def discover(self):
        self.status_text = "Discovering..."
        def run():  # tk is touched only on the GUI thread: poll() picks it up
            self._found = discover_lights()
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
                "selected": self.selected_ips(),
                "delay_ms": int(self.params["delay"] * 1000)}
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
        self.delay_ms.set(int(data.get("delay_ms", 0)))
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
        pending = deque()  # (send_at, rgbd, repeats) waiting out the delay
        try:
            while not self.stop_event.is_set():
                now = time.time()
                while pending and pending[0][0] <= now:
                    _, pkt, reps = pending.popleft()
                    for _ in range(reps):
                        sender.set_color(*pkt)
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
                p = self.params  # one read: a consistent snapshot
                r, g, b, dim = audio_to_color(
                    mono, rate, state,
                    sensitivity=p["sensitivity"],
                    smooth=p["smooth"],
                    mode=p["mode"],
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
                # beats and drops always go out at once, at full flash (the
                # kick refractory caps them at 5/s); the rest is rate-limited.
                # a drop goes twice: UDP has no retry and it's the one that
                # must not be lost
                ev = state["event"]
                due = ev is not None \
                    or (now - last_send_t >= 1.0 / SEND_HZ
                        and (changed or now - last_send_t >= 1.0))
                if due:
                    pkt, reps = (r, g, b, dim), 2 if ev == "drop" else 1
                    if p["delay"] > 0:
                        pending.append((now + p["delay"], pkt, reps))
                    else:
                        for _ in range(reps):
                            sender.set_color(*pkt)
                    last_sent = pkt
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
        if self._found is not None:
            ips, self._found = self._found, None
            self._fill_lights(ips)
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

        # top: scrolling bass-level hills (what drives the colour)
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
    import argparse
    ap = argparse.ArgumentParser(description="WiZ Sync")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--debug", action="store_true",
                    help="log beats/drops/near-misses to wiz_sync_debug.log")
    ap.add_argument("--replay", metavar="AUDIO",
                    help="run the detector over a file (needs ffmpeg), no GUI")
    ap.add_argument("--mode", default=DEFAULT_MODE, choices=list(MODES))
    args = ap.parse_args()
    if args.replay:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s")
        replay(args.replay, MODES[args.mode])
    else:
        if args.debug or os.environ.get("WIZ_SYNC_DEBUG"):
            # the windowed exe has no console, so the log goes to a file
            logging.basicConfig(
                level=logging.DEBUG, format="%(asctime)s %(message)s",
                filename=os.path.join(_BASE, "wiz_sync_debug.log"))
        root = tk.Tk()
        App(root)
        root.mainloop()
