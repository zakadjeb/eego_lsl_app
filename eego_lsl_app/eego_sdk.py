"""ctypes wrapper for ANT Neuro / eemagine eego SDK C wrapper.

The default bundled DLL is now the Windows x64 SDK DLL, so it should load from
normal 64-bit Python 3.13 on Windows. If you need 32-bit Python, replace
sdk/eego-SDK.dll with sdk/windows/32bit/eego-SDK.dll.
"""
from __future__ import annotations

import ctypes as C
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import List


class EegoSdkError(RuntimeError):
    pass


class AmplifierInfo(C.Structure):
    _fields_ = [("id", C.c_int), ("serial", C.c_char * 64)]


class ChannelInfo(C.Structure):
    _fields_ = [("index", C.c_int), ("type", C.c_int)]


# Matches eemagine/sdk/wrapper.h in eego-SDK 1.3.23.45142.
CHANNEL_TYPES = {
    # Values 0-8 are defined by eemagine/sdk/wrapper.h for SDK 1.3.x.
    0: "reference",
    1: "bipolar",
    2: "accelerometer",
    3: "gyroscope",
    4: "magnetometer",
    5: "trigger",
    6: "sample_counter",
    7: "impedance_reference",
    8: "impedance_ground",
    # Some ANT/eego SDK builds may expose extra channel types. The uploaded
    # wrapper.h does not define EOG explicitly, but leaving this alias here
    # lets the app use it if a newer DLL returns it. Unknown codes are still
    # logged as unknown_<code>.
    9: "eog",
    10: "impedance_eog",
}


def windows_dll_arch(path: str | os.PathLike[str]) -> str:
    """Return '32bit', '64bit', or 'unknown' for a Windows PE DLL."""
    p = Path(path)
    try:
        with p.open("rb") as f:
            if f.read(2) != b"MZ":
                return "unknown"
            f.seek(0x3C)
            pe_offset = int.from_bytes(f.read(4), "little")
            f.seek(pe_offset)
            if f.read(4) != b"PE\0\0":
                return "unknown"
            machine = int.from_bytes(f.read(2), "little")
    except Exception:
        return "unknown"
    if machine == 0x8664:
        return "64bit"
    if machine == 0x014C:
        return "32bit"
    return "unknown"


@dataclass(frozen=True)
class Channel:
    index: int
    type_code: int

    @property
    def type(self) -> str:
        return CHANNEL_TYPES.get(self.type_code, f"unknown_{self.type_code}")


@dataclass(frozen=True)
class Amplifier:
    id: int
    serial: str


