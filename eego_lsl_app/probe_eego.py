from eego_sdk import EegoSdk

with EegoSdk() as sdk:
    print("SDK version:", sdk.version())
    amps = sdk.amplifiers()
    print("Amplifiers:", amps)
    for amp in amps:
        print("Channels for", amp.serial, sdk.amplifier_channels(amp.id)[:20], "...")
        print("Sampling rates:", sdk.sampling_rates(amp.id))
        print("Reference ranges:", sdk.reference_ranges(amp.id))
        print("Bipolar ranges:", sdk.bipolar_ranges(amp.id))
