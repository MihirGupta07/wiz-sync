"""Self-check for the audio->color mapping. Run: python test_mapping.py"""
import numpy as np
from wiz_sync import audio_to_color, new_map_state, MODES, N_BARS, CHUNK

RATE = 48000
t = np.arange(CHUNK) / RATE


def tone(freq, amp):
    return amp * np.sin(2 * np.pi * freq * t)


# COLOR follows the loudness envelope: loud -> warm (red), then quieting -> cool
state = new_map_state()
for _ in range(80):
    r, g, b, _ = audio_to_color(tone(440, 0.8), RATE, state, mode=MODES["Pop"])
assert r > b, f"loud audio should be warm (red>blue): {(r, g, b)}"
for _ in range(300):
    r, g, b, _ = audio_to_color(tone(440, 0.02), RATE, state, mode=MODES["Pop"])
assert b > r, f"quiet audio should be cool (blue>red): {(r, g, b)}"

# a silent gap between tracks must not drag the loudness floor down — quiet
# music afterwards should still read cool, not pinned warm
for _ in range(500):
    audio_to_color(np.zeros(CHUNK), RATE, state, mode=MODES["Pop"])
for _ in range(80):
    audio_to_color(tone(440, 0.8), RATE, state, mode=MODES["Pop"])
for _ in range(60):
    r, g, b, _ = audio_to_color(tone(440, 0.02), RATE, state, mode=MODES["Pop"])
assert b > r, f"quiet after a silent gap should be cool: {(r, g, b)}"


# SMOOTH sets how long the flash throbs: Jazz should glow far longer than Rave
def throb_hops(name):
    st = new_map_state()
    m = MODES[name]
    for i in range(48):  # one kick at hop 24 over quiet
        _, _, _, dim = audio_to_color(tone(55, 0.8) if i == 24 else tone(60, 0.03),
                                      RATE, st, mode=m)
    n = 0
    while dim > m["floor"] + 10 and n < 200:
        _, _, _, dim = audio_to_color(tone(60, 0.03), RATE, st, mode=m)
        n += 1
    return n


rave, jazz = throb_hops("Rave"), throb_hops("Jazz")
assert jazz > 3 * max(rave, 1), f"Smooth should lengthen the throb: {rave} vs {jazz}"

# BRIGHTNESS preserves beat dynamics: a hard kick flashes brighter than a soft
# one (the original ". - . -" complaint). Alternate strong/weak kicks.
state = new_map_state()
strong = tone(60, 0.9)
weak = tone(60, 0.28)
quiet = tone(60, 0.03)
strong_dims, weak_dims = [], []
for i in range(240):
    if i % 16 == 0:
        _, _, _, dim = audio_to_color(strong, RATE, state, mode=MODES["Techno"])
        if i >= 32:
            strong_dims.append(dim)
    elif i % 16 == 8:
        _, _, _, dim = audio_to_color(weak, RATE, state, mode=MODES["Techno"])
        if i >= 32:
            weak_dims.append(dim)
    else:
        audio_to_color(quiet, RATE, state, mode=MODES["Techno"])
assert max(strong_dims) > max(weak_dims) + 10, \
    f"hard kick should out-flash soft kick: {max(strong_dims)} vs {max(weak_dims)}"
# ...and a detected beat pops hard above the floor (Techno floor=12), not a fizzle
assert max(strong_dims) > 80, f"detected beats should pop: {max(strong_dims)}"

# beat detection: kicks over a sustained bassline fire ~one onset each, on-beat
state = new_map_state()
bassline = tone(80, 0.25)
kick = bassline + tone(55, 0.8)
fired = []
for i in range(120):
    audio_to_color(kick if i % 24 == 0 else bassline, RATE, state,
                   mode=MODES["Techno"])
    if i >= 12 and state["onset"]:
        fired.append(i)
assert 3 <= len(fired) <= 5, f"expected ~4 onsets: {fired}"
assert all(min(abs(f - m) for m in (24, 48, 72, 96)) <= 2 for f in fired), \
    f"onsets off-beat: {fired}"

# Disco cycles the hue on beats (the one color exception)
state = new_map_state()
for i in range(120):
    audio_to_color(kick if i % 24 == 0 else quiet, RATE, state,
                   mode=MODES["Disco"])
assert state["hue"] > 0, "Disco should advance hue on beats"

# DROP: groove (kick+bass) -> bass-light build -> kick+bass slams back fires
# exactly one drop, on the build->drop transition (not at the song's start),
# and drop beats out-flash build beats. A live detector only knows the bass
# ceiling once it's heard it, hence the opening groove, as in real songs.
state = new_map_state()
sec = int(RATE / CHUNK)
build_at, drop_at = 12 * sec, 26 * sec
hits, build_dims, drop_dims = [], [], []
for i in range(drop_at + 300):
    hit_beat = tone(55, 0.8) if i % 24 == 0 else 0
    if build_at <= i < drop_at:
        c = tone(400, 0.3) + (tone(55, 0.08) if i % 24 == 0 else 0)
    else:
        c = tone(400, 0.3) + tone(80, 0.3) + hit_beat
    _, _, _, dim = audio_to_color(c, RATE, state, mode=MODES["Party"])
    if state["drop_hit"]:
        hits.append(i)
    if state["onset"] and i >= build_at + 2 * sec:
        (build_dims if i < drop_at else drop_dims).append(dim)
assert len(hits) == 1 and drop_at <= hits[0] <= drop_at + 30, \
    f"expected one drop at hop ~{drop_at}: {hits}"
assert np.mean(drop_dims) > np.mean(build_dims) + 20, \
    f"drop should out-flash build: {np.mean(drop_dims):.0f} vs {np.mean(build_dims):.0f}"

# spectrum output is the right shape for the visualiser bars
assert len(state["spectrum"]) == N_BARS

print("all mapping checks passed")
