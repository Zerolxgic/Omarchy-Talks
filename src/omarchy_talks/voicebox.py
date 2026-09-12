"""VoiceBox public HTTP contract; no provider or storage internals."""
import http.client
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .config import Config, ReaderError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VoiceBox:
    BOOTSTRAP_NAME = "Omarchy Talks Default"
    def __init__(self, config: Config):
        self.config = config
        self.opener = build_opener(NoRedirect)

    def request(self, method, path, payload=None, timeout=10.0):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.config.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, response.read()
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise ReaderError(f"VoiceBox {method} {path}: connection/read failed: {exc}") from exc

    @staticmethod
    def json_body(body):
        try:
            return json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise ReaderError("VoiceBox: invalid JSON response") from exc

    def generate(self, text):
        status, body = self.request("POST", "/generate", {
            "text": text, "profile_id": self.config.profile_id, "engine": "kokoro",
        })
        if status != 200:
            raise ReaderError(f"VoiceBox generation submission: HTTP {status}: {body[:300].decode(errors='replace')}")
        result = self.json_body(body)
        generation_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ReaderError("VoiceBox generation submission: response has no valid generation ID")
        return generation_id

    def audio(self, generation_id):
        deadline = time.monotonic() + self.config.timeout
        path = "/audio/" + quote(generation_id, safe="")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReaderError("VoiceBox audio polling: timed out waiting for WAV")
            status, body = self.request("GET", path, timeout=min(10.0, remaining))
            if time.monotonic() >= deadline:
                raise ReaderError("VoiceBox audio polling: timed out waiting for WAV")
            if status == 200:
                return body
            if status != 404 or self.json_body(body) != {"detail": "Audio file not found"}:
                raise ReaderError(f"VoiceBox audio polling: unexpected HTTP {status}: {body[:300].decode(errors='replace')}")
            time.sleep(min(self.config.poll_interval, max(0, deadline - time.monotonic())))

    def cancel(self, generation_id):
        path = "/generate/" + quote(generation_id, safe="") + "/cancel"
        status, body = self.request("POST", path)
        if status != 200:
            raise ReaderError(
                f"VoiceBox cancellation: HTTP {status}: "
                f"{body[:300].decode(errors='replace')}"
            )
        result = self.json_body(body)
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, str) or not message.strip():
            raise ReaderError("VoiceBox cancellation: response has no message")
        return message

    def profile_available(self) -> bool:
        return any(profile["id"] == self.config.profile_id
                   for profile in self.profiles())

    def profiles(self) -> list[dict]:
        """Return the public VoiceBox profile inventory with a stable shape."""
        status, body = self.request("GET", "/profiles", timeout=2)
        if status != 200:
            raise ReaderError(f"VoiceBox profiles: HTTP {status}")
        profiles = self.json_body(body)
        if not isinstance(profiles, list) or not all(
                isinstance(profile, dict) and isinstance(profile.get("id"), str)
                for profile in profiles):
            raise ReaderError("VoiceBox profiles: invalid response shape")
        return profiles

    def kokoro_voices(self) -> list[dict]:
        status, body = self.request("GET", "/profiles/presets/kokoro", timeout=5)
        if status != 200:
            raise ReaderError(f"VoiceBox Kokoro presets: HTTP {status}")
        result = self.json_body(body)
        voices = result.get("voices") if isinstance(result, dict) else None
        if not isinstance(voices, list) or not all(
                isinstance(voice, dict) and isinstance(voice.get("voice_id"), str) and
                isinstance(voice.get("name"), str)
                for voice in voices):
            raise ReaderError("VoiceBox Kokoro presets: invalid response shape")
        return voices

    def active_voice(self) -> dict:
        profile = next((item for item in self.profiles()
                        if item["id"] == self.config.profile_id), None)
        if profile is None:
            raise ReaderError("VoiceBox profile: configured profile is missing")
        if profile.get("preset_engine") != "kokoro" or not isinstance(
                profile.get("preset_voice_id"), str):
            raise ReaderError("VoiceBox profile: configured profile is not a Kokoro preset")
        return {
            "voice_id": profile["preset_voice_id"],
            "profile_id": profile["id"],
            "name": profile.get("name") or profile["preset_voice_id"],
        }

    def ensure_kokoro_profile(self, voice_id: str) -> dict:
        voices = {voice["voice_id"]: voice for voice in self.kokoro_voices()}
        voice = voices.get(voice_id)
        if voice is None:
            raise ReaderError(f"VoiceBox voice: unknown Kokoro voice {voice_id!r}")
        for profile in self.profiles():
            if (profile.get("preset_engine") == "kokoro" and
                    profile.get("preset_voice_id") == voice_id):
                return profile
        payload = {
            "name": f"Omarchy Talks — {voice['name']}",
            "description": f"Omarchy Talks Kokoro preset {voice_id}",
            "language": voice.get("language", "en"),
            "voice_type": "preset",
            "preset_engine": "kokoro",
            "preset_voice_id": voice_id,
            "default_engine": "kokoro",
        }
        status, body = self.request("POST", "/profiles", payload, timeout=10)
        if status != 200:
            raise ReaderError(f"VoiceBox voice profile creation: HTTP {status}: {body[:300].decode(errors='replace')}")
        profile = self.json_body(body)
        if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
            raise ReaderError("VoiceBox voice profile creation: invalid response shape")
        return profile

    def bootstrap_profile(self) -> dict:
        matches = [p for p in self.profiles() if p.get("name") == self.BOOTSTRAP_NAME]
        if len(matches) > 1:
            raise ReaderError("VoiceBox bootstrap: multiple Omarchy Talks Default profiles found")
        if matches:
            profile = matches[0]
        else:
            voices = {v["voice_id"]: v for v in self.kokoro_voices()}
            if "af_heart" not in voices:
                raise ReaderError("VoiceBox bootstrap: Kokoro preset af_heart is unavailable")
            payload = {"name": self.BOOTSTRAP_NAME, "description": "Omarchy Talks first-run default", "language": "en", "voice_type": "preset", "preset_engine": "kokoro", "preset_voice_id": "af_heart", "default_engine": "kokoro"}
            status, body = self.request("POST", "/profiles", payload, timeout=10)
            if status != 200:
                raise ReaderError(f"VoiceBox bootstrap: profile creation HTTP {status}")
            profile = self.json_body(body)
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        verified = next((p for p in self.profiles() if p.get("id") == profile_id), None)
        if not isinstance(profile_id, str) or not verified or verified.get("name") != self.BOOTSTRAP_NAME or verified.get("preset_engine") != "kokoro" or verified.get("preset_voice_id") != "af_heart":
            raise ReaderError("VoiceBox bootstrap: created profile verification failed")
        return verified
