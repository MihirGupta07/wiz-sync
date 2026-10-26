Play your own songs and videos and sync the lights to them.

- **Music:** open an MP3, FLAC, WAV, M4A, OGG or other audio file and the lights flash to its beat, with the same build-up and drop effects as live sync. WiZ Sync plays the file itself, so the flashes are timed to when each beat leaves the speakers.
- **Video:** open an MP4, MKV, MOV, WebM or other video and it plays in its own window while the lights glow with the scene's main colour, fading smoothly and switching instantly on scene cuts.
- Play/pause, stop, seek and volume controls in the new **Player** row.

Playing files needs [ffmpeg](https://ffmpeg.org): run `winget install ffmpeg`, or put `ffmpeg.exe` in the WiZ Sync install folder. Live PC-audio sync works without it.

**Install:** download `WiZSync-Setup-1.1.0.exe` and run it (per-user, no admin needed). Installing over 1.0.x keeps your saved bulbs.

Requires Windows 10/11 x64 and WiZ bulbs on the same network. The installer isn't code-signed, so SmartScreen may warn: click "More info" → "Run anyway". Allow the firewall prompt on Private networks (UDP 38899, local only).

**Photosensitivity warning:** this app produces rapid flashing light.
