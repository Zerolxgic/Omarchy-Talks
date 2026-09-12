"""Ordered semantic-streaming orchestration with one future chunk."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import time

from .config import ReaderError
from .playback import start_playback
from .segmenter import segment
from .session import SessionStopped


@dataclass(frozen=True)
class PreparedChunk:
    index: int
    text: str
    generation_id: str
    audio: bytes
    session_token: str | None = None


class StreamTrace:
    """Small in-memory event record, optionally serialized after playback."""

    def __init__(self, source_text: str, chunks: list[str]):
        self.data = {
            "source_text": source_text,
            "chunks": [
                {"index": index, "text": text, "characters": len(text)}
                for index, text in enumerate(chunks, 1)
            ],
            "max_future_chunks": 0,
            "playback_order": [],
            "events": [],
            "result": "running",
        }

    def event(self, name: str, index: int | None, **values) -> None:
        event = {
            "event": name,
            "monotonic_seconds": time.monotonic(),
            **values,
        }
        if index is not None:
            event["chunk"] = index + 1
        self.data["events"].append(event)

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.data, indent=2) + "\n")


def _prepare(client, index: int, text: str, trace: StreamTrace, session=None) -> PreparedChunk:
    if session is not None:
        session.assert_active()
    trace.event("generation_start", index)
    generation_id = client.generate(text)
    if session is not None:
        session.register_generation(generation_id)
    trace.event("generation_submitted", index, generation_id=generation_id)
    audio = client.audio(generation_id)
    trace.event("audio_ready", index, generation_id=generation_id, audio_bytes=len(audio))
    if session is not None:
        session.assert_active()
    return PreparedChunk(index, text, generation_id, audio,
                         session.token if session is not None else None)


def speak(client, player: str, text: str, trace: StreamTrace | None = None,
          session=None) -> StreamTrace:
    chunks = segment(text)
    if not chunks:
        raise ReaderError("Input: text must not be empty or whitespace-only")
    trace = trace or StreamTrace(text, chunks)
    current = _prepare(client, 0, chunks[0], trace, session)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="omarchy-talks-next")
    try:
        for index in range(len(chunks)):
            if session is not None:
                session.assert_active()
                if current.session_token != session.token:
                    raise SessionStopped("stale prepared chunk discarded")
            if session is not None:
                playback = session.start_owned_playback(
                    lambda: start_playback(current.audio, player)
                )
            else:
                playback = start_playback(current.audio, player)
            trace.event("playback_start", current.index, generation_id=current.generation_id)

            future = None
            if index + 1 < len(chunks):
                if session is not None:
                    session.assert_active()
                future = executor.submit(
                    _prepare, client, index + 1, chunks[index + 1], trace, session
                )
                trace.data["max_future_chunks"] = max(trace.data["max_future_chunks"], 1)

            try:
                playback.wait()
                if session is not None:
                    session.clear_playback(playback)
                    session.assert_active()
                trace.event("playback_exit", current.index, exit_code=0)
                trace.data["playback_order"].append(current.index + 1)
                if session is not None:
                    session.unregister_generation(current.generation_id)
            except BaseException as exc:
                if session is not None:
                    session.clear_playback(playback)
                trace.event("playback_exit", current.index, exit_code=playback.returncode)
                if future is not None:
                    future.cancel()
                if session is not None and session.stopped.is_set():
                    raise SessionStopped("speech stopped") from exc
                raise

            if future is not None:
                current = future.result()
                if session is not None:
                    session.assert_active()
                    if current.session_token != session.token:
                        raise SessionStopped("stale prepared chunk discarded")
        trace.data["result"] = "success"
        if session is not None:
            session.finish()
        return trace
    except SessionStopped as exc:
        if session is not None:
            session.request_stop()
        trace.data["result"] = "stopped"
        trace.data["error"] = str(exc)
        raise
    except BaseException as exc:
        trace.data["result"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failure"
        trace.data["error"] = str(exc)
        raise
    finally:
        stopped = session is not None and session.stopped.is_set()
        executor.shutdown(wait=not stopped, cancel_futures=stopped)
