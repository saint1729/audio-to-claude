import os
import time
from pathlib import Path

import numpy as np
import openai
import soundfile as sf
from openai import OpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "whisper-large-v3-turbo"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini-transcribe"

# RMS energy below this level is considered silence (tune if needed)
SILENCE_THRESHOLD = float(os.getenv("SILENCE_THRESHOLD", "0.005"))

# How many characters of previous transcript to feed as context (prompt)
CONTEXT_CHARS = int(os.getenv("TRANSCRIPTION_CONTEXT_CHARS", "256"))


class Transcriber:
    def __init__(self, model: str | None = None, retries: int = 3) -> None:
        provider = os.getenv("TRANSCRIPTION_PROVIDER", "openai").lower()

        if provider == "groq":
            self.client = OpenAI(
                api_key=os.environ["GROQ_API_KEY"],
                base_url=GROQ_BASE_URL,
            )
            self.model = model or os.getenv("TRANSCRIPTION_MODEL", GROQ_DEFAULT_MODEL)
        else:
            self.client = OpenAI()
            self.model = model or os.getenv("TRANSCRIPTION_MODEL", OPENAI_DEFAULT_MODEL)

        self.retries = retries

    def transcribe(self, audio_path: Path, previous_text: str = "") -> str:
        # Skip silent chunks to avoid hallucinations
        audio, _ = sf.read(audio_path, dtype="float32")
        rms = float(np.sqrt(np.mean(audio ** 2)))
        silence_threshold = float(os.getenv("SILENCE_THRESHOLD", str(SILENCE_THRESHOLD)))
        if rms < silence_threshold:
            return ""

        # Pass the tail of the previous transcript as a prompt so the model
        # has rolling context — dramatically reduces cut-off words and ellipsis.
        context_chars = int(os.getenv("TRANSCRIPTION_CONTEXT_CHARS", str(CONTEXT_CHARS)))
        prompt = previous_text[-context_chars:].strip() if previous_text else ""

        last_exc: Exception | None = None

        for attempt in range(self.retries):
            try:
                with audio_path.open("rb") as audio_file:
                    kwargs: dict = dict(model=self.model, file=audio_file)
                    if prompt:
                        kwargs["prompt"] = prompt
                    result = self.client.audio.transcriptions.create(**kwargs)
                text = result.text.strip()
                # Discard placeholder-only responses ("...", "…", etc.)
                if all(c in ".… " for c in text):
                    return ""
                return text
            except (openai.PermissionDeniedError, openai.RateLimitError) as exc:
                last_exc = exc
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                print(f"Transcription error ({exc.status_code}), retrying in {wait}s...")
                time.sleep(wait)

        raise last_exc or RuntimeError("Transcription failed after all retries.")
