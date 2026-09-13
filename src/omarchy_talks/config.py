"""Persistent configuration with environment overrides for the reader."""
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any
from urllib.parse import urlsplit


class ReaderError(Exception):
    """An actionable reader failure."""


@dataclass(frozen=True)
class Config:
    base_url: str
    profile_id: str
    poll_interval: float = 0.05
    timeout: float = 120.0
    path: Path | None = None

    @staticmethod
    def default_path(environ: Mapping[str, str] | None = None) -> Path:
        environ = os.environ if environ is None else environ
        base = environ.get("XDG_CONFIG_HOME")
        return ((Path(base) if base else Path.home() / ".config") /
                "omarchy-talks" / "config.toml")

    @classmethod
    def from_sources(
        cls,
        environ: Mapping[str, str] | None = None,
        path: Path | None = None,
        require_profile: bool = True,
    ) -> "Config":
        environ = os.environ if environ is None else environ
        path = path or cls.default_path(environ)
        values: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open("rb") as stream:
                    document = tomllib.load(stream)
                values = document.get("voicebox", {})
                if not isinstance(values, dict):
                    raise ValueError("[voicebox] must be a TOML table")
            except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
                raise ReaderError(f"Configuration: could not read {path}: {exc}") from exc

        url = environ.get(
            "OMARCHY_TALKS_VOICEBOX_URL",
            values.get("url", "http://127.0.0.1:17493"),
        )
        profile = environ.get(
            "OMARCHY_TALKS_PROFILE_ID", values.get("profile_id", "")
        )
        interval = environ.get(
            "OMARCHY_TALKS_POLL_INTERVAL", values.get("poll_interval", 0.05)
        )
        if not isinstance(url, str) or not isinstance(profile, str):
            raise ReaderError("Configuration: VoiceBox URL and profile ID must be strings")
        if interval is None:
            raise ReaderError("Configuration: poll interval must be finite and positive")
        url = url.rstrip("/")
        profile = profile.strip()
        try:
            parsed = urlsplit(url)
            parsed.port
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("invalid backend URL")
            interval = float(interval)
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError("poll interval must be finite and positive")
        except ValueError as exc:
            raise ReaderError(f"Configuration: {exc}") from exc
        if require_profile and not profile:
            raise ReaderError(
                f"Configuration: set voicebox.profile_id in {path} or "
                "OMARCHY_TALKS_PROFILE_ID"
            )
        return cls(url, profile, interval, path=path)

    def persist_profile_id(
        self, profile_id: str, environ: Mapping[str, str] | None = None
    ) -> "Config":
        """Persist a verified VoiceBox profile without changing env precedence."""
        environ = os.environ if environ is None else environ
        if environ.get("OMARCHY_TALKS_PROFILE_ID"):
            raise ReaderError(
                "Configuration: OMARCHY_TALKS_PROFILE_ID overrides the saved voice; "
                "unset it before changing the persistent selection"
            )
        profile_id = profile_id.strip()
        if not profile_id:
            raise ReaderError("Configuration: profile ID must not be empty")
        path = self.path or self.default_path(environ)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_mode & 0o077:
            path.parent.chmod(0o700)
        document = "[voicebox]\n"
        if path.exists():
            try:
                document = path.read_text()
                parsed = tomllib.loads(document)
                if not isinstance(parsed.get("voicebox", {}), dict):
                    raise ValueError("[voicebox] must be a TOML table")
            except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
                raise ReaderError(f"Configuration: could not read {path}: {exc}") from exc
        lines = document.splitlines()
        section_start = next((i for i, line in enumerate(lines)
                              if line.strip() == "[voicebox]"), None)
        if section_start is None:
            if lines and lines[-1]:
                lines.append("")
            lines.append("[voicebox]")
            section_start = len(lines) - 1
        section_end = next((i for i in range(section_start + 1, len(lines))
                            if lines[i].lstrip().startswith("[") and
                            lines[i].rstrip().endswith("]")), len(lines))
        replacement = f'profile_id = {profile_id!r}'
        for i in range(section_start + 1, section_end):
            if lines[i].split("#", 1)[0].strip().startswith("profile_id"):
                lines[i] = replacement
                break
        else:
            lines.insert(section_end, replacement)
        payload = ("\n".join(lines) + "\n").encode()
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-",
                                             delete=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        except OSError as exc:
            raise ReaderError(f"Configuration: could not write {path}: {exc}") from exc
        return Config(self.base_url, profile_id, self.poll_interval, self.timeout, path)

    @classmethod
    def from_env(cls) -> "Config":
        """Compatibility name retained for existing callers and tests."""
        return cls.from_sources()
