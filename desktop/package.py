"""Build, verify, and archive one desktop target on its native host."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
REDHAND = DESKTOP.parent
VERSION = "0.1.0"


def run(*args, **options):
    subprocess.run([str(arg) for arg in args], check=True, **options)


def package(agenttrueup: Path, work: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    system = platform.system().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    target = {"darwin": "macos", "linux": "linux", "windows": "windows"}[system]
    package_name = f"FileChangeDesk-v{VERSION}-{target}-{architecture}"
    if system == "darwin":
        app = work / "文件改动记录.app"
        run("bash", DESKTOP / "build-mac.sh", agenttrueup, sys.executable, work / "build", app)
        run(sys.executable, DESKTOP / "smoke.py", app / "Contents/Resources/engine/changes-engine")
        archive = output / (package_name + ".zip")
        run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive)
    else:
        binary_name = "FileChangeDesk"
        distribution = work / "dist"
        run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--name", binary_name,
            "--paths", REDHAND / "src", "--paths", agenttrueup / "src", "--specpath", work,
            "--workpath", work / "build", "--distpath", distribution, DESKTOP / "portable.py")
        bundle = distribution / binary_name
        licenses = bundle / "Licenses"
        licenses.mkdir()
        shutil.copy2(REDHAND / "LICENSE", licenses / "redhand.txt")
        shutil.copy2(agenttrueup / "LICENSE", licenses / "agenttrueup.txt")
        executable = bundle / (binary_name + (".exe" if system == "windows" else ""))
        readme = ("文件改动记录 / File Change Desk\n\n"
                  f"解压完整文件夹，打开 {executable.name}。点「试用演示」试用内置样例。\n"
                  "Extract the entire folder, open the executable, and choose 试用演示 to try the sample.\n")
        (bundle / "README.txt").write_text(readme, encoding="utf-8")
        result = work / "smoke-result.txt"
        command = [executable, "--self-test", result]
        if system == "linux" and not os.environ.get("DISPLAY"):
            command[:0] = ["xvfb-run", "-a"]
        try:
            run(*command, timeout=90)
        finally:
            if result.is_file():
                print(result.read_text(encoding="utf-8"), flush=True)
        assert result.read_text(encoding="utf-8").startswith("PASS:")
        kind = "zip" if system == "windows" else "gztar"
        archive = shutil.make_archive(str(output / package_name), kind, distribution, binary_name)
    print(f"Verified and packaged: {archive}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("agenttrueup", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    package(arguments.agenttrueup.resolve(), arguments.work.resolve(), arguments.output.resolve())
