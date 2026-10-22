#!/usr/bin/env python3
"""Import verified locally owned engine assets; never downloads proprietary data.

Run as the device user, with DSP disabled, after installing this fork:
  python3 scripts/import-original-engine.py --source-dir /ABS/private-pack \
    --python-dir /ABS/python-embed --proton /ABS/Steam/steamapps/common/Proton/proton
Use --steam-dir /ABS/Steam if Proton lives outside the Steam directory tree.
No service starts or routing changes occur. Select the engine in Decky afterwards.
The private pack must include the headphone spatial settings and supported
speaker Game controls. Supply both official XML files to import the full profile
catalog immediately. An existing compatible pack can also be upgraded from Decky.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
from allydsp.original_assets import AssetError, import_engine


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", required=True, type=Path, help="Flat private folder: reviewed DLLs, runtime blobs, speaker post-stage plan and allocator shim")
    parser.add_argument("--python-dir", required=True, type=Path, help="Reviewed Windows x64 embedded Python folder, including LICENSE.txt")
    parser.add_argument("--proton", required=True, type=Path, help="Absolute executable Proton script path")
    parser.add_argument("--steam-dir", type=Path, help="Steam client directory; otherwise derived from Proton path")
    parser.add_argument("--speaker-xml", type=Path, help="Pinned official RC73XA speaker tuning XML; requires --headphone-xml")
    parser.add_argument("--headphone-xml", type=Path, help="Pinned official generic headphone tuning XML; requires --speaker-xml")
    args = parser.parse_args()
    if bool(args.speaker_xml) != bool(args.headphone_xml):
        parser.error("--speaker-xml and --headphone-xml must be supplied together")
    try:
        result = import_engine(args.source_dir, args.python_dir, args.proton, steam_dir=args.steam_dir,
                               speaker_xml=args.speaker_xml, headphone_xml=args.headphone_xml)
    except (AssetError, OSError) as error:
        parser.exit(1, "Engine asset import failed: " + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
