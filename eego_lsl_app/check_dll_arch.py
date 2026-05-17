from pathlib import Path
import platform
from eego_sdk import windows_dll_arch

base = Path(__file__).resolve().parent
paths = [
    base / "sdk" / "eego-SDK.dll",
    base / "sdk" / "windows" / "64bit" / "eego-SDK.dll",
    base / "sdk" / "windows" / "32bit" / "eego-SDK.dll",
]
print(f"Python: {platform.python_version()} ({platform.architecture()[0]})")
for p in paths:
    if p.exists():
        print(f"{p.relative_to(base)}: {windows_dll_arch(p)}")
    else:
        print(f"{p.relative_to(base)}: missing")
