<p align="center">
  <img src="packaging/wiz-sync-512.png" alt="WiZ Sync icon" width="128">
</p>

<h1 align="center">WiZ Sync — Music Sync for Philips WiZ Smart Bulbs on Windows</h1>

<p align="center">
  Turn your WiZ Wi-Fi lights into an audio-reactive DJ light rig that flashes, pulses and changes colour to whatever your PC is playing.
</p>

<p align="center">
  <a href="https://github.com/MihirGupta07/wiz-sync/releases/latest"><img alt="Download for Windows" src="https://img.shields.io/github/v/release/MihirGupta07/wiz-sync?label=Download%20for%20Windows&style=for-the-badge"></a>
</p>

WiZ Sync is a free Windows desktop app that syncs **Philips WiZ smart bulbs to music** in real time. It listens to your PC's audio output directly. You don't need a microphone, the WiZ cloud or an account. It works with Spotify, YouTube, Apple Music, games, DJ software, movies and any other app that plays sound.

> ⚠️ **Photosensitivity warning:** WiZ Sync produces rapid flashing light. Lower the **Flash** knob or pick a calmer preset if flashing bothers you.

## Features

- **Beat-synced flashes.** Lights flash on every kick drum, and harder hits flash brighter.
- **Bass-driven colour.** Colour follows the low end, from cool blue when the bass is light to deep red when it's heavy.
- **Build-ups and drops.** Builds wash toward white and pulse with the snare roll. Drops land with a full-white impact, and the beats after hit at full brightness.
- **Blackouts.** The lights cut out when the music suddenly goes quiet.
- **Seven presets:** Rave, Techno, Disco, Party, Pop, Concert and Jazz.
- **Fine-tuning knobs:** Sensitivity, Brightness, Smoothing, Floor and Flash.
- **Light delay** so the lights line up with Bluetooth speakers and headphones.
- **Multiple bulbs.** It finds the WiZ bulbs on your network and controls any combination of them.
- **Local and private.** It talks to the bulbs only over your local network (UDP 38899), with low latency and no internet required.
- **Lightweight.** It uses about 2–3% of one CPU core.

## Download and install

### Installer

1. Download **`WiZSync-Setup-1.0.0.exe`** from the [latest release](https://github.com/MihirGupta07/wiz-sync/releases/latest).
2. Run it. It installs for your user account only, with no admin rights needed.

### Windows Package Manager (WinGet)

```powershell
winget install MihirGupta.WiZSync
```

*(WinGet availability is pending review in [microsoft/winget-pkgs#460838](https://github.com/microsoft/winget-pkgs/pull/460838).)*

> The installer isn't code-signed yet, so Windows SmartScreen may show a warning. Click **More info → Run anyway**. When Windows Firewall asks, allow access on **Private networks** so the app can reach your bulbs.

## How to use

1. Make sure your PC and WiZ bulbs are on the **same Wi-Fi/LAN**. Set the bulbs up in the WiZ app first.
2. Open WiZ Sync and click **Discover**.
3. Select the bulbs you want to sync and click **Save**.
4. Press the red **power jewel** and play some music.
5. Turn the **Mode** knob to switch presets. If the lights lag behind Bluetooth audio, raise **Light delay**.

Your saved bulbs and settings are stored in `%LOCALAPPDATA%\WiZ Sync`.

## Requirements

- Windows 10 or Windows 11 (64-bit)
- Philips WiZ (or WiZ Connected) Wi-Fi bulbs, light strips or lamps on the same local network
- Any audio output device: speakers, headphones, HDMI or Bluetooth

## FAQ

**Does it need a microphone?**
No. WiZ Sync captures your PC's audio output digitally with WASAPI loopback, so room noise doesn't affect it.

**Does it work with Spotify / YouTube / Netflix / games?**
Yes. It reacts to anything playing through your default Windows audio device.

**Does it work with Philips Hue?**
No. WiZ Sync is made for WiZ bulbs, which use a different, local Wi-Fi protocol.

**My bulbs aren't found.**
Check that the PC and bulbs are on the same network and not separated by a guest network or VLAN. Also check that Windows Firewall allows WiZ Sync on Private networks, and that **Allow local communication** is enabled in the WiZ app.

**The lights are a little behind the music.**
Bluetooth audio adds latency. Raise the **Light delay** setting until the flashes land on the beat.

## Run from source

```bash
pip install -r requirements.txt
python wiz_sync.py
```

Command-line options:

| Option | Description |
| --- | --- |
| `--debug` | Log beats, drops and near-misses to `wiz_sync_debug.log` |
| `--replay AUDIO` | Run the beat/drop detector over an audio file without the GUI (needs ffmpeg) |
| `--mode NAME` | Preset used with `--replay` (Rave, Techno, Disco, Party, Pop, Concert, Jazz) |
| `--version` | Print the version |

Run the tests with `python test_mapping.py`.

### Build the installer

```bash
python -m PyInstaller --noconfirm wiz-sync.spec
iscc packaging\wiz-sync.iss
```

This builds `dist\WiZSync-Setup-<version>.exe`. The WinGet manifests are in [`packaging/winget`](packaging/winget).

---

*WiZ Sync is an independent project and is not affiliated with or endorsed by WiZ, Signify or Philips.*
