"""Command parsing and user-facing failures."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from .config import Config, ReaderError
from .playback import check_player
from .runtime import StreamTrace, speak as stream_speak
from .segmenter import segment
from .selection import SelectionError, capture_selected_text
from .session import ActiveSession, SessionRegistry, SessionStopped
from .ui import PLUGIN_ID, ShellPublisher
from .voicebox import VoiceBox


def main(argv=None):
    parser = argparse.ArgumentParser(prog="omarchy-talks")
    sub = parser.add_subparsers(dest="command", required=True)
    speak = sub.add_parser("speak", help="Read supplied text through VoiceBox")
    speak.add_argument("text")
    sub.add_parser(
        "speak-selection",
        help="Read the current Wayland primary selection through VoiceBox",
    )
    sub.add_parser("stop", help="Stop the active Omarchy Talks read")
    sub.add_parser("pause", help="Pause the active Omarchy Talks read")
    sub.add_parser("resume", help="Resume the paused Omarchy Talks read")
    sub.add_parser("toggle-pause", help="Toggle pause for the active read")
    status = sub.add_parser("status", help="Show authoritative reader state")
    status.add_argument("--json", action="store_true", dest="as_json")
    voices = sub.add_parser("voices", help="List available Kokoro voices")
    voices.add_argument("--json", action="store_true", dest="as_json")
    voice = sub.add_parser("voice", help="Inspect or change the persistent Kokoro voice")
    voice_sub = voice.add_subparsers(dest="voice_command", required=True)
    voice_current = voice_sub.add_parser("current", help="Show the active voice")
    voice_current.add_argument("--json", action="store_true", dest="as_json")
    voice_set = voice_sub.add_parser("set", help="Select a Kokoro preset voice")
    voice_set.add_argument("voice_id")
    voice_set.add_argument("--json", action="store_true", dest="as_json")
    profile = sub.add_parser("profile", help="Manage the OT-owned bootstrap profile")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_sub.add_parser("bootstrap", help="Create or reuse the first-run Kokoro profile")
    sub.add_parser("doctor", help="Check the desktop reader runtime")
    args = parser.parse_args(argv)
    publisher = ShellPublisher()
    try:
        if args.command == "status":
            result = SessionRegistry().status()
            if args.as_json:
                print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            else:
                print(_status_text(result))
            return 0
        if args.command in {"voices", "voice"}:
            config = Config.from_env()
            client = VoiceBox(config)
            if args.command == "voices":
                active = client.active_voice()
                result = {
                    "engine": "kokoro",
                    "voices": [
                        {
                            "voice_id": item["voice_id"],
                            "name": item["name"],
                            "language": item.get("language"),
                            "gender": item.get("gender"),
                            "active": item["voice_id"] == active["voice_id"],
                        }
                        for item in client.kokoro_voices()
                    ],
                }
                if args.as_json:
                    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
                else:
                    for item in result["voices"]:
                        marker = "*" if item["active"] else " "
                        details = ", ".join(value for value in
                                            (item["gender"], item["language"]) if value)
                        print(f"{marker} {item['voice_id']}\t{item['name']}" +
                              (f" ({details})" if details else ""))
                return 0
            if args.voice_command == "current":
                result = client.active_voice()
            else:
                profile = client.ensure_kokoro_profile(args.voice_id)
                saved = config.persist_profile_id(profile["id"])
                result = {
                    "voice_id": profile["preset_voice_id"],
                    "profile_id": saved.profile_id,
                    "name": profile.get("name") or profile["preset_voice_id"],
                }
            if args.as_json:
                print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            else:
                print(f"{result['voice_id']}\t{result['name']}\t{result['profile_id']}")
            return 0
        if args.command == "profile":
            try:
                config = Config.from_env()
                client = VoiceBox(config)
                if client.profile_available():
                    print(config.profile_id)
                    return 0
            except ReaderError:
                config = Config.from_sources(require_profile=False)
                client = VoiceBox(config)
            saved = config.persist_profile_id(client.bootstrap_profile()["id"])
            print(saved.profile_id)
            return 0
        if args.command == "stop":
            outcome, _ = SessionRegistry().stop_active()
            messages = {
                "stopped": "stopped active speech",
                "inactive": "no active speech",
                "stale-recovered": "recovered stale session state; no active speech",
            }
            print(f"omarchy-talks: {messages[outcome]}")
            return 0
        if args.command in {"pause", "resume", "toggle-pause"}:
            result = SessionRegistry().control_active(args.command)
            outcome = result["outcome"]
            if outcome == "error":
                raise ReaderError(f"Session: {result.get('message', 'control failed')}")
            print(f"omarchy-talks: {outcome.replace('-', ' ')}")
            return 0 if outcome not in {"unavailable"} else 1
        if args.command == "doctor":
            return _doctor()
        try:
            text = args.text if args.command == "speak" else capture_selected_text()
        except SelectionError as exc:
            publisher.notice("info", "No text selected")
            raise exc
        if not text.strip():
            raise ReaderError("Input: text must not be empty or whitespace-only")
        if len(text) > 50000:
            raise ReaderError("Input: text exceeds VoiceBox's 50,000 character limit")
        config = Config.from_env()
        player = check_player()
        client = VoiceBox(config)
        trace = StreamTrace(text, segment(text))
        trace_path = os.environ.get("OMARCHY_TALKS_TRACE_PATH")
        session = ActiveSession(SessionRegistry(), client, trace, publisher)
        try:
            with session.signal_guard():
                stream_speak(client, player, text, trace, session)
        except SessionStopped:
            print("omarchy-talks: stopped")
            for failure in session.cancellation_failures:
                print(f"omarchy-talks: cancellation warning: {failure}", file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            session.request_stop()
            raise
        except BaseException as exc:
            session.fail(str(exc))
            raise
        finally:
            session.close()
            if trace_path:
                try:
                    trace.write(trace_path)
                except OSError as exc:
                    raise ReaderError(f"Trace: could not write {trace_path}: {exc}") from exc
        return 0
    except ReaderError as exc:
        print(f"omarchy-talks: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("omarchy-talks: interrupted", file=sys.stderr)
        return 130


def _status_text(result: dict) -> str:
    if not result["active"]:
        return "omarchy-talks: idle"
    return (
        f"omarchy-talks: {result['state']} "
        f"(session {result['session_id']})"
    )


def _doctor() -> int:
    checks = []
    try:
        config = Config.from_env()
        checks.append((True, f"config: {Config.default_path()}"))
    except ReaderError as exc:
        config = None
        checks.append((False, str(exc)))
    for executable in ("wl-paste", "pw-play", "omarchy-shell"):
        path = shutil.which(executable)
        checks.append((path is not None, f"{executable}: {path or 'missing'}"))
    try:
        service = subprocess.run(
            ["systemctl", "--user", "is-active", "omarchy-talks-voicebox.service"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=2,
        )
        checks.append((service.returncode == 0,
                       f"VoiceBox service: {service.stdout.strip() or 'unavailable'}"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        checks.append((False, f"VoiceBox service: unavailable ({exc})"))
    if config is not None:
        try:
            client = VoiceBox(config)
            status, _ = client.request("GET", "/health", timeout=2)
            checks.append((status == 200, f"VoiceBox API: HTTP {status}"))
            available = client.profile_available() if status == 200 else False
            checks.append((available, "VoiceBox profile: " +
                           ("available" if available else "missing")))
        except ReaderError as exc:
            checks.append((False, str(exc)))
    checks.append((True, f"UI plugin contract: {PLUGIN_ID}"))
    for passed, message in checks:
        print(f"{'ok' if passed else 'FAIL'}  {message}")
    return 0 if all(passed for passed, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
