import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from omarchy_talks.cli import main
from omarchy_talks.config import Config, ReaderError
from omarchy_talks.session import ActiveSession, SessionRegistry, SessionStopped
from omarchy_talks.ui import PLUGIN_ID, ShellPublisher, session_payload
from omarchy_talks.voicebox import VoiceBox


class FakePublisher:
    def __init__(self):
        self.shown = []
        self.updated = []

    def show(self, payload):
        self.shown.append(payload)
        return True

    def update(self, payload):
        self.updated.append(payload)
        return True


class FakePlayback:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0

    def pause(self):
        self.pause_calls += 1

    def resume(self):
        self.resume_calls += 1

    def stop(self):
        self.stop_calls += 1


class ConfigTests(unittest.TestCase):
    def test_config_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text(
                "[voicebox]\n"
                "url = 'http://127.0.0.1:18000/'\n"
                "profile_id = 'file-profile'\n"
                "poll_interval = 0.2\n"
            )
            config = Config.from_sources({}, path)
            self.assertEqual(config.base_url, "http://127.0.0.1:18000")
            self.assertEqual(config.profile_id, "file-profile")
            self.assertEqual(config.poll_interval, 0.2)

            config = Config.from_sources({
                "OMARCHY_TALKS_VOICEBOX_URL": "http://localhost:19000",
                "OMARCHY_TALKS_PROFILE_ID": "env-profile",
                "OMARCHY_TALKS_POLL_INTERVAL": "0.1",
            }, path)
            self.assertEqual(config.base_url, "http://localhost:19000")
            self.assertEqual(config.profile_id, "env-profile")
            self.assertEqual(config.poll_interval, 0.1)

    def test_built_in_defaults_and_missing_profile(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "missing.toml"
            with self.assertRaisesRegex(ReaderError, "profile_id"):
                Config.from_sources({}, path)
            config = Config.from_sources({
                "OMARCHY_TALKS_PROFILE_ID": "profile",
            }, path)
            self.assertEqual(config.base_url, "http://127.0.0.1:17493")
            self.assertEqual(config.poll_interval, 0.05)

    def test_invalid_toml_and_table_shape_are_actionable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text("[voicebox\n")
            with self.assertRaisesRegex(ReaderError, "could not read"):
                Config.from_sources({}, path)
            path.write_text("voicebox = 'wrong'\n")
            with self.assertRaisesRegex(ReaderError, "must be a TOML table"):
                Config.from_sources({}, path)


class SessionControlTests(unittest.TestCase):
    def test_status_shapes_and_pause_resume_toggle_are_authoritative(self):
        with tempfile.TemporaryDirectory() as root:
            registry = SessionRegistry(Path(root) / "runtime")
            publisher = FakePublisher()
            active = ActiveSession(registry, object(), publisher=publisher)
            playback = FakePlayback()
            try:
                with active.signal_guard():
                    self.assertEqual(registry.status()["state"], "preparing")
                    active.current_playback = playback
                    active.transition("playing")

                    paused = registry.control_active("pause")
                    self.assertEqual(paused, {"outcome": "paused", "state": "paused"})
                    self.assertEqual(playback.pause_calls, 1)
                    self.assertEqual(registry.status()["can_resume"], True)

                    resumed = registry.control_active("resume")
                    self.assertEqual(resumed, {"outcome": "resumed", "state": "playing"})
                    self.assertEqual(playback.resume_calls, 1)

                    toggled = registry.control_active("toggle-pause")
                    self.assertEqual(toggled["state"], "paused")
                    active.request_stop()
                    self.assertEqual(playback.resume_calls, 2)
                    self.assertEqual(playback.stop_calls, 1)
                self.assertEqual(registry.status(), {
                    "protocol": 1, "active": False, "state": "idle"
                })
            finally:
                active.close()

    def test_controls_are_unavailable_while_preparing(self):
        with tempfile.TemporaryDirectory() as root:
            registry = SessionRegistry(Path(root) / "runtime")
            active = ActiveSession(registry, object(), publisher=FakePublisher())
            try:
                with active.signal_guard():
                    self.assertEqual(
                        registry.control_active("pause"),
                        {"outcome": "unavailable", "state": "preparing"},
                    )
            finally:
                active.close()

    def test_stale_session_cannot_publish_state(self):
        with tempfile.TemporaryDirectory() as root:
            registry = SessionRegistry(Path(root) / "runtime")
            publisher = FakePublisher()
            active = ActiveSession(registry, object(), publisher=publisher)
            try:
                registry.claim_replacing(active.token)
                registry.release(active.token)
                with self.assertRaises(SessionStopped):
                    active.transition("playing")
                self.assertEqual(publisher.updated, [])
            finally:
                active.close()

    def test_status_cli_json_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["status", "--json"]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), {
                "protocol": 1, "active": False, "state": "idle"
            })


class PublisherTests(unittest.TestCase):
    def completed(self, stdout="ok\n", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout, "")

    def test_session_payload_contract_and_plugin_id(self):
        self.assertEqual(PLUGIN_ID, "omarchy-talks.controls")
        self.assertEqual(session_payload("state", "opaque", "playing"), {
            "protocol": 1,
            "kind": "session",
            "event": "state",
            "session_id": "opaque",
            "state": "playing",
            "message": None,
        })

    def test_failed_call_falls_back_to_summon(self):
        publisher = ShellPublisher(timeout=0.1)
        with patch("omarchy_talks.ui.shutil.which", return_value="/usr/bin/omarchy-shell"), \
             patch("omarchy_talks.ui.subprocess.run", side_effect=[
                 self.completed("unknown\n"), self.completed("ok\n")
             ]) as run:
            self.assertTrue(publisher.update({"kind": "session"}))
        self.assertEqual(run.call_args_list[0].args[0][1:5], [
            "shell", "call", PLUGIN_ID, "update"
        ])
        self.assertEqual(run.call_args_list[1].args[0][1:4], [
            "shell", "summon", PLUGIN_ID
        ])

    def test_missing_or_failing_shell_is_nonfatal(self):
        publisher = ShellPublisher()
        with patch("omarchy_talks.ui.shutil.which", return_value=None):
            self.assertFalse(publisher.show({"kind": "session"}))
        with patch("omarchy_talks.ui.shutil.which", return_value="shell"), \
             patch("omarchy_talks.ui.subprocess.run", side_effect=OSError("gone")):
            self.assertFalse(publisher.update({"kind": "session"}))


class DoctorContractTests(unittest.TestCase):
    def test_profile_availability_uses_configured_id(self):
        client = VoiceBox(Config("http://localhost:17493", "wanted"))
        client.request = Mock(return_value=(
            200, b'[{"id":"other"},{"id":"wanted"}]'
        ))
        self.assertTrue(client.profile_available())
        client.request.assert_called_once_with("GET", "/profiles", timeout=2)

    def test_service_definition_preserves_required_paths_and_security_boundary(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / "resources/systemd/omarchy-talks-voicebox.service").read_text()
        self.assertIn("WorkingDirectory=%h/.local/share/omarchy-talks/voicebox-src", unit)
        self.assertIn("%h/.local/share/omarchy-talks/voicebox-venv/bin/python", unit)
        self.assertIn("--data-dir %h/.local/share/omarchy-talks/voicebox", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("sudo", unit)
        self.assertNotIn("HSA_OVERRIDE_GFX_VERSION", unit)


if __name__ == "__main__":
    unittest.main()
