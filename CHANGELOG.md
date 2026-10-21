# Changelog

## 0.3.1 (2026-10-22, original-engine fork)

- Recover the desired ON state after suspend/resume or an original-engine
  failure. Check the current daemon, heartbeat, output identity and exact channel
  routes; wait with bounded retries while the audio session returns. Preserve
  OFF, external output selection, hardware volume/mute and saved output boost.
- Keep controller focus on Output boost while saving. Serialize rapid changes,
  retain the latest selection and handle errors without disabling the slider.
- Preserve cancellation during recovery and volume-sync shutdown, including
  failures in an already-running routing operation.
- Shorten shutdown by draining only the validated private Dolby Proton prefix;
  immediately clean residual processes in the service's own cgroup. Keep the
  bounded systemd helper timeout and route restoration.
- Detect resume with a lightweight clock check every 250 ms while retaining the
  normal three-second interval for full audio health checks.

## 0.3.0 (2026-10-21, original-engine fork)

- Apply the supported official Speaker/Game runtime controls, including leveler,
  DRC, gain controls and EQ/regulator arrays, through the original Dolby receiver.
- Add the original VLLDP160 speaker post-stage candidate; keep it out of the
  headphone path. The Windows endpoint's selected version and stage order remain
  unverified.
- Verify runtime delivery, level-control state, private asset hashes and both
  persistent DSP instances. Retain hardware volume control and crash recovery.
- Require re-importing the matching private pack. Old generations and saved
  output boost values are preserved. Proprietary assets are excluded.

## 0.1.10 (2026-10-03)

- Setup and diagnostics pages fit the handheld screen: the setup controls come
  first and the steps sit in two columns above Steam's footer; the diagnostics
  report scrolls with the D-pad block by block.
- Update the converter (speaker-tuning-to-easyeffects) to 628d041 (Dependabot).
  The generated presets are unchanged.

## 0.1.9 (2026-10-03)

- Support the ROG Ally X (2024, RC72LA, codec subsystem 1043:1EB3) with its own
  ASUS package (Dolby Atmos driver V9.816.706.24) (#7 by @gmartsenkov).
- Per-device package registry in `defaults/fallback-sources.json`: display name,
  ASUS API query and pinned fallback package per lowercase codec subsystem id.
- Compare codec subsystem ids in lowercase everywhere.
- If the newest package from the ASUS API has no tuning for the codec, setup
  downloads the pinned package and tries again.
- "Try anyway" on an unknown device tries every pinned package.
- Generic device names in the unit description and the plugin metadata.

## 0.1.8 (2026-10-03)

- The backend shuts down cleanly when Decky stops it. With Decky v3.2.9 the
  plugin's socket loop spins once Decky closes its end of the connection, so
  the backend was killed after 5 s and `_uninstall` often never ran, which left
  the unit and runtime data behind after an uninstall. The backend now makes
  that read wait, and its unload and uninstall hooks no longer await anything.

## 0.1.7 (2026-10-03)

- Running setup no longer switches the DSP on: it starts the chain only when
  the DSP is switched on and no headphones are in use, and autostart follows
  the switch.
- Switching the DSP on or changing presets while headphones are in use no
  longer starts the chain; it starts once the headphones are unplugged.
- install.sh stops ally-dsp.service while it replaces the plugin and starts it
  again afterwards (#8 by @gmartsenkov).
- Update rollup to 4.63.4 (Dependabot).

## 0.1.6 (2026-10-03)

- No Steam restart after an in-app update: Decky already loads the new UI, and
  the panel now switches to it by itself (otherwise it asks to press B and
  reopen Ally DSP). The "Restart Steam after updates" toggle is gone. Updating
  from 0.1.4 with that toggle on still restarts Steam once, because 0.1.4's
  code handles that update.
- Uninstall cleanup no longer relies on a 15-minute update marker: the unit and
  runtime data are removed a minute after Decky's `_uninstall` only if the
  plugin is really gone. Fixes data loss when Decky's update prompt was
  confirmed after more than 15 minutes or two prompts were confirmed in a row.
- Saving a setting while setup runs no longer overwrites the finished setup.
- Update checks no longer block the backend: the panel shows the cached result
  and a due check runs in the background; after a failed check the next try
  waits 30 minutes instead of running on every refresh.
- Extras and pre-gain are locked while setup runs; changes that still arrive
  are applied when it finishes, as is the running game's own preset. A
  reconversion repeats until the presets match the current extras.

## 0.1.4 (2026-09-20)

- Restart Steam automatically after an in-app update so the new UI loads
  (Maintenance toggle, default on); manual restart button when the UI is stale.

## 0.1.3 (2026-09-19)

- Update @types/react and @types/react-dom to 19.3.0 (Dependabot).

## 0.1.2 (2026-09-19)

- Keep unit and runtime data when Decky replaces the plugin during an update
  (Decky calls `_uninstall` in that path); real uninstalls still clean up.
- Restore the active preset and unit on startup; re-run setup if data is missing.
- Show a hint when Steam still runs an older cached UI bundle.

## 0.1.1 (2026-09-19)

- Remove the Custom 1–3 presets (Dolby's neutral personalize profiles).
- English-only UI.
- Await background tasks on unload.

## 0.1.0 (2026-09-19)

First release.

- Setup wizard: hardware check, ASUS "Dolby Atmos driver" download with SHA-256
  verification, DAX3 tuning extraction, converter venv, conversion of all
  profiles, activation.
- PipeWire filter chain (convolver, LSP parametric EQ, multiband compressor,
  autogain, limiter) as WirePlumber smart filter in a systemd user unit.
- Presets Game/Dynamic/Movie/Music/Voice × Balanced/Detailed/Warm, global or
  per game; headphone pause; extras (leveler, dialog, regulator, pre-gain).
- Diagnostics page and text report.
- Signed self-update from GitHub Releases, installed via Decky Loader.
