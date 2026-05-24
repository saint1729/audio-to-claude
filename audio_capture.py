from pathlib import Path
import tempfile

import numpy as np
import sounddevice as sd
import soundfile as sf


class AudioCapture:
    def __init__(
        self,
        device_name: str,
        sample_rate: int = 16_000,
        channels: int = 1,
    ) -> None:
        self.device_name = device_name
        self.sample_rate = sample_rate
        self.channels = channels

    def find_input_device(self) -> int:
        devices = sd.query_devices()

        for index, device in enumerate(devices):
            name = str(device.get("name", ""))
            max_input_channels = int(device.get("max_input_channels", 0))

            if (
                self.device_name.lower() in name.lower()
                and max_input_channels > 0
            ):
                return index

        available = "\n".join(
            f"{i}: {device['name']}"
            for i, device in enumerate(devices)
        )

        raise RuntimeError(
            f"Could not find input device containing "
            f"'{self.device_name}'.\n\nAvailable devices:\n{available}"
        )

    def record_chunk(self, seconds: int) -> Path:
        device_index = self.find_input_device()

        audio = sd.rec(
            int(seconds * self.sample_rate),
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            device=device_index,
        )
        sd.wait()

        temp_file = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        )
        temp_path = Path(temp_file.name)
        temp_file.close()

        sf.write(temp_path, audio, self.sample_rate)
        return temp_path

    def record_until_silence(
        self,
        min_seconds: float = 2.0,
        max_seconds: float = 6.0,
        silence_duration: float = 0.8,
        silence_threshold: float = 0.005,
        pre_roll: "np.ndarray | None" = None,
    ) -> "tuple[Path, np.ndarray]":
        """Record until a trailing silence is detected or max_seconds is reached.

        Prepends `pre_roll` audio (tail of the previous chunk) so the first
        words of each new utterance are never dropped.

        Returns (wav_path, tail) where tail is the last 0.5 s of audio,
        ready to be passed as pre_roll to the next call.
        """
        device_index = self.find_input_device()
        block_secs = 0.1
        block_size = int(block_secs * self.sample_rate)
        min_blocks = int(min_seconds / block_secs)
        max_blocks = int(max_seconds / block_secs)
        need_silent = int(silence_duration / block_secs)

        frames: list[np.ndarray] = []
        silent_count = 0

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            device=device_index,
            blocksize=block_size,
        ) as stream:
            while len(frames) < max_blocks:
                block, _ = stream.read(block_size)
                frames.append(block.copy())
                if len(frames) >= min_blocks:
                    rms = float(np.sqrt(np.mean(block ** 2)))
                    if rms < silence_threshold:
                        silent_count += 1
                        if silent_count >= need_silent:
                            break
                    else:
                        silent_count = 0

        recorded = np.concatenate(frames, axis=0)
        # Prepend pre-roll so the model hears audio context before the first word
        audio = np.concatenate([pre_roll, recorded], axis=0) if pre_roll is not None else recorded

        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()
        sf.write(temp_path, audio, self.sample_rate)

        # Return the last 0.5 s as pre-roll for the next chunk
        tail_samples = int(0.5 * self.sample_rate)
        tail = recorded[-tail_samples:]
        return temp_path, tail
