#!/usr/bin/env python3
"""Install Kinect v1 model 1473 firmware using OpenKinect's Xbox update extractor."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

# Pin and verify the extractor; firmware itself comes from Microsoft's Xbox update.
EXTRACTOR_URL = ("https://raw.githubusercontent.com/OpenKinect/libfreenect/"
                 "09a1f098040d00e6070c18174904547ec31d2774/src/fwfetcher.py")
EXTRACTOR_SHA256 = "bdc0d43a43591d078dd88dc6ac55385e95f53697853bac20952c98b7f2ebc653"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, help="install an already extracted audios.bin (offline)")
    parser.add_argument("--output", type=Path, default=Path.home() / ".libfreenect" / "audios.bin")
    args = parser.parse_args()
    target = args.output.expanduser().resolve()
    if target.is_file() and target.stat().st_size > 0:
        print(f"Firmware already installed: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="echosense-firmware-") as folder:
        work = Path(folder)
        extracted = work / "audios.bin"
        if args.firmware:
            shutil.copyfile(args.firmware.expanduser(), extracted)
        else:
            print("Downloading OpenKinect extractor; it downloads Microsoft's Xbox system update.", flush=True)
            with urlopen(EXTRACTOR_URL, timeout=60) as response:
                script = response.read()
            if hashlib.sha256(script).hexdigest() != EXTRACTOR_SHA256:
                raise RuntimeError("OpenKinect extractor checksum mismatch")
            fetcher = work / "fwfetcher.py"
            fetcher.write_bytes(script)
            subprocess.run([sys.executable, str(fetcher), str(extracted)], cwd=work, check=True)
        if extracted.stat().st_size == 0:
            raise RuntimeError("Firmware file is empty")
        # Stage beside the destination so replacement is atomic, including across filesystems.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".audios-", delete=False) as stage:
            staged = Path(stage.name)
            try:
                stage.write(extracted.read_bytes())
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
        try:
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
    print(f"Installed: {target}\nRestart Echo-Sense; accelerometer auto mode now supports model 1473.")


if __name__ == "__main__":
    main()
