"""Best-effort Omarchy shell publication behind a narrow adapter."""
import json
import shutil
import subprocess


PLUGIN_ID = "omarchy-talks.controls"


class ShellPublisher:
    def __init__(self, executable: str = "omarchy-shell", timeout: float = 1.0):
        self.executable = executable
        self.timeout = timeout

    def _run(self, arguments: list[str]) -> tuple[bool, str]:
        executable = shutil.which(self.executable)
        if executable is None:
            return False, ""
        try:
            result = subprocess.run(
                [executable, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, ""
        return result.returncode == 0, result.stdout.strip()

    @staticmethod
    def _payload(payload: dict) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def show(self, payload: dict) -> bool:
        ok, answer = self._run(
            ["shell", "summon", PLUGIN_ID, self._payload(payload)]
        )
        return ok and answer == "ok"

    def update(self, payload: dict) -> bool:
        encoded = self._payload(payload)
        ok, answer = self._run(
            ["shell", "call", PLUGIN_ID, "update", encoded]
        )
        if ok and answer == "ok":
            return True
        return self.show(payload)

    def notice(self, level: str, message: str) -> bool:
        return self.update({
            "protocol": 1,
            "kind": "notice",
            "level": level,
            "message": message,
        })


def session_payload(event: str, session_id: str, state: str,
                    message: str | None = None) -> dict:
    payload = {
        "protocol": 1,
        "kind": "session",
        "event": event,
        "session_id": session_id,
        "state": state,
        "message": message,
    }
    return payload