class EegoSdk:
    def __init__(self, dll_path: str | os.PathLike[str] | None = None) -> None:
        if dll_path is None:
            dll_path = Path(__file__).resolve().parent / "sdk" / "eego-SDK.dll"
        self.dll_path = str(Path(dll_path).resolve())
        if not Path(self.dll_path).exists():
            raise EegoSdkError(f"DLL not found: {self.dll_path}")

        python_arch = platform.architecture()[0]
        dll_arch = windows_dll_arch(self.dll_path)
        if dll_arch in {"32bit", "64bit"} and dll_arch != python_arch:
            raise EegoSdkError(
                f"Architecture mismatch: Python is {python_arch}, but {self.dll_path} is {dll_arch}. "
                "Use matching Python, or replace sdk/eego-SDK.dll with the matching DLL from sdk/windows/."
            )

        try:
            self.dll = C.CDLL(self.dll_path)
        except OSError as exc:
            raise EegoSdkError(
                f"Could not load eego SDK DLL: {self.dll_path}\n{exc}\n"
                "Make sure the ANT/eego driver/runtime and license are installed on this computer."
            ) from exc
        self._bind_functions()
        self.dll.eemagine_sdk_init()

    def close(self) -> None:
        try:
            self.dll.eemagine_sdk_exit()
        except Exception:
            pass

    def __enter__(self) -> "EegoSdk":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _bind_functions(self) -> None:
        d = self.dll
        d.eemagine_sdk_init.argtypes = []
        d.eemagine_sdk_init.restype = None
        d.eemagine_sdk_exit.argtypes = []
        d.eemagine_sdk_exit.restype = None
        d.eemagine_sdk_get_version.argtypes = []
        d.eemagine_sdk_get_version.restype = C.c_int
        d.eemagine_sdk_get_amplifiers_info.argtypes = [C.POINTER(AmplifierInfo), C.c_int]
        d.eemagine_sdk_get_amplifiers_info.restype = C.c_int
        d.eemagine_sdk_get_amplifier_channel_list.argtypes = [C.c_int, C.POINTER(ChannelInfo), C.c_int]
        d.eemagine_sdk_get_amplifier_channel_list.restype = C.c_int
        d.eemagine_sdk_get_amplifier_sampling_rates_available.argtypes = [C.c_int, C.POINTER(C.c_int), C.c_int]
        d.eemagine_sdk_get_amplifier_sampling_rates_available.restype = C.c_int
        d.eemagine_sdk_get_amplifier_reference_ranges_available.argtypes = [C.c_int, C.POINTER(C.c_double), C.c_int]
        d.eemagine_sdk_get_amplifier_reference_ranges_available.restype = C.c_int
        d.eemagine_sdk_get_amplifier_bipolar_ranges_available.argtypes = [C.c_int, C.POINTER(C.c_double), C.c_int]
        d.eemagine_sdk_get_amplifier_bipolar_ranges_available.restype = C.c_int
        d.eemagine_sdk_open_eeg_stream.argtypes = [C.c_int, C.c_int, C.c_double, C.c_double, C.c_ulonglong, C.c_ulonglong]
        d.eemagine_sdk_open_eeg_stream.restype = C.c_int
        d.eemagine_sdk_open_impedance_stream.argtypes = [C.c_int, C.c_ulonglong]
        d.eemagine_sdk_open_impedance_stream.restype = C.c_int
        d.eemagine_sdk_close_stream.argtypes = [C.c_int]
        d.eemagine_sdk_close_stream.restype = C.c_int
        d.eemagine_sdk_get_stream_channel_count.argtypes = [C.c_int]
        d.eemagine_sdk_get_stream_channel_count.restype = C.c_int
        d.eemagine_sdk_get_stream_channel_list.argtypes = [C.c_int, C.POINTER(ChannelInfo), C.c_int]
        d.eemagine_sdk_get_stream_channel_list.restype = C.c_int
        d.eemagine_sdk_prefetch.argtypes = [C.c_int]
        d.eemagine_sdk_prefetch.restype = C.c_int
        d.eemagine_sdk_get_data.argtypes = [C.c_int, C.POINTER(C.c_double), C.c_int]
        d.eemagine_sdk_get_data.restype = C.c_int
        d.eemagine_sdk_get_error_string.argtypes = [C.c_char_p, C.c_int]
        d.eemagine_sdk_get_error_string.restype = C.c_int

    def _error_string(self) -> str:
        buf = C.create_string_buffer(2048)
        try:
            self.dll.eemagine_sdk_get_error_string(buf, len(buf))
            return buf.value.decode(errors="replace")
        except Exception:
            return "unknown SDK error"

    def _check(self, value: int, context: str) -> int:
        if value < 0:
            raise EegoSdkError(f"{context} failed with code {value}: {self._error_string()}")
        return int(value)

    def version(self) -> int:
        return self._check(self.dll.eemagine_sdk_get_version(), "get_version")

    def amplifiers(self, max_count: int = 16) -> List[Amplifier]:
        arr = (AmplifierInfo * max_count)()
        n = self._check(self.dll.eemagine_sdk_get_amplifiers_info(arr, max_count), "get_amplifiers_info")
        return [Amplifier(int(arr[i].id), arr[i].serial.decode(errors="replace").strip("\x00")) for i in range(n)]

    def amplifier_channels(self, amplifier_id: int, max_count: int = 512) -> List[Channel]:
        arr = (ChannelInfo * max_count)()
        n = self._check(self.dll.eemagine_sdk_get_amplifier_channel_list(amplifier_id, arr, max_count), "get_amplifier_channel_list")
        return [Channel(int(arr[i].index), int(arr[i].type)) for i in range(n)]

    def sampling_rates(self, amplifier_id: int, max_count: int = 64) -> List[int]:
        arr = (C.c_int * max_count)()
        n = self._check(self.dll.eemagine_sdk_get_amplifier_sampling_rates_available(amplifier_id, arr, max_count), "get_sampling_rates")
        return [int(arr[i]) for i in range(n)]

    def reference_ranges(self, amplifier_id: int, max_count: int = 64) -> List[float]:
        arr = (C.c_double * max_count)()
        n = self._check(self.dll.eemagine_sdk_get_amplifier_reference_ranges_available(amplifier_id, arr, max_count), "get_reference_ranges")
        return [float(arr[i]) for i in range(n)]

    def bipolar_ranges(self, amplifier_id: int, max_count: int = 64) -> List[float]:
        arr = (C.c_double * max_count)()
        n = self._check(self.dll.eemagine_sdk_get_amplifier_bipolar_ranges_available(amplifier_id, arr, max_count), "get_bipolar_ranges")
        return [float(arr[i]) for i in range(n)]

    def open_impedance_stream(self, amplifier_id: int, ref_mask: int = 0xFFFFFFFFFFFFFFFF) -> int:
        return self._check(self.dll.eemagine_sdk_open_impedance_stream(amplifier_id, ref_mask), "open_impedance_stream")

    def open_eeg_stream(self, amplifier_id: int, sampling_rate: int = 512, reference_range: float = 0.15,
                        bipolar_range: float = 4.0, ref_mask: int = 0xFFFFFFFFFFFFFFFF,
                        bip_mask: int = 0xFFFFFFFFFFFFFFFF) -> int:
        return self._check(
            self.dll.eemagine_sdk_open_eeg_stream(
                amplifier_id, int(sampling_rate), float(reference_range), float(bipolar_range), ref_mask, bip_mask
            ),
            "open_eeg_stream",
        )

    def close_stream(self, stream_id: int) -> None:
        self._check(self.dll.eemagine_sdk_close_stream(stream_id), "close_stream")

    def stream_channels(self, stream_id: int, max_count: int = 512) -> List[Channel]:
        arr = (ChannelInfo * max_count)()
        n = self._check(self.dll.eemagine_sdk_get_stream_channel_list(stream_id, arr, max_count), "get_stream_channel_list")
        return [Channel(int(arr[i].index), int(arr[i].type)) for i in range(n)]

    def stream_channel_count(self, stream_id: int) -> int:
        return self._check(self.dll.eemagine_sdk_get_stream_channel_count(stream_id), "get_stream_channel_count")

    def get_data(self, stream_id: int) -> list[list[float]]:
        """Return streamed samples as rows: sample x channel.

        This is used for EEG streams. The SDK manual says EEG GetData() returns
        a buffer of samples and should be polled at least once per second.
        """
        n_bytes = self._check(self.dll.eemagine_sdk_prefetch(stream_id), "prefetch")
        if n_bytes == 0:
            return []
        n_doubles = n_bytes // C.sizeof(C.c_double)
        channel_count = self.stream_channel_count(stream_id)
        if channel_count <= 0:
            return []
        buf = (C.c_double * n_doubles)()
        self._check(self.dll.eemagine_sdk_get_data(stream_id, buf, n_bytes), "get_data")
        sample_count = n_doubles // channel_count
        return [[float(buf[ch + sample * channel_count]) for ch in range(channel_count)] for sample in range(sample_count)]

    def get_impedance_state(self, stream_id: int) -> list[float]:
        """Return the latest impedance state as one vector in raw Ohm.

        The ANT/eego SDK manual is explicit that the impedance "stream" is not
        a continuous sample stream: it is a state. Calling GetData() returns one
        sample containing the latest known impedance values, in Ohm.

        The C++ SDK wrapper still calls prefetch() before get_data(). In normal
        builds, prefetch() should return exactly channel_count * sizeof(double)
        for an impedance state. As a defensive fallback, if a build reports 0
        bytes for the state, we still request one full channel vector.
        """
        channel_count = self.stream_channel_count(stream_id)
        if channel_count <= 0:
            return []
        n_bytes = self._check(self.dll.eemagine_sdk_prefetch(stream_id), "prefetch")
        expected_bytes = channel_count * C.sizeof(C.c_double)
        if n_bytes <= 0:
            n_bytes = expected_bytes
        if n_bytes < expected_bytes:
            raise EegoSdkError(
                f"impedance GetData buffer too small: prefetch returned {n_bytes} bytes, "
                f"but one state for {channel_count} channels requires {expected_bytes} bytes"
            )
        buf = (C.c_double * (n_bytes // C.sizeof(C.c_double)))()
        self._check(self.dll.eemagine_sdk_get_data(stream_id, buf, n_bytes), "get_impedance_state")
        return [float(buf[ch]) for ch in range(channel_count)]
