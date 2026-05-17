from __future__ import annotations

from typing import Sequence


class LslUnavailable(RuntimeError):
    pass


def make_outlet(name: str, stream_type: str, channel_names: Sequence[str], sampling_rate: float, unit: str):
    try:
        from pylsl import StreamInfo, StreamOutlet
    except Exception as exc:
        raise LslUnavailable("pylsl is not installed. Run: pip install pylsl") from exc

    info = StreamInfo(name, stream_type, len(channel_names), sampling_rate, "float32", name)
    desc = info.desc()
    channels = desc.append_child("channels")
    for ch_name in channel_names:
        ch = channels.append_child("channel")
        ch.append_child_value("label", str(ch_name))
        ch.append_child_value("unit", unit)
        ch.append_child_value("type", stream_type)
    return StreamOutlet(info)
