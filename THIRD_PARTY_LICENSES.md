# Third-party components

Ally DSP itself is licensed under the MIT License (see `LICENSE`). The release
zip and the source tree contain or fetch the following third-party work.

| Component | Use | License | Source |
|---|---|---|---|
| LSP Plugins (`lsp-plugins-lv2.so`, five plugin descriptions) | LV2 DSP stages (parametric EQ, multiband compressor, autogain, limiter, filter) run by PipeWire | LGPL-3.0-or-later, see `third_party/licenses/LGPL-3.0.txt` | https://lsp-plug.in — binary taken unmodified from the Arch Linux package `lsp-plugins-lv2` 1.2.22 as mirrored by SteamOS; complete source at https://github.com/lsp-plugins/lsp-plugins |
| speaker-tuning-to-easyeffects | Converts the Dolby DAX3 tuning XML into an EasyEffects preset and a PipeWire filter chain (runs on the device in a private venv) | MIT, Copyright (c) 2026 Antoine Cellerier | https://github.com/antoinecellerier/speaker-tuning-to-easyeffects (git submodule `third_party/speaker-tuning-to-easyeffects`) |
| numpy, scipy | Numerical dependencies of the converter, installed by pip into the plugin's private venv on the device | BSD-3-Clause | https://numpy.org, https://scipy.org |
| Decky plugin template | Project skeleton (build configuration, type stubs) | BSD-3-Clause, Steam Deck Homebrew | https://github.com/SteamDeckHomebrew/decky-plugin-template |
| minisign file format | The release signatures follow Frank Denis' minisign format; the Ed25519 code in `py_modules/allydsp/ed25519.py` is an original implementation of RFC 8032 | MIT (minisign) | https://jedisct1.github.io/minisign/ |

The updater and release pipeline design follows
https://github.com/bassobr/Decky-Wifi-Streaming-Optimizer (BSD-3-Clause, a
fork of Arcada Labs' WiFi Optimizer); no code was copied from it.

**Not included on purpose:** the Dolby/ASUS speaker tuning (`DEV_*_SUBSYS_*.xml`)
is proprietary. The plugin downloads ASUS' public driver package onto the
user's own device during setup and extracts the file there; it is never
redistributed with this project.

The experimental original-engine fork distributes its own Python IPC, COM input and routing host code under MIT. DolbyAudioProcessing.dll, DolbyHrtfEnc.dll and OEM configuration blobs are proprietary and must be supplied locally; they are excluded from source and release archives. A user-supplied Windows Python embed runtime is subject to the Python license included with that runtime. Proton remains a separately installed Steam dependency. No Dolby trademark endorsement is implied.
