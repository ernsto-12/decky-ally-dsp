# Ally DSP: original-engine fork

This is [ernsto-12/decky-ally-dsp](https://github.com/ernsto-12/decky-ally-dsp), an experimental fork of [bassobr/Ally DSP](https://github.com/bassobr/decky-ally-dsp). The upstream MIT license and notices are retained.

The fork adds a choice between the existing converted speaker chain and a persistent original Dolby DLL engine hosted by Proton. **It replaces the installed Ally DSP**, using the same Decky identity and `ally-dsp.service`. Installing two copies with different folders is unsupported. Only one engine runs at a time. The installer backs up the previous plugin, settings and service before replacement; it keeps private tuning and assets in the data directory.

The original engine accepts 48 kHz, six-channel PCM mapped to static audio objects through the original frontend and processing DLL. Version 0.5.0 imports the official **Game, Dynamic, Movie, Music and Voice** settings for the internal speakers and generic headphones, with **Balanced, Detailed and Warm** voicings. Global and per-game selections apply to the original engine. Profile and control changes go through the original settings receiver in the same running DSP, without restarting the audio route.

The Dolby controls page provides the original 20-band EQ, intelligent EQ, dialog enhancer, volume leveler and spatial processing switch. These manual overrides are global; preset and voicing selection can be per-game. An empty override uses the selected official profile; **Reset official** restores it. Flat EQ writes zero band gains, which is a separate operation from restoring official settings. The original controls are independent of the converted engine's Extras. Switching off DSP Enabled bypasses the selected plugin engine.

Before audio starts, the host verifies the original receiver and the initial speaker Game level-control state against the native reference. The speaker path also runs an original **VLLDP160 speaker post-stage candidate** using the source speaker properties. The headphone path retains its own binaural settings and never runs the speaker post-stage. A separate headphone post-stage has not been validated.

This is still a partial reproduction of the Windows endpoint: the live Windows VLLDP version and stage order, hardware calibration and toggle boundaries have not been established. VLLDP160 comes from the official package, but the running Windows endpoint has not been shown to select that version. Its post-stage declares **176 frames (3.67 ms)** of additional latency; this is not the total audio-path latency. The source PO field is ignored by the frontend processing DLL. Ordinary PipeWire PCM carries no separate Atmos objects or Atmos bitstreams. This release processes ordinary PCM; dynamic game-object capture is deferred. The plugin remains an experimental implementation rather than a complete Dolby Access replacement.

Ordinary stereo game PCM provides no separate height channels or Atmos object metadata for this host to reconstruct.

On an RC73XA running SteamOS 3.8.28, 20 profile/control conditions produced byte-identical F32 and WAV output under native Windows and Proton. These include both output routes, all five presets, live EQ changes, resets and live profile changes in a persistent DSP. Isolated PipeWire null-sink tests verified capture, processing and playback PCM. These checks do not establish complete Windows endpoint equivalence or compatibility with every game.

Original assets are imported locally with `scripts/import-original-engine.py --help`. The public release includes the PCM host code. Dolby DLLs, OEM blobs, Windows Python and Proton are provisioned privately. Until the private assets are imported, the original option is unavailable and the converted engine remains usable. A fresh install still requires a legitimate compatible original-engine pack; the curl installer does not acquire the Dolby Access processing DLLs automatically.

On a supported RC73XA with a verified compatible private pack, the update prepares the current worker and all official profiles automatically. It downloads a pinned official ASUS package when the two required tuning XMLs are absent, verifies the package and XML hashes, then creates a new private asset generation. The previous generation is retained. Failed installation restores the previous plugin and its private descriptors before resuming audio. Original-engine hardware support is currently restricted to RC73XA/1043:1384; the converted engine retains the broader device list below.

**Output boost** is an original-engine-only manual control: **0–12 dB** in **0.5 dB** steps, default **0 dB**. It adjusts the plugin's playback stream after Dolby processing, independently of the converted engine's pre-gain, and does not change the DLL's PCM processing. Use **0 dB** when comparing the imported official profile's output with a reference; the speaker DLL now applies its own official loudness controls. This manual boost provides no automatic loudness compensation. Higher boost can clip loud scenes. An update does not overwrite a saved boost value.

While the original engine is active, the physical output remains the volume and mute master, so the handheld's volume keys act directly on the speakers. The virtual input displays the same controls and changes there are mirrored back to the physical output. Its monitor passes unattenuated PCM to the DLL to avoid double attenuation. Decky observes sink changes to keep the controls in sync; the physical volume keys continue working if Decky stops.

While Decky is running, the plugin checks the desired ON state after suspend/resume and original-engine failures. Recovery checks the live daemon, heartbeat, output identity and channel routes, then rebuilds the audio path when needed. It preserves OFF and user volume/mute/boost settings and waits while the audio session returns. Output boost remains adjustable during its own save so controller focus stays on the slider.

## Release scope

This release provides the Converted Tuning engine and the original Dolby engine for up to 5.1-channel PCM input and stereo output. The official **Dynamic** preset is a listening profile; it does not enable dynamic Atmos object capture. The game-object prototype is deferred and its proxy, game registration tools and session services are excluded from this package.

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
