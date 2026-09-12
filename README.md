# Omarchy Talks

Omarchy Talks is a thin, local text reader for Omarchy. Select text in a Wayland application, invoke the read-selection hotkey, and it synthesizes and plays ordered speech through a locally managed, pinned VoiceBox Kokoro runtime. The reader owns selection capture, segmentation, playback, replacement, and Omarchy integration. VoiceBox owns profiles, models, and inference.

## Current support

This release is tested on Omarchy with an English Kokoro CPU runtime. It does not claim support for other operating systems, GPU/CUDA paths, or VoiceBox languages that require phonemizers not installed by this project. VoiceBox may list additional presets; that does not make their runtime paths supported here.

The observed development-machine footprint was about 152 MB for the pinned VoiceBox checkout and 2.1 GB for its virtual environment. This is an observed local value, not a download-size or disk-use guarantee.

## Install

Prerequisites are a normal Omarchy desktop with `git`, `uv`, `pw-play`, `wl-paste`, `systemctl --user`, and Omarchy plugin tools available on `PATH`. Omarchy does not currently include `uv` on a pristine installation; install it first with Astral's official user-scoped method at <https://docs.astral.sh/uv/getting-started/installation/>. The installer does not use `sudo` or install system packages.

```bash
git clone https://github.com/Zerolxgic/Omarchy-Talks.git
cd Omarchy-Talks
scripts/install-user-runtime.sh --enable
omarchy-talks doctor
```

Installation creates or uses only user-owned locations:

- executable managed by `uv tool`;
- configuration: `~/.config/omarchy-talks/config.toml`;
- VoiceBox source, virtual environment, and profile data: `~/.local/share/omarchy-talks/`;
- user unit: `~/.config/systemd/user/omarchy-talks-voicebox.service`;
- Omarchy plugins: `~/.config/omarchy/plugins/omarchy-talks.controls` and `~/.config/omarchy/plugins/omarchy-talks.settings`.

The service listens on `127.0.0.1:17493` and uses the pinned revision recorded in [`VOICEBOX_PIN`](VOICEBOX_PIN). The installer deliberately resolves a Kokoro-only CPU dependency path; it does not install VoiceBox's all-engine CUDA/NVIDIA stack.

## Use

The installer adds `SUPER + ALT + R` for Read/replace selection and `SUPER + ALT + SHIFT + R` for Stop speech. It keeps a backup of `~/.config/hypr/bindings.lua`, writes only a marked Omarchy Talks block, and removes that block on uninstall. Select text, use the read hotkey, then use the transient playback surface to pause/resume or stop. The separate **Setup → Omarchy Talks** panel changes the voice for subsequent reads.

```bash
omarchy-talks speak-selection
omarchy-talks pause
omarchy-talks resume
omarchy-talks stop
omarchy-talks status --json
omarchy-talks voices --json
omarchy-talks voice set af_bella
omarchy-talks doctor
systemctl --user status omarchy-talks-voicebox.service
journalctl --user -u omarchy-talks-voicebox.service
```

`speak-selection` reads only the Wayland primary selection. It never falls back to the ordinary clipboard. A new read replaces the active read; stale audio must not resume.

## Uninstall

```bash
scripts/uninstall-user-runtime.sh
```

Normal uninstall removes the service and integration links while retaining `~/.config/omarchy-talks` and `~/.local/share/omarchy-talks` (configuration, profiles, VoiceBox source, and environment). On a disposable installation, use `scripts/uninstall-user-runtime.sh --purge` to remove those retained paths too.

## Diagnostics and development

Run `omarchy-talks doctor` before reporting a problem. Public unit tests can run without a graphical session:

```bash
uv run python -m unittest discover -s tests -v
omarchy-plugin-validate plugins/omarchy-talks.controls
omarchy-plugin-validate plugins/omarchy-talks.settings
```

## Known limitations

- Audio intelligibility and desktop appearance require human acceptance on the target desktop; automated WAV/player checks alone do not establish them.
- Only the tested English Kokoro CPU path is supported.
- VoiceBox work may continue briefly after an accepted cancellation request.
- The reader accepts complete PCM WAV artifacts only.

## License and third parties

Omarchy Talks is MIT licensed. It does not relicense VoiceBox, Kokoro, models, PyTorch, or any other dependency. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for the relevant boundaries.
