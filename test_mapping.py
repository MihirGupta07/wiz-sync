"""Self-check for the audio->color mapping. Run: python test_mapping.py"""
import numpy as np
from wiz_sync import (audio_to_color, new_map_state, MODES, N_BARS, CHUNK,
                      IMPACT_HOLD_S, _detect, _detector)

RATE = 48000
SEC = RATE / CHUNK  # hops per second
_n = [0]  # running sample index so tones stay phase-continuous across chunks


def tone(freq, amp):
    t = (_n[0] + np.arange(CHUNK)) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


def run(chunk, state, mode="Party", **kw):
    _n[0] += CHUNK
    m = MODES[mode]
    kw.setdefault("sensitivity", m["sens"])
    return audio_to_color(chunk, RATE, state, mode=m, **kw)


def hops(s):
    return int(s * SEC)


# COLOR follows the bass level: loud bass -> warm, then quieter -> cool
state = new_map_state()
for _ in range(hops(1.7)):
    r, g, b, _ = run(tone(60, 0.8), state, "Pop")
assert r > b, f"loud bass should be warm (red>blue): {(r, g, b)}"
for _ in range(hops(6)):
    r, g, b, _ = run(tone(60, 0.02), state, "Pop")
assert b > r, f"quiet bass should be cool (blue>red): {(r, g, b)}"

# a silent gap between tracks must not drag the floor down - quiet bass
# afterwards should still read cool, not pinned warm
for _ in range(hops(10)):
    run(np.zeros(CHUNK), state, "Pop")
for _ in range(hops(1.7)):
    run(tone(60, 0.8), state, "Pop")
for _ in range(hops(1.3)):
    r, g, b, _ = run(tone(60, 0.02), state, "Pop")
assert b > r, f"quiet after a silent gap should be cool: {(r, g, b)}"


# SMOOTH sets how long the flash throbs: Jazz should glow far longer than Rave
def throb_hops(name):
    st = new_map_state()
    m = MODES[name]
    for i in range(hops(1)):  # one kick at 0.5s over quiet
        _, _, _, dim = run(tone(55, 0.8) if i == hops(0.5) else tone(60, 0.03),
                           st, name)
    n = 0
    while dim > m["floor"] + 10 and n < 400:
        _, _, _, dim = run(tone(60, 0.03), st, name)
        n += 1
    return n


rave, jazz = throb_hops("Rave"), throb_hops("Jazz")
assert jazz > 3 * max(rave, 1), f"Smooth should lengthen the throb: {rave} vs {jazz}"

# BRIGHTNESS keeps beat dynamics: a hard kick flashes brighter than a soft one
state = new_map_state()
strong_dims, weak_dims = [], []
beat, klen = hops(0.33), hops(0.08)  # ~80ms kicks, peak read over each
for i in range(hops(5)):
    ph = i % (2 * beat)
    amp = 0.9 if ph < klen else 0.28 if beat <= ph < beat + klen else 0.03
    _, _, _, dim = run(tone(60, amp), state, "Techno")
    if i >= hops(1) and ph in (0, beat):
        (strong_dims if ph == 0 else weak_dims).append(0)
    if i >= hops(1) and amp > 0.03 and (strong_dims or weak_dims):
        lst = strong_dims if ph < klen else weak_dims
        if lst:
            lst[-1] = max(lst[-1], dim)
assert max(strong_dims) > max(weak_dims) + 10, \
    f"hard kick should out-flash soft kick: {max(strong_dims)} vs {max(weak_dims)}"
assert max(strong_dims) > 80, f"detected beats should pop: {max(strong_dims)}"

# beat detection: kicks over a sustained bassline fire one onset each, on-beat
state = new_map_state()
period = hops(0.5)
fired = []
for i in range(hops(2.5)):
    c = tone(80, 0.25) + (tone(55, 0.8) if i % period == 0 else 0)
    run(c, state, "Techno")
    if i >= hops(0.25) and state["onset"]:
        fired.append(i)
expect = range(period, hops(2.5), period)
assert len(fired) == len(expect), f"expected {len(expect)} onsets: {fired}"
assert all(min(abs(f - m) for m in expect) <= 2 for f in fired), \
    f"onsets off-beat: {fired}"

