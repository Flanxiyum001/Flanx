"""Local-first Discord voice conversation for Max.

Only the invoking owner is processed. PCM stays in memory and is discarded after
transcription; raw audio and transcripts are not persisted by this module.
"""
from __future__ import annotations

import array
import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

try:
    from discord.ext import voice_recv
    _AudioSink = voice_recv.AudioSink
except ImportError:
    voice_recv = None
    class _AudioSink:
        def __init__(self, *args, **kwargs):
            pass

WAKE_WORD_RE = re.compile(r"\bhey[\s,!.:-]*max\b", re.IGNORECASE)
SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * CHANNELS * 2
SILENCE_FRAMES_TO_END = 32
MIN_SPEECH_FRAMES = 12
MAX_UTTERANCE_FRAMES = 750
DEFAULT_WAKE_WINDOW_SECONDS = 25.0


class VoiceConfigurationError(RuntimeError):
    """Voice mode is missing an optional dependency or local model configuration."""


def normalize_transcript(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def extract_wake_command(text: str) -> tuple[bool, str]:
    """Return whether a transcript contains 'Hey Max' and the text after it."""
    normalized = normalize_transcript(text)
    match = WAKE_WORD_RE.search(normalized)
    if not match:
        return False, ""
    return True, normalize_transcript(normalized[match.end():].lstrip(" ,.!?:;-"))


def _pcm_rms(pcm: bytes) -> float:
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    if os.sys.byteorder != "little":
        samples.byteswap()
    return math.sqrt(sum(int(x) * int(x) for x in samples) / len(samples))


class VoiceConversation:
    """Owner-only wake-word conversation with local STT/TTS and interruptible playback."""

    def __init__(self, *, voice_client, loop: asyncio.AbstractEventLoop,
                 requester_id: int, reply_callback: Callable[..., object]):
        if voice_recv is None:
            raise VoiceConfigurationError("Install voice dependencies with pip install -r requirements-voice.txt.")
        model_path = os.getenv("MAX_PIPER_MODEL", "")
        if not model_path:
            raise VoiceConfigurationError("Set MAX_PIPER_MODEL to a local Piper .onnx voice model path.")
        if not Path(model_path).is_file():
            raise VoiceConfigurationError("MAX_PIPER_MODEL must point to an existing local .onnx file.")
        if shutil.which(os.getenv("MAX_PIPER_BIN", "piper")) is None:
            raise VoiceConfigurationError("Piper executable not found; install piper-tts or set MAX_PIPER_BIN.")
        self.voice_client = voice_client
        self.loop = loop
        self.requester_id = int(requester_id)
        self.reply_callback = reply_callback
        self._lock = threading.Lock()
        self._pcm = bytearray()
        self._speech_frames = 0
        self._silence_frames = 0
        self._capturing = False
        self._closed = False
        self._wake_until = 0.0
        self._turn_lock = asyncio.Lock()
        self._speaking = False
        self._sink = None
        self._stt_model = None
        self._stt_lock = threading.Lock()
        self._wake_window = max(5.0, float(os.getenv("MAX_VOICE_WAKE_WINDOW_SECONDS", DEFAULT_WAKE_WINDOW_SECONDS)))
        self._rms_threshold = max(50.0, float(os.getenv("MAX_VOICE_RMS_THRESHOLD", "420")))

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Voice session is already closed.")
        self._sink = _OwnerAudioSink(self)
        self.voice_client.listen(self._sink)

    def close(self) -> None:
        self._closed = True
        for method in ("stop_listening", "stop_playing"):
            try:
                getattr(self.voice_client, method)()
            except (AttributeError, RuntimeError):
                pass
        with self._lock:
            self._pcm.clear()
            self._capturing = False
            self._speech_frames = 0
            self._silence_frames = 0

    def _on_frame(self, user, pcm: bytes | None) -> None:
        """Bounded PCM work only; invoked from the voice-recv audio thread."""
        if self._closed or pcm is None or getattr(user, "id", None) != self.requester_id or not pcm:
            return
        voiced = _pcm_rms(pcm) >= self._rms_threshold
        completed = None
        with self._lock:
            if self._closed:
                return
            if voiced and not self._capturing:
                self._capturing = True
                self._pcm.clear()
                self._speech_frames = 0
                self._silence_frames = 0
            if self._capturing:
                self._pcm.extend(pcm)
                if voiced:
                    self._speech_frames += 1
                    self._silence_frames = 0
                    if self._speaking:
                        self.loop.call_soon_threadsafe(self._stop_playback)
                else:
                    self._silence_frames += 1
                if (self._silence_frames >= SILENCE_FRAMES_TO_END
                        or self._speech_frames >= MAX_UTTERANCE_FRAMES):
                    trailing = min(len(self._pcm), self._silence_frames * FRAME_BYTES)
                    if self._speech_frames >= MIN_SPEECH_FRAMES:
                        completed = bytes(self._pcm[:-trailing] if trailing else self._pcm)
                    self._pcm.clear()
                    self._capturing = False
                    self._speech_frames = 0
                    self._silence_frames = 0
        if completed:
            self.loop.call_soon_threadsafe(self._schedule_transcription, completed)

    def _schedule_transcription(self, pcm: bytes) -> None:
        if not self._closed:
            asyncio.create_task(self._handle_utterance(pcm))

    def _load_stt(self):
        with self._stt_lock:
            if self._stt_model is not None:
                return self._stt_model
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise VoiceConfigurationError("faster-whisper is missing; install requirements-voice.txt.") from exc
            device = os.getenv("MAX_STT_DEVICE", "cpu")
            compute_type = os.getenv("MAX_STT_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
            self._stt_model = WhisperModel(os.getenv("MAX_STT_MODEL", "base.en"),
                                           device=device, compute_type=compute_type)
            return self._stt_model

    @staticmethod
    def _pcm_to_float_audio(pcm: bytes):
        try:
            import numpy as np
        except ImportError as exc:
            raise VoiceConfigurationError("NumPy is required by faster-whisper.") from exc
        stereo = np.frombuffer(pcm, dtype="<i2")
        stereo = stereo[:stereo.size - (stereo.size % CHANNELS)]
        if stereo.size == 0:
            return np.zeros(0, dtype=np.float32)
        mono = stereo.reshape(-1, CHANNELS).astype(np.float32).mean(axis=1) / 32768.0
        return np.ascontiguousarray(mono[::3], dtype=np.float32)

    def _transcribe_sync(self, pcm: bytes) -> str:
        audio = self._pcm_to_float_audio(pcm)
        if audio.size == 0:
            return ""
        segments, _ = self._load_stt().transcribe(
            audio, beam_size=1, vad_filter=True, language=os.getenv("MAX_STT_LANGUAGE", "en"),
            condition_on_previous_text=False,
        )
        return normalize_transcript(" ".join(segment.text for segment in segments))

    async def _handle_utterance(self, pcm: bytes) -> None:
        if self._closed:
            return
        try:
            transcript = await asyncio.to_thread(self._transcribe_sync, pcm)
        except Exception as exc:
            logging.getLogger(__name__).warning("Voice transcription unavailable: %s", type(exc).__name__)
            return
        if not transcript or self._closed:
            return
        has_wake, after_wake = extract_wake_command(transcript)
        if has_wake:
            self._wake_until = time.monotonic() + self._wake_window
            prompt = after_wake
            if not prompt:
                await self._speak("I'm here, bro. What's up?")
                return
        elif time.monotonic() <= self._wake_until:
            prompt = transcript
        else:
            return
        async with self._turn_lock:
            if self._closed:
                return
            self._wake_until = time.monotonic() + self._wake_window
            try:
                response = await self.reply_callback(prompt, task="general")
            except Exception as exc:
                logging.getLogger(__name__).warning("Voice model request failed: %s", type(exc).__name__)
                await self._speak("Sorry bro, my model connection failed just now. Try me again.")
                return
            if not isinstance(response, str) or not response.strip():
                await self._speak("I didn't get a useful answer back. Try saying that again.")
                return
            await self._speak(normalize_transcript(response)[:1200])

    def _stop_playback(self) -> None:
        if self._speaking:
            try:
                self.voice_client.stop_playing()
            except (AttributeError, RuntimeError):
                try:
                    self.voice_client.stop()
                except (AttributeError, RuntimeError):
                    pass

    def _synthesize_sync(self, text: str) -> str:
        output_path = None
        try:
            handle = tempfile.NamedTemporaryFile(prefix="max-voice-", suffix=".wav", delete=False)
            output_path = handle.name
            handle.close()
            completed = subprocess.run(
                [os.getenv("MAX_PIPER_BIN", "piper"), "--model", os.environ["MAX_PIPER_MODEL"],
                 "--output_file", output_path],
                input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=max(10, int(os.getenv("MAX_TTS_TIMEOUT_SECONDS", "35"))), check=False,
            )
            if completed.returncode != 0 or not Path(output_path).is_file() or Path(output_path).stat().st_size < 44:
                raise VoiceConfigurationError("Piper could not synthesize this response; check the local voice model.")
            return output_path
        except Exception:
            if output_path:
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise

    async def _speak(self, text: str) -> None:
        if self._closed or not text.strip():
            return
        try:
            wav_path = await asyncio.to_thread(self._synthesize_sync, text)
        except Exception as exc:
            logging.getLogger(__name__).warning("Voice synthesis unavailable: %s", type(exc).__name__)
            return
        if self._closed or not self.voice_client.is_connected():
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            return
        finished = self.loop.create_future()
        def after_playback(error):
            def complete():
                if finished.done():
                    return
                if error:
                    finished.set_exception(error)
                else:
                    finished.set_result(None)
            self.loop.call_soon_threadsafe(complete)
        try:
            import discord
            source = discord.FFmpegPCMAudio(wav_path)
            self._speaking = True
            self.voice_client.play(source, after=after_playback)
            try:
                await finished
            except asyncio.CancelledError:
                self.voice_client.stop_playing()
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning("Discord voice playback failed: %s", type(exc).__name__)
        finally:
            self._speaking = False
            try:
                os.unlink(wav_path)
            except OSError:
                pass


class _OwnerAudioSink(_AudioSink):
    """Decode Opus to PCM and forward only the invoking owner's frames."""
    def __init__(self, conversation: VoiceConversation):
        super().__init__()
        self.conversation = conversation

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data) -> None:
        if user is not None and getattr(user, "id", None) == self.conversation.requester_id:
            self.conversation._on_frame(user, getattr(data, "pcm", None))

    def cleanup(self) -> None:
        return
