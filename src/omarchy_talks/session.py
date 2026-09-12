"""Secure per-user active-session ownership without a daemon."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import stat
import tempfile
import threading
import time
import uuid

from .config import ReaderError
from .ui import ShellPublisher, session_payload


class SessionStopped(Exception):
    """The active read was intentionally stopped or replaced."""


@dataclass(frozen=True)
class SessionRecord:
    pid: int
    start_ticks: int
    token: str
    state: str = "preparing"


ACTIVE_STATES = {"preparing", "playing", "paused"}
SESSION_STATES = ACTIVE_STATES | {"finished", "stopped", "error"}
CONTROL_SIGNALS = {
    "pause": signal.SIGUSR1,
    "resume": signal.SIGUSR2,
    "toggle-pause": signal.SIGRTMIN,
}
SIGNAL_CONTROLS = {value: key for key, value in CONTROL_SIGNALS.items()}
CONTROL_CODES = {name: index for index, name in enumerate(CONTROL_SIGNALS, 1)}
CODE_CONTROLS = {value: key for key, value in CONTROL_CODES.items()}


def _process_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rfind(")") + 2:].split()[19])
    except (FileNotFoundError, PermissionError, ValueError, IndexError, OSError):
        return None


class SessionRegistry:
    def __init__(self, root: Path | None = None):
        if root is None:
            xdg = os.environ.get("XDG_RUNTIME_DIR")
            root = (Path(xdg) / "omarchy-talks" if xdg else
                    Path(tempfile.gettempdir()) / f"omarchy-talks-{os.getuid()}")
        self.root = root
        self.lock_path = root / "session.lock"
        self.state_path = root / "active.json"
        self._ensure_root()

    def _ensure_root(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ReaderError("Session: runtime directory is not a user-owned directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            self.root.chmod(0o700)

    @contextmanager
    def _locked(self):
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ReaderError(f"Session: cannot open runtime lock: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_locked(self) -> tuple[SessionRecord | None, bool]:
        try:
            data = json.loads(self.state_path.read_text())
            record = SessionRecord(
                pid=int(data["pid"]),
                start_ticks=int(data["start_ticks"]),
                token=str(data["token"]),
                state=str(data.get("state", "preparing")),
            )
            valid = (record.pid > 0 and len(record.token) >= 16 and
                     record.state in SESSION_STATES and
                     _process_start_ticks(record.pid) == record.start_ticks)
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return None, self.state_path.exists()
        return (record, False) if valid else (None, True)

    def _remove_locked(self, token: str | None = None) -> bool:
        if token is not None:
            record, _ = self._read_locked()
            if record is None or record.token != token:
                return False
        try:
            self.state_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _active_locked(self) -> tuple[SessionRecord | None, bool]:
        record, stale = self._read_locked()
        if stale:
            self._remove_locked()
        return record, stale

    def _write_locked(self, record: SessionRecord):
        temporary = self.root / f"state-{record.pid}-{record.token}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            payload = json.dumps({
                "pid": record.pid,
                "start_ticks": record.start_ticks,
                "token": record.token,
                "state": record.state,
            }).encode()
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.state_path)
        self.state_path.chmod(0o600)

    @staticmethod
    def _signal_record(record: SessionRecord, signum=signal.SIGTERM):
        if _process_start_ticks(record.pid) != record.start_ticks:
            return False
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                fd = os.pidfd_open(record.pid)
                try:
                    signal.pidfd_send_signal(fd, signum)
                finally:
                    os.close(fd)
            else:
                os.kill(record.pid, signum)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def claim_replacing(self, token: str, timeout: float = 5.0) -> bool:
        """Stop any prior owner, wait for release, then claim this process."""
        replaced = False
        deadline = time.monotonic() + timeout
        while True:
            with self._locked():
                record, _ = self._active_locked()
                if record is None:
                    start_ticks = _process_start_ticks(os.getpid())
                    if start_ticks is None:
                        raise ReaderError("Session: cannot identify current process")
                    self._write_locked(SessionRecord(
                        os.getpid(), start_ticks, token, "preparing"
                    ))
                    return replaced
            replaced = True
            self._signal_record(record)
            while time.monotonic() < deadline:
                with self._locked():
                    current, _ = self._active_locked()
                    if current is None or current.token != record.token:
                        break
                time.sleep(0.005)
            else:
                raise ReaderError("Session: active reader did not release ownership")

    def release(self, token: str) -> bool:
        with self._locked():
            return self._remove_locked(token)

    def is_owner(self, token: str) -> bool:
        with self._locked():
            record, _ = self._active_locked()
            return record is not None and record.token == token

    def update_state(self, token: str, state: str) -> bool:
        if state not in SESSION_STATES:
            raise ValueError(f"invalid session state: {state}")
        with self._locked():
            record, _ = self._active_locked()
            if record is None or record.token != token:
                return False
            self._write_locked(SessionRecord(
                record.pid, record.start_ticks, record.token, state,
            ))
            return True

    def status(self) -> dict:
        with self._locked():
            record, _ = self._active_locked()
        if record is None:
            return {"protocol": 1, "active": False, "state": "idle"}
        return {
            "protocol": 1,
            "active": record.state in ACTIVE_STATES,
            "session_id": record.token,
            "state": record.state,
            "can_pause": record.state == "playing",
            "can_resume": record.state == "paused",
            "can_stop": record.state in ACTIVE_STATES,
        }

    def control_active(self, action: str, timeout: float = 2.0) -> dict:
        if action not in {"pause", "resume", "toggle-pause"}:
            raise ValueError(f"invalid control action: {action}")
        with self._locked():
            record, stale = self._active_locked()
        if record is None:
            return {"outcome": "stale-recovered" if stale else "inactive"}
        allowed = (
            (action == "pause" and record.state == "playing") or
            (action == "resume" and record.state == "paused") or
            (action == "toggle-pause" and record.state in {"playing", "paused"})
        )
        if not allowed:
            return {"outcome": "unavailable", "state": record.state}
        expected = "paused" if (action == "pause" or
                                (action == "toggle-pause" and record.state == "playing")) else "playing"
        if not self._signal_record(record, CONTROL_SIGNALS[action]):
            return {"outcome": "stale-recovered"}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._locked():
                current, _ = self._active_locked()
            if current is None or current.token != record.token:
                return {"outcome": "inactive"}
            if current.state == expected:
                return {
                    "outcome": "paused" if expected == "paused" else "resumed",
                    "state": expected,
                }
            time.sleep(0.005)
        raise ReaderError(f"Session: active reader did not acknowledge {action}")

    def stop_active(self, timeout: float = 5.0) -> tuple[str, float | None]:
        with self._locked():
            record, stale = self._active_locked()
        if record is None:
            return ("stale-recovered" if stale else "inactive"), None
        started = time.monotonic()
        if not self._signal_record(record):
            if _process_start_ticks(record.pid) == record.start_ticks:
                raise ReaderError("Session: cannot signal the active reader")
            with self._locked():
                current, _ = self._active_locked()
                if current is not None and current.token == record.token:
                    self._remove_locked(record.token)
            return "stale-recovered", time.monotonic() - started
        deadline = started + timeout
        while time.monotonic() < deadline:
            with self._locked():
                current, _ = self._active_locked()
                if current is None or current.token != record.token:
                    return "stopped", time.monotonic() - started
            time.sleep(0.005)
        raise ReaderError("Session: active reader did not acknowledge stop")


class ActiveSession:
    def __init__(self, registry: SessionRegistry, client, trace=None,
                 publisher=None):
        self.registry = registry
        self.client = client
        self.trace = trace
        self.token = uuid.uuid4().hex
        self.stopped = threading.Event()
        self.current_playback = None
        self._generation_ids = set()
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop_handled = threading.Event()
        self._control_stop = threading.Event()
        self._control_read, self._control_write = os.pipe()
        os.set_blocking(self._control_write, False)
        self._control_thread = None
        self.cancellation_failures = []
        self.replaced_existing = False
        self.state = "preparing"
        self.publisher = publisher or ShellPublisher()
        self._publisher_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="omarchy-talks-ui"
        )

    def _trace(self, name, **values):
        if self.trace is not None:
            self.trace.event(name, None, session_token=self.token, **values)

    def claim(self):
        self.replaced_existing = self.registry.claim_replacing(self.token)
        self._start_control_thread()
        self._trace("session_claimed", replaced_existing=self.replaced_existing)
        self._publisher_executor.submit(
            self.publisher.show,
            session_payload("started", self.token, "preparing"),
        )

    def transition(self, state: str, message: str | None = None):
        with self._state_lock:
            if not self.registry.update_state(self.token, state):
                self.stopped.set()
                raise SessionStopped("stale session event suppressed")
            self.state = state
            self._trace("session_state", state=state, message=message)
        self._publisher_executor.submit(
            self.publisher.update,
            session_payload("state", self.token, state, message),
        )

    def _start_control_thread(self):
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name="omarchy-talks-control",
            daemon=True,
        )
        self._control_thread.start()

    def _control_loop(self):
        while not self._control_stop.is_set():
            try:
                ready, _, _ = select.select([self._control_read], [], [], 0.1)
                if not ready:
                    continue
                commands = os.read(self._control_read, 4096)
            except OSError:
                break
            for code in commands:
                action = CODE_CONTROLS.get(code)
                if action is None:
                    continue
                try:
                    self._handle_control(action)
                except ReaderError as exc:
                    self._trace("control_failure", action=action, error=str(exc))

    def _queue_control(self, signum, _frame):
        action = SIGNAL_CONTROLS.get(signum)
        if action is None:
            return
        try:
            os.write(self._control_write, bytes([CONTROL_CODES[action]]))
        except (BlockingIOError, OSError):
            pass

    def _handle_control(self, action: str) -> dict:
        with self._state_lock:
            if self.stopped.is_set() or not self.registry.is_owner(self.token):
                return {"outcome": "inactive"}
            desired = action
            if action == "toggle-pause":
                desired = "resume" if self.state == "paused" else "pause"
            if desired == "pause":
                if self.state == "paused":
                    return {"outcome": "unchanged", "state": self.state}
                if self.state != "playing" or self.current_playback is None:
                    return {"outcome": "unavailable", "state": self.state}
                self.current_playback.pause()
                self.transition("paused")
                return {"outcome": "paused", "state": self.state}
            if desired == "resume":
                if self.state == "playing":
                    return {"outcome": "unchanged", "state": self.state}
                if self.state != "paused" or self.current_playback is None:
                    return {"outcome": "unavailable", "state": self.state}
                self.current_playback.resume()
                self.transition("playing")
                return {"outcome": "resumed", "state": self.state}
            return {"outcome": "error", "message": "unknown control action"}

    def assert_active(self):
        if self.stopped.is_set() or not self.registry.is_owner(self.token):
            self.stopped.set()
            raise SessionStopped("speech stopped")

    def register_generation(self, generation_id: str):
        with self._lock:
            self._generation_ids.add(generation_id)
            stopped = self.stopped.is_set()
        if stopped:
            self._cancel_one(generation_id)
            raise SessionStopped("speech stopped")

    def unregister_generation(self, generation_id: str):
        with self._lock:
            self._generation_ids.discard(generation_id)

    def start_owned_playback(self, starter):
        """Close the signal race between spawning and recording the owned child."""
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        playback = None
        try:
            self.assert_active()
            playback = starter()
            self.current_playback = playback
            self.transition("playing")
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        self.assert_active()
        return playback

    def set_playback(self, playback):
        self.current_playback = playback

    def clear_playback(self, playback):
        if self.current_playback is playback:
            self.current_playback = None

    def _cancel_one(self, generation_id: str):
        try:
            message = self.client.cancel(generation_id)
            self._trace("cancellation", generation_id=generation_id,
                        outcome="success", message=message)
        except ReaderError as exc:
            self.cancellation_failures.append(str(exc))
            self._trace("cancellation", generation_id=generation_id,
                        outcome="failure", error=str(exc))

    def request_stop(self, *_, state="stopped", message=None):
        if self._stop_handled.is_set():
            return
        self._stop_handled.set()
        self.stopped.set()
        payload = None
        with self._state_lock:
            playback = self.current_playback
            if playback is not None:
                if self.state == "paused":
                    try:
                        playback.resume()
                    except Exception as exc:
                        self._trace("playback_resume_failure", error=str(exc))
                try:
                    playback.stop()
                except Exception as exc:
                    self._trace("playback_stop_failure", error=str(exc))
            try:
                if self.registry.update_state(self.token, state):
                    self.state = state
                    payload = session_payload("state", self.token, state, message)
            except (ReaderError, SessionStopped):
                pass
            self.registry.release(self.token)
        if payload is not None:
            self._publisher_executor.submit(self.publisher.update, payload)
        self._trace("session_invalidated")
        with self._lock:
            generation_ids = tuple(self._generation_ids)
        for generation_id in generation_ids:
            self._cancel_one(generation_id)

    def finish(self):
        self.transition("finished")

    def fail(self, message: str):
        self.request_stop(state="error", message=message)

    @contextmanager
    def signal_guard(self):
        handled = {signal.SIGTERM, *CONTROL_SIGNALS.values()}
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, handled)
        previous = {item: signal.getsignal(item) for item in handled}
        try:
            self.claim()
            signal.signal(signal.SIGTERM, self.request_stop)
            for signum in CONTROL_SIGNALS.values():
                signal.signal(signum, self._queue_control)
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, handled)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            self.registry.release(self.token)

    def close(self):
        self.registry.release(self.token)
        self._control_stop.set()
        try:
            os.write(self._control_write, b"\0")
        except OSError:
            pass
        if self._control_thread is not None and self._control_thread is not threading.current_thread():
            self._control_thread.join(timeout=0.3)
        os.close(self._control_read)
        os.close(self._control_write)
        self._publisher_executor.shutdown(wait=True, cancel_futures=False)
