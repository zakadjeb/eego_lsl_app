import time
from eego_sdk import EegoSdk

with EegoSdk() as sdk:
    amps = sdk.amplifiers()
    if not amps:
        raise SystemExit("No amplifier found")
    amp = amps[0]
    sid = sdk.open_impedance_stream(amp.id)
    try:
        print("Opened impedance stream", sid, "for", amp.serial)
        print("Channels:", sdk.stream_channels(sid))
        while True:
            samples = sdk.get_data(sid)
            if samples:
                print(samples[-1][:10], "...")
            time.sleep(0.25)
    finally:
        sdk.close_stream(sid)
