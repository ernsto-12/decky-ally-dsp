"""Static configuration shared by backend, CLI and tests."""
from __future__ import annotations

PLUGIN_NAME = "Ally DSP"
GITHUB_REPO = "ernsto-12/decky-ally-dsp"
RELEASE_ZIP_TEMPLATE = "ally-dsp-{version}.zip"
USER_AGENT = "ally-dsp/decky (+https://github.com/ernsto-12/decky-ally-dsp)"

# Dolby profile ids from the DAX3 XML, in display order.
PROFILES = [
    ("game", "Game"),
    ("dynamic", "Dynamic"),
    ("movie", "Movie"),
    ("music", "Music"),
    ("voice", "Voice"),
]
PROFILE_IDS = [p for p, _ in PROFILES]
PROFILE_LABELS = dict(PROFILES)
VOICINGS = ["balanced", "detailed", "warm"]
VOICING_LABELS = {"balanced": "Balanced", "detailed": "Detailed", "warm": "Warm"}
DEFAULT_PROFILE = "game"
DEFAULT_VOICING = "balanced"

# LV2 plugins the converted chain uses; bundled in bin/lv2 (LGPL-3).
LSP_URIS = [
    "http://lsp-plug.in/plugins/lv2/para_equalizer_x16_lr",
    "http://lsp-plug.in/plugins/lv2/mb_compressor_stereo",
    "http://lsp-plug.in/plugins/lv2/limiter_stereo",
    "http://lsp-plug.in/plugins/lv2/autogain_stereo",
    "http://lsp-plug.in/plugins/lv2/filter_stereo",
]
# Virtual bass needs Calf, which is not bundled; offered only when installed system-wide.
CALF_SATURATOR_URI = "http://calf.sourceforge.net/plugins/Saturator"

NODE_BASE = "ally_dsp"
INPUT_NODE = f"effect_input.{NODE_BASE}"
OUTPUT_NODE = f"effect_output.{NODE_BASE}"
SMART_FILTER_NAME = "ally-dsp"

UPDATE_CHECK_INTERVAL_S = 6 * 3600
UPDATE_RETRY_S = 30 * 60
PREGAIN_MIN_DB = -6.0
PREGAIN_MAX_DB = 6.0
