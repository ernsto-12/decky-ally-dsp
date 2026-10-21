# Ally DSP: original-engine fork

This is [ernsto-12/decky-ally-dsp](https://github.com/ernsto-12/decky-ally-dsp), an experimental fork of [bassobr/Ally DSP](https://github.com/bassobr/decky-ally-dsp). The upstream MIT license and notices are retained.

The fork adds a choice between the existing converted speaker chain and a persistent original Dolby DLL engine hosted by Proton. **It replaces the installed Ally DSP**, using the same Decky identity and `ally-dsp.service`. Installing two copies with different folders is unsupported. Only one engine runs at a time. The installer backs up the previous plugin, settings and service before replacement; it keeps private tuning and assets in the data directory.

The original engine selects the official **speaker Game** or **generic headphone Game** configuration when the internal analog route changes. Both accept 48 kHz, six-channel PCM mapped to static audio objects through the original frontend and processing DLL. The headphone configuration renders binaural stereo; the tested speaker configuration applies its separate stereo spatial path. It does not receive Windows Atmos object metadata or decode Atmos bitstreams from ordinary PipeWire PCM. This profile does not establish identical built-in speaker calibration or a complete replacement for Dolby Access.

Ordinary stereo game PCM provides no separate height channels or Atmos object metadata for this host to reconstruct.

On an RC73XA running SteamOS 3.8.28, 18 offline frontend conditions were byte-identical to Windows. A persistent original HRTF worker also reproduced the Windows six-channel reference exactly. Isolated PipeWire null-sink tests verified capture, processing and playback PCM. These are channel-bed and transport checks; game compatibility, long-session stability and perceived sound still require testing.

Original assets are imported locally with `scripts/import-original-engine.py --help`. The public release includes host code, not Dolby DLLs, OEM blobs, Windows Python or a Proton distribution. Until the private assets are imported, the original option is unavailable and the converted engine remains usable. In original mode, the imported pack fixes the effective Game profile for the active speaker/headphone route; converted per-game presets, voicings and extras remain saved for the converted engine.

**Output boost** is an original-engine-only manual control: **0–12 dB** in **0.5 dB** steps, default **0 dB**. It adjusts the plugin's playback stream, independently of the converted engine's pre-gain, and does not change the DLL's PCM processing. It provides no automatic loudness compensation. Higher boost can clip loud scenes.

While the original engine is active, volume keys control the virtual input as the single master. The physical output is temporarily held at 100% (unity) to avoid double attenuation. Stopping the original engine transfers the current master volume back to the physical output.

The remainder describes the upstream converted engine.

Decky Loader plugin that brings the Dolby speaker tuning of the ASUS ROG Ally
handhelds (ROG Xbox Ally X, ROG Xbox Ally, ROG Ally X) to SteamOS. On Windows the
built-in speakers are shaped by Dolby Atmos; on SteamOS they play untreated.
Ally DSP downloads the public ASUS Dolby driver package for the detected device,
extracts the tuning for the installed codec, converts it with
[speaker-tuning-to-easyeffects](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects)
into a PipeWire filter chain and runs the chain as a WirePlumber smart filter in
front of the speaker sink. Steam keeps seeing the real speakers, the volume keys
keep working, headphones pause the chain.

## Features

- Setup wizard: hardware check, ASUS package download with SHA-256 verification,
  tuning extraction, converter venv, conversion of all profiles, activation.
- Presets Game, Dynamic, Movie, Music and Voice, each with the Dolby voicings
  Balanced, Detailed and Warm.
- Global preset or per-game preset, switched with the running app.
- Extras: Dolby volume leveler (on by default), dialog enhancer, regulator, pre-gain.
- Headphone detection on the shared 3.5 mm jack pauses the chain.
- Diagnostics page and text report.
- Signed updates from GitHub Releases, installed through Decky Loader.
- No root; nothing is written outside the home directory.

## Supported hardware

| Device | Codec subsystem | Status |
|---|---|---|
| ROG Xbox Ally X (RC73XA) | 1043:1384 | developed and tested on SteamOS 3.8.16 |
| ROG Xbox Ally (RC73YA) | 1043:1394 | tuning is in the same ASUS package, untested |
| ROG Ally X (2024, RC72LA) | 1043:1EB3 | tested on SteamOS 3.8.28 by a contributor |

Each device has its own entry in `defaults/fallback-sources.json`: the ASUS
support API query and a pinned package (URL, SHA-256, size). The RC72LA uses an
older ASUS package (V9.816.706.24) because the Xbox Ally package has no tuning
for its codec. To add a device, add its lowercase codec subsystem id to that file.

Requirements: SteamOS 3.8 or newer, Decky Loader, internet access during setup
(10 MB ASUS package plus about 220 MB numpy/scipy for the converter).

## Install

With Decky Loader installed, open Konsole in Desktop Mode:

```bash
curl -fsSL https://raw.githubusercontent.com/ernsto-12/decky-ally-dsp/main/install.sh -o /tmp/ally-dsp-install.sh && sudo bash /tmp/ally-dsp-install.sh
```

Then open the Quick Access menu → Decky → Ally DSP → Setup. Updates are offered
in the plugin; `install.sh` can be re-run at any time.

## How it works

```
game → PipeWire → [Ally DSP: convolver → LSP PEQ → LSP multiband compressor → LSP autogain → LSP limiter] → speaker sink → ALC294 → 2× TAS2781 → speakers
```

The chain runs in its own `pipewire -c` process (`~/.config/systemd/user/ally-dsp.service`),
the pattern Valve uses for the Steam Deck microphone filter. The LSP LV2 plugins
are bundled in `bin/lv2` (LGPL-3, see `THIRD_PARTY_LICENSES.md`). A preset
switch restarts that process with another configuration. Details:
[docs/architecture.md](docs/architecture.md), background: [docs/research.md](docs/research.md).

## Command line

The backend doubles as a CLI for support (run as `deck`):

```bash
cd ~/homebrew/plugins/"Ally DSP"
PYTHONPATH=py_modules python3 -m allydsp.cli doctor
PYTHONPATH=py_modules python3 -m allydsp.cli setup
PYTHONPATH=py_modules python3 -m allydsp.cli apply movie warm --save
PYTHONPATH=py_modules python3 -m allydsp.cli status
```

## Development

```bash
pnpm install && pnpm build          # frontend
python3 -m pytest tests -q          # backend (Python 3.9+)
scripts/assemble-converter.sh       # copy converter from the submodule
scripts/fetch-lsp.sh                # bundle the LV2 plugins
scripts/dev-deploy.sh deck@<ip>     # copy to the handheld and restart Decky
```

Releases use tags `vX.Y.Z` matching `package.json`. Current releases are signed
locally and uploaded to GitHub. GitHub Actions builds, tests and packages
`ally-dsp-X.Y.Z.zip`; the tag workflow can also sign `SHA256SUMS` and publish
when `MINISIGN_SEED` is configured. Without that secret, CI retains build
artifacts and skips signed release publication.

## License

MIT. Third-party components and why the Dolby tuning is never redistributed:
`THIRD_PARTY_LICENSES.md`.