# strength is measured against the peak BEFORE this frame: a new loudest beat
# reads 1.0 (not flux/itself after updating), a half-size one reads ~0.5
d = _detector(50)
for _ in range(40):
    _detect(d, 0.01, 3.0, 5, 1.0)
s1 = _detect(d, 1.0, 3.0, 5, 1.0)
for _ in range(10):
    _detect(d, 0.01, 3.0, 5, 1.0)
s2 = _detect(d, 0.5, 3.0, 5, 1.0)
assert s1 == 1.0 and abs(s2 - 0.5) < 0.01, f"strength vs previous peak: {s1} {s2}"
# ...and the refractory gap swallows a second hit right after the first
_detect(d, 0.01, 3.0, 5, 1.0)
assert _detect(d, 1.0, 3.0, 5, 1.0) is None, "refractory should block re-trigger"

# Disco cycles the hue on beats (the one color exception)
state = new_map_state()
for i in range(hops(2.5)):
    run(tone(55, 0.8) if i % period == 0 else tone(60, 0.03), state, "Disco")
assert state["hue"] > 0, "Disco should advance hue on beats"


# DROP: groove (kick+bass) -> bass-light build with a snare roll -> kick+bass
# slams back fires exactly one drop, on the build->drop transition (not at the
# song's start, not during the build), and drop beats out-flash build beats.
# A live detector only knows the bass ceiling once it's heard it, hence the
# opening groove, as in real songs.
def song(drop_kick=True, gap=0.0, build_s=14):
    st = new_map_state()
    build_at, drop_at = hops(12), hops(12 + build_s)
    hits, build_dims, drop_dims, dims, tens = [], [], [], [], 0.0
    noise = np.random.default_rng(0)
    for i in range(drop_at + hops(6)):
        on = i % period == 0
        if build_at <= i < drop_at - hops(gap):
            c = tone(400, 0.3) + (tone(55, 0.08) if on else 0)
            if i % (period // 2) == 0:  # snare roll: broadband bursts
                c = c + noise.normal(0, 0.3, CHUNK)
        elif build_at <= i < drop_at:  # the pre-drop cut-out
            c = tone(400, 0.003)
        else:
            c = tone(400, 0.3) + tone(80, 0.3)
            if on and (drop_kick or i < drop_at):
                c = c + tone(55, 0.8)
        _, _, _, dim = run(c, st)
        dims.append(dim)
        if build_at <= i < drop_at:
            tens = max(tens, st["tension"])
        if st["drop_hit"]:
            hits.append(i)
        if st["onset"] and i >= build_at + hops(2):
            (build_dims if i < drop_at else drop_dims).append(dim)
    return hits, build_dims, drop_dims, dims, drop_at, tens, st


hits, build_dims, drop_dims, dims, drop_at, tens, state = song()
assert len(hits) == 1 and drop_at <= hits[0] <= drop_at + hops(0.4), \
    f"expected one drop at hop ~{drop_at}: {hits}"
assert np.mean(drop_dims) > np.mean(build_dims) + 20, \
    f"drop should out-flash build: {np.mean(drop_dims):.0f} vs {np.mean(build_dims):.0f}"
assert tens > 0.5, f"a long build should build tension: {tens:.2f}"
# the impact is held full long enough for a slow-fading bulb to show it
held = dims[hits[0]:hits[0] + hops(IMPACT_HOLD_S) - 1]
assert min(held) == 100, f"impact should hold at 100: {held}"

# a drop whose kick the detector misses (bassline only) still fires via the
# confirm path
hits, *_ = song(drop_kick=False)
assert len(hits) == 1 and drop_at <= hits[0] <= drop_at + hops(0.6), \
    f"missed-kick drop should still fire: {hits}"

# a real cut-out before the drop goes dark, then the drop lands
hits, _, _, dims, drop_at, _, _ = song(gap=0.6)
dark = dims[drop_at - hops(0.4):drop_at]
assert max(dark) <= 15, f"pre-drop gap should black out: {dark}"
assert len(hits) == 1 and dims[hits[0]] == 100, f"drop after blackout: {hits}"

# spectrum output is the right shape for the visualiser bars
assert len(state["spectrum"]) == N_BARS

print("all mapping checks passed")

# bulb model -> what it can show (moduleName from getSystemConfig)
from wiz_sync import bulb_kind
assert bulb_kind("ESP25_SHRGB_01") == "Colour"
assert bulb_kind("ESP01_SHRGBC1_31") == "Colour"
assert bulb_kind("ESP01_SHTW1C_31") == "White only"
assert bulb_kind("ESP06_SHDW9_01") == "White only"
assert bulb_kind("ESP10_SOCKET_06") == ""
assert bulb_kind("") == ""
print("bulb kind checks passed")

# ---- media files
from wiz_sync import LightOut, parse_probe, video_size, video_to_color

# ffmpeg's banner -> what's in the file; a song's cover art isn't video
VIDEO = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':
  Duration: 00:03:21.50, start: 0.000000, bitrate: 2687 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080 [SAR 1:1 DAR 16:9], 2609 kb/s, 23.98 fps, 23.98 tbr, 24k tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 128 kb/s (default)"""
SONG = """Input #0, mp3, from 'song.mp3':
  Duration: 00:04:05.03, start: 0.025056, bitrate: 320 kb/s
  Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 320 kb/s
  Stream #0:1: Video: mjpeg (Baseline), yuvj420p(pc, bt470bg/unknown/unknown), 600x600 [SAR 1:1 DAR 1:1], 90k tbr, 90k tbn (attached pic)"""
v, s_ = parse_probe(VIDEO), parse_probe(SONG)
assert v == dict(duration=201.5, audio=True, video=True, w=1920, h=1080,
                 fps=23.98), v
assert s_["audio"] and not s_["video"] and abs(s_["duration"] - 245.03) < 1e-6
assert not any(parse_probe("song.txt: Invalid data found").values())

# frames fit the screen box, keep their shape, never upscale, even sides
assert video_size(1920, 1080) == (640, 360)
assert video_size(1080, 1920) == (202, 360)
assert video_size(320, 241) == (320, 240)


def frame(rgb, h=72, w=128):
    return np.tile(np.array(rgb, np.uint8), (h, w, 1))


# ambient colour is the scene's vivid colour, not the grey around it
st = {}
f = frame((120, 120, 120))
f[20:50, 40:90] = (200, 20, 20)  # a red car on grey tarmac
r, g, b, dim = video_to_color(f, 1 / 24, st)
assert r > 200 and g < 120 and b < 120, f"vivid colour should win: {(r, g, b)}"
# a grey scene reads white, a dark scene dims down, a bright one up
r, g, b, _ = video_to_color(frame((128, 128, 128)), 1 / 24, {})
assert min(r, g, b) > 240, f"grey scene should be white: {(r, g, b)}"
dark = video_to_color(frame((10, 10, 40)), 1 / 24, {})[3]
bright = video_to_color(frame((240, 240, 200)), 1 / 24, {})[3]
assert dark < 30 and bright > 90, f"dimming should follow the picture: {dark} {bright}"
# small changes glide, a scene cut snaps
st = {}
video_to_color(frame((0, 0, 200)), 1 / 24, st)
r, _, b, _ = video_to_color(frame((20, 0, 200)), 1 / 24, st)
assert not st["cut"]
r, _, b, _ = video_to_color(frame((220, 0, 0)), 1 / 24, st)
assert st["cut"] and r > 200 and b < 20, f"scene cut should snap: {(r, b)}"


# light packets: events at once, the rest rate-limited, `at` held back
class FakeSender:
    def __init__(self):
        self.sent = []

    def set_color(self, *pkt):
        self.sent.append(pkt)


fs = FakeSender()
lo = LightOut(fs)
lo.push((255, 0, 0, 50), None, 10.0)
lo.push((0, 255, 0, 50), None, 10.01)  # too soon after the last send
assert fs.sent == [(255, 0, 0, 50)], fs.sent
lo.push((0, 0, 255, 100), "drop", 10.02)  # events skip the rate limit, x2
assert fs.sent[1:] == [(0, 0, 255, 100)] * 2, fs.sent
lo.push((9, 9, 9, 20), "beat", 10.03, at=10.2)
lo.flush(10.1)
assert len(fs.sent) == 3, "held packet went out early"
lo.flush(10.2)
assert fs.sent[-1] == (9, 9, 9, 20), fs.sent
print("media checks passed")
