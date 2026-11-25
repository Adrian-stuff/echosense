#!/usr/bin/env python3
"""Install model 1473 firmware and a libfreenect build that avoids macOS audio USB resets."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

REVISION = "09a1f098040d00e6070c18174904547ec31d2774"
SOURCE_SHA256 = "9c32e8b7fad220ef34e1423fd532a32f6f0df8f782d872bb35d982e90fc359a8"
RESET_BLOCK = """\tlibusb_reset_device(audioHandle);
\tlibusb_close(audioHandle);

\tres = libusb_open(audio, &audioHandle);
\tif (res < 0) {
\t\tFN_ERROR("Failed to set the LED of K4W or 1473 device: %s\\n", libusb_error_name(res));
\t\treturn res;
\t}
"""


def main():
    if sys.platform != "darwin":
        raise SystemExit("This driver workaround is for macOS only.")
    subprocess.run([sys.executable, str(Path(__file__).with_name("setup_kinect1_firmware.py"))], check=True)
    if shutil.which("cmake"):
        cmake = [shutil.which("cmake")]
    elif shutil.which("uv"):
        cmake = [shutil.which("uv"), "tool", "run", "--from", "cmake", "cmake"]
    else:
        raise SystemExit("Install CMake first: brew install cmake")
    brew = shutil.which("brew")
    if not brew:
        raise SystemExit("Homebrew with libusb is required: brew install libusb")
    prefix = subprocess.check_output([brew, "--prefix"], text=True).strip()
    target = Path.home() / ".local/share/echosense/libfreenect/libfreenect.dylib"
    with tempfile.TemporaryDirectory(prefix="echosense-driver-") as folder:
        work = Path(folder)
        with urlopen(f"https://codeload.github.com/OpenKinect/libfreenect/tar.gz/{REVISION}", timeout=60) as response:
            archive = response.read()
        if hashlib.sha256(archive).hexdigest() != SOURCE_SHA256:
            raise RuntimeError("libfreenect source checksum mismatch")
        (work / "source.tar.gz").write_bytes(archive)
        subprocess.run(["tar", "-xzf", str(work / "source.tar.gz"), "--strip-components=1", "-C", str(work)], check=True)
        source = work / "src/usb_libusb10.c"
        text = source.read_text()
        if text.count(RESET_BLOCK) != 1:
            raise RuntimeError("Expected audio reset block not found")
        source.write_text(text.replace(RESET_BLOCK,
            "\t// macOS: retain the open audio handle; resetting detaches model 1473.\n"))
        build = work / "build"
        subprocess.run(cmake + ["-S", str(work), "-B", str(build),
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", f"-DCMAKE_PREFIX_PATH={prefix}",
            "-DBUILD_EXAMPLES=OFF", "-DBUILD_FAKENECT=OFF", "-DBUILD_C_SYNC=OFF",
            "-DBUILD_REDIST_PACKAGE=ON", "-DBUILD_OPENNI2_DRIVER=OFF"], check=True)
        subprocess.run(cmake + ["--build", str(build), "--target", "freenect", "-j", "4"], check=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=".libfreenect-", dir=target.parent)
        os.close(fd)
        try:
            shutil.copy2(build / "lib/libfreenect.dylib", staged)
            os.replace(staged, target)
        finally:
            Path(staged).unlink(missing_ok=True)
    print(f"Installed: {target}\nEcho-Sense selects this driver automatically. Restart the app.")


if __name__ == "__main__":
    main()
