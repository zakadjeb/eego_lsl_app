from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from eego_sdk import EegoSdk, EegoSdkError
from lsl_streams import make_outlet


@dataclass
class WorkerConfig:
    amplifier_id: int
    serial: str
    channel_names: list[str]
    sampling_rate: int = 512
    reference_range: float = 0.15
    bipolar_range: float = 4.0
    impedance_poll_hz: float = 4.0
    stream_lsl: bool = True
    # Number of referential SDK channels to stream. For the EE-21x/EE-22x
    # 64-channel pinning, EOG is Ref 32 and therefore belongs inside this order.
    # Do not request bipolar channels merely to get EOG.
    selected_reference_count: int = 64
    eog_channel_name: str | None = None  # kept for compatibility; not used as bipolar fallback
    # Names to map onto impedance_reference columns. This may include cap
    # auxiliary contacts such as EOG and REF. GND is mapped from impedance_ground.
    impedance_channel_names: list[str] | None = None


class StreamWorker(threading.Thread):
    def __init__(self, mode: str, config: WorkerConfig, out_queue: queue.Queue):
        super().__init__(daemon=True)
        if mode not in {"impedance", "eeg"}:
            raise ValueError("mode must be 'impedance' or 'eeg'")
        self.mode = mode
        self.config = config
        self.out_queue = out_queue
        self.stop_event = threading.Event()
        self.stream_id: int | None = None
        self.sdk: EegoSdk | None = None
        self._close_lock = threading.Lock()
        self._stream_closed = False

    def stop(self) -> None:
        self.stop_event.set()
        # Try to interrupt blocking get_data calls by closing the SDK stream immediately.
        self._close_stream_safely()

    def _close_stream_safely(self) -> None:
        with self._close_lock:
            if self._stream_closed or self.sdk is None or self.stream_id is None:
                return
            try:
                self.sdk.close_stream(self.stream_id)
            except Exception:
                pass
            finally:
                self._stream_closed = True

    def run(self) -> None:
        try:
            with EegoSdk() as sdk:
                self.sdk = sdk
                if self.mode == "impedance":
                    self._run_impedance(sdk)
                else:
                    self._run_eeg(sdk)
        except Exception as exc:
            self.out_queue.put({"type": "error", "message": str(exc)})
        finally:
            self.sdk = None
            self.stream_id = None
            self.out_queue.put({"type": "stopped", "mode": self.mode})

    def _reference_mask(self) -> int:
        """Bitmask for the first N reference electrodes in the selected layout.

        ANT/eego uses unsigned 64-bit masks. For 64 channels, all bits must be
        set. For smaller layouts, only the first N reference channels are used.
        """
        n = max(0, min(64, int(self.config.selected_reference_count or len(self.config.channel_names))))
        if n >= 64:
            return 0xFFFFFFFFFFFFFFFF
        if n == 0:
            return 0
        return (1 << n) - 1

    def _channel_type_summary(self, channels) -> str:
        counts = {}
        for ch in channels:
            counts[ch.type] = counts.get(ch.type, 0) + 1
        return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) or "none"

    def _channel_debug_list(self, channels, limit: int = 96) -> str:
        """Compact list of returned SDK columns for debugging channel mapping."""
        parts = [f"col{i}:{ch.type}[code={ch.type_code}, idx={ch.index}]" for i, ch in enumerate(channels[:limit])]
        if len(channels) > limit:
            parts.append(f"...+{len(channels)-limit} more")
        return "; ".join(parts)

    def _selected_eeg_indices_and_names(self, channels):
        """Return EEG data columns for SDK referential channels only.

        Appendix A of the EE-21x/EE-22x manual lists EOG as Ref 32, i.e. as one
        of the 64 referential inputs. Therefore the app streams EOG when it is
        present in the loaded 64-channel cap file, but it does so as a reference
        channel, not by opening bipolar channel 1.
        """
        n = max(0, min(64, int(self.config.selected_reference_count or len(self.config.channel_names))))
        layout_names = list(self.config.channel_names)[:n]
        indices = [i for i, ch in enumerate(channels) if ch.type == "reference"][:n]
        names = layout_names[:len(indices)]
        if len(names) < len(indices):
            names.extend([f"REF{idx + 1}" for idx in range(len(names), len(indices))])
        missing = len([i for i, ch in enumerate(channels) if ch.type == "reference"]) == 0
        return indices, names, missing, "EOG is treated as Ref 32 if present in the loaded cap layout; bip_mask=0"

    def _selected_impedance_indices_and_names(self, channels):
        """Return impedance columns using the *actual* impedance-stream layout.

        Important correction from the diagnostic output:

        In the ANT/eego impedance stream, the first 64 columns can be reported by
        the wrapper as ordinary ``reference`` channels even though their values
        are impedance states. The extra physical reference contact is then
        reported as ``impedance_reference`` and ground as ``impedance_ground``.

        Therefore, for a 64-channel cap, the correct mapping is:

          - columns 0..63 with type ``reference`` -> Appendix-A Ref 1..64
            (Fp1, Fpz, ..., EOG as Ref 32, ..., Oz)
          - first extra ``impedance_reference`` column -> REF
          - first ``impedance_ground`` column -> GND

        We should not require the first 64 electrode columns to be labelled
        ``impedance_reference``. That was the v22 bug and caused only REF/GND to
        be used.
        """
        manual_ref_names = list(self.config.channel_names)[:64]
        indices: list[int] = []
        names: list[str] = []
        aux_found: list[str] = []
        used: set[int] = set()

        # Electrode impedance values: in the impedance stream these often appear
        # as type "reference" / code 0. Treat the first selected reference
        # columns as impedance columns and label them by the manual order.
        reference_cols = [(i, ch) for i, ch in enumerate(channels) if ch.type == "reference" or ch.type_code == 0]
        n_ref = min(len(manual_ref_names), len(reference_cols), max(0, min(64, int(self.config.selected_reference_count or 64))))
        for ordinal, (i, ch) in enumerate(reference_cols[:n_ref]):
            indices.append(i)
            names.append(manual_ref_names[ordinal])
            used.add(i)

        if n_ref:
            aux_found.append(
                f"electrode impedances=first {n_ref} REFERENCE columns in impedance stream "
                f"[columns 0..{n_ref-1} mapped to Appendix-A Ref order]"
            )

        # Extra REF contact: wrapper enum says 7 = IMPEDANCE_REFERENCE.
        # Legacy direct C++ enum can use 5 for impedance_reference.
        extra_ref_codes = {7}
        if not any(ch.type == "impedance_reference" or ch.type_code == 7 for ch in channels):
            extra_ref_codes.add(5)

        extra_ref_count = 0
        for i, ch in enumerate(channels):
            if i in used:
                continue
            if ch.type == "impedance_reference" or ch.type_code in extra_ref_codes:
                name = "REF" if extra_ref_count == 0 else f"IMP_REF_EXTRA{extra_ref_count + 1}"
                indices.append(i)
                names.append(name)
                used.add(i)
                aux_found.append(
                    f"{name}=IMPEDANCE_REFERENCE [stream_column={i}, sdk_index={ch.index}, code={ch.type_code}]"
                )
                extra_ref_count += 1

        # Ground contact: wrapper enum says 8 = IMPEDANCE_GROUND. Legacy direct
        # C++ enum can use 6.
        ground_codes = {8, 6}
        for i, ch in enumerate(channels):
            if i in used:
                continue
            if ch.type == "impedance_ground" or ch.type_code in ground_codes:
                indices.append(i)
                names.append("GND")
                used.add(i)
                aux_found.append(
                    f"GND=IMPEDANCE_GROUND [stream_column={i}, sdk_index={ch.index}, code={ch.type_code}]"
                )
                break

        missing_impedance_refs = n_ref == 0
        return indices, names, missing_impedance_refs, aux_found

    def _filter_rows(self, rows, indices):
        out = []
        for row in rows:
            out.append([float(row[i]) for i in indices if i < len(row)])
        return out

    def _run_impedance(self, sdk: EegoSdk) -> None:
        ref_mask = self._reference_mask()
        self.stream_id = sdk.open_impedance_stream(self.config.amplifier_id, ref_mask=ref_mask)
        self._stream_closed = False
        channels = sdk.stream_channels(self.stream_id)
        indices, names, fallback, aux_found = self._selected_impedance_indices_and_names(channels)
        aux_text = f" Aux contacts: {', '.join(aux_found)}." if aux_found else " No EOG/GND auxiliary impedance channels reported by SDK."
        self.out_queue.put({
            "type": "info",
            "message": (
                f"Impedance state channel types: {self._channel_type_summary(channels)}. "
                f"Displaying/streaming {len(names)} impedance channel(s) in kOhm. "
                f"SDK GetData() is treated as a latest-known impedance state, not an EEG-like sample stream. "
                f"Cap-file contacts are mapped from impedance_reference plus GND from impedance_ground; "
                f"ref_mask=0x{ref_mask:016X}."
                f"{aux_text}"
            ),
        })
        self.out_queue.put({"type": "info", "message": "Impedance SDK columns: " + self._channel_debug_list(channels)})
        if fallback:
            self.out_queue.put({
                "type": "error",
                "message": (
                    "The impedance stream did not report any impedance_reference channels. "
                    "To avoid accidental bipolar/reference-channel mapping, no fallback mapping is used. "
                    "Check SDK version/channel types in the diagnostic line above."
                ),
            })
        outlet = None
        if self.config.stream_lsl:
            outlet = make_outlet(f"eego-{self.config.serial}-Impedance", "Impedance", names, 0.0, "kOhm")
        self.out_queue.put({"type": "started", "mode": "impedance", "channels": names})
        sleep_s = max(0.02, 1.0 / max(self.config.impedance_poll_hz, 0.1))
        try:
            while not self.stop_event.is_set():
                # The SDK manual says the impedance stream is actually a state:
                # GetData() returns one latest-known impedance vector, not a
                # continuous data block. Read exactly one state and display/stream
                # that state. Values are raw Ohm from the SDK.
                state_raw_ohm = sdk.get_impedance_state(self.stream_id)
                latest_raw_ohm = [float(state_raw_ohm[i]) for i in indices if i < len(state_raw_ohm)]
                if latest_raw_ohm:
                    latest_kohm = [float(x) / 1000.0 for x in latest_raw_ohm]
                    if outlet:
                        outlet.push_sample(latest_kohm)
                    self.out_queue.put({"type": "impedance", "names": names, "values": latest_kohm, "raw_ohm": latest_raw_ohm})
                time.sleep(sleep_s)
        finally:
            self._close_stream_safely()

    def _nearest(self, requested, available):
        if not available:
            return requested
        return min(available, key=lambda x: abs(float(x) - float(requested)))

    def _run_eeg(self, sdk: EegoSdk) -> None:
        # The ANT/eego SDK only accepts specific hardware ranges. Passing an
        # unsupported value can trigger the SDK error:
        #   not implemented: function: edi::eego::utils::range_convert
        # Therefore we query the connected amplifier and coerce to a supported
        # value before opening the stream.
        available_rates = sdk.sampling_rates(self.config.amplifier_id)
        available_ref_ranges = sdk.reference_ranges(self.config.amplifier_id)
        available_bip_ranges = sdk.bipolar_ranges(self.config.amplifier_id)

        sampling_rate = int(self._nearest(self.config.sampling_rate, available_rates))
        reference_range = float(self._nearest(self.config.reference_range, available_ref_ranges))
        bipolar_range = float(self._nearest(self.config.bipolar_range, available_bip_ranges))

        notes = []
        if sampling_rate != self.config.sampling_rate:
            notes.append(f"sampling rate {self.config.sampling_rate} → {sampling_rate} Hz")
        if abs(reference_range - self.config.reference_range) > 1e-12:
            notes.append(f"reference range {self.config.reference_range:g} → {reference_range:g} V")
        if abs(bipolar_range - self.config.bipolar_range) > 1e-12:
            notes.append(f"bipolar range {self.config.bipolar_range:g} → {bipolar_range:g} V")
        if notes:
            self.out_queue.put({"type": "info", "message": "Adjusted EEG settings to supported SDK values: " + ", ".join(notes)})

        ref_mask = self._reference_mask()
        # Keep EEG strictly to referential channels. EOG is Ref 32 in the manual
        # pinning, so it is already included in ref_mask/ref channel order when
        # the loaded cap file contains it. Do not request bipolar channels here.
        bip_mask = 0
        self.stream_id = sdk.open_eeg_stream(
            self.config.amplifier_id,
            sampling_rate=sampling_rate,
            reference_range=reference_range,
            bipolar_range=bipolar_range,
            ref_mask=ref_mask,
            bip_mask=bip_mask,
        )
        self._stream_closed = False
        channels = sdk.stream_channels(self.stream_id)
        indices, names, fallback, eog_source = self._selected_eeg_indices_and_names(channels)
        self.out_queue.put({
            "type": "info",
            "message": (
                f"EEG stream channel types: {self._channel_type_summary(channels)}. "
                f"Displaying/streaming {len(names)} referential EEG channel(s); "
                f"ref_mask=0x{ref_mask:016X}, bip_mask=0x{bip_mask:016X}. "
                f"{eog_source}."
            ),
        })
        self.out_queue.put({"type": "info", "message": "EEG SDK columns: " + self._channel_debug_list(channels)})
        if fallback:
            self.out_queue.put({
                "type": "error",
                "message": (
                    "The EEG stream did not report any reference channels. "
                    "To avoid streaming the wrong channel type, no fallback mapping is used."
                ),
            })
        outlet = None
        if self.config.stream_lsl:
            outlet = make_outlet(f"eego-{self.config.serial}-EEG", "EEG", names, sampling_rate, "microvolts")
        self.out_queue.put({"type": "started", "mode": "eeg", "channels": names})
        last_gui = 0.0
        gui_rows_uv = []
        try:
            while not self.stop_event.is_set():
                samples = sdk.get_data(self.stream_id)
                if samples:
                    # Keep only selected EEG columns: reference electrodes plus EOG if available.
                    eeg_rows = self._filter_rows(samples, indices)
                    # The SDK usually returns EEG in volts. Convert to microvolts for LSL/user display.
                    rows_uv = [[float(x) * 1_000_000.0 for x in row] for row in eeg_rows]
                    if not rows_uv:
                        continue
                    if outlet:
                        for row_uv in rows_uv:
                            outlet.push_sample(row_uv)

                    # Keep the GUI responsive: collect samples and send small blocks at ~10 Hz.
                    gui_rows_uv.extend(rows_uv)
                    if len(gui_rows_uv) > 512:
                        gui_rows_uv = gui_rows_uv[-512:]

                    now = time.time()
                    if now - last_gui > 0.10:
                        latest_uv = rows_uv[-1]
                        self.out_queue.put({"type": "eeg", "names": names, "values": latest_uv})
                        if gui_rows_uv:
                            self.out_queue.put({"type": "eeg_block", "names": names, "samples": gui_rows_uv[-256:]})
                            gui_rows_uv.clear()
                        last_gui = now
                else:
                    time.sleep(0.005)
        finally:
            self._close_stream_safely()

    def _names_for_channels(self, channels) -> list[str]:
        # Prefer layout names for reference EEG channels. Extra SDK channels keep their numeric/type label.
        out = []
        layout_names = list(self.config.channel_names)
        ref_i = 0
        for ch in channels:
            if ch.type == "reference" and ref_i < len(layout_names):
                out.append(layout_names[ref_i])
                ref_i += 1
            else:
                out.append(f"{ch.type}_{ch.index}")
        return out
