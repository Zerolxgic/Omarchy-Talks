"""Validate PCM WAVs and explicitly own pw-play processes/artifacts."""
import io
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import wave

from .config import ReaderError


def check_player():
    player = shutil.which("pw-play")
    if not player:
        raise ReaderError("Playback: pw-play is missing from PATH")
    return player


def validate_wav(audio):
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            expected = frames * wav.getnchannels() * wav.getsampwidth()
            if frames <= 0 or wav.getframerate() <= 0 or len(wav.readframes(frames)) != expected:
                raise ValueError("empty or truncated PCM data")
    except (wave.Error, EOFError, ValueError) as exc:
        raise ReaderError(f"VoiceBox audio: malformed/empty or unsupported PCM WAV: {exc}") from exc


class PlaybackSession:
    def __init__(self, directory, process):
        self._directory = directory
        self.process = process

    @property
    def returncode(self):
        return self.process.returncode

    def wait(self):
        try:
            _, stderr = self.process.communicate()
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self._directory.cleanup()
        if self.process.returncode:
            raise ReaderError(
                f"Playback: pw-play exited {self.process.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

    def stop(self, timeout=0.2):
        """Terminate only this owned player and reap it promptly."""
        try:
            if self.process.poll() is not None:
                return
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            self._directory.cleanup()

    def pause(self):
        if self.process.poll() is not None:
            raise ReaderError("Playback: cannot pause completed pw-play process")
        self.process.send_signal(signal.SIGSTOP)

    def resume(self):
        if self.process.poll() is not None:
            raise ReaderError("Playback: cannot resume completed pw-play process")
        self.process.send_signal(signal.SIGCONT)


def start_playback(audio, player=None):
    validate_wav(audio)
    player = player or check_player()
    directory = tempfile.TemporaryDirectory(prefix="omarchy-talks-")
    path = Path(directory.name) / "speech.wav"
    try:
        path.write_bytes(audio)
        process = subprocess.Popen([player, str(path)], stderr=subprocess.PIPE)
    except OSError as exc:
        directory.cleanup()
        raise ReaderError(f"Playback: {exc}") from exc
    return PlaybackSession(directory, process)


def play(audio, player=None):
    start_playback(audio, player).wait()
