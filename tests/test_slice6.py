import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from omarchy_talks.cli import main
from omarchy_talks.config import Config, ReaderError
from omarchy_talks.voicebox import VoiceBox


class VoiceConfigTests(unittest.TestCase):
    def test_persist_profile_id_preserves_other_voicebox_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[voicebox]\nurl = 'http://127.0.0.1:17493'\npoll_interval = 0.1\nprofile_id = 'old'\n")
            config = Config.from_sources(environ={}, path=path)
            saved = config.persist_profile_id("new", environ={})
            self.assertEqual(saved.profile_id, "new")
            self.assertEqual(Config.from_sources(environ={}, path=path).profile_id, "new")
            self.assertIn("poll_interval = 0.1", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_persist_profile_id_rejects_environment_override(self):
        config = Config("http://localhost", "old")
        with self.assertRaisesRegex(ReaderError, "overrides"):
            config.persist_profile_id("new", environ={"OMARCHY_TALKS_PROFILE_ID": "env"})


class VoiceBoxVoiceTests(unittest.TestCase):
    def client(self):
        return VoiceBox(Config("http://localhost", "current"))

    def test_kokoro_voices_rejects_bad_shape(self):
        client = self.client()
        client.request = Mock(return_value=(200, b'{"voices":[{"voice_id":"a"}]}'))
        with self.assertRaisesRegex(ReaderError, "invalid response"):
            client.kokoro_voices()

    def test_ensure_reuses_existing_kokoro_profile(self):
        client = self.client()
        client.request = Mock()
        client.kokoro_voices = Mock(return_value=[{"voice_id": "af_bella", "name": "Bella"}])
        profile = {"id": "existing", "preset_engine": "kokoro", "preset_voice_id": "af_bella"}
        client.profiles = Mock(return_value=[profile])
        self.assertEqual(client.ensure_kokoro_profile("af_bella"), profile)
        client.request.assert_not_called()

    def test_ensure_creates_missing_kokoro_profile(self):
        client = self.client()
        client.kokoro_voices = Mock(return_value=[{"voice_id": "af_bella", "name": "Bella", "language": "en"}])
        client.profiles = Mock(return_value=[])
        client.request = Mock(return_value=(200, b'{"id":"created","preset_voice_id":"af_bella"}'))
        profile = client.ensure_kokoro_profile("af_bella")
        self.assertEqual(profile["id"], "created")
        self.assertEqual(client.request.call_args.args[:2], ("POST", "/profiles"))
        self.assertEqual(client.request.call_args.args[2]["preset_voice_id"], "af_bella")

    def test_bootstrap_creates_and_verifies_only_the_ot_profile(self):
        client = self.client()
        created = {
            "id": "ot-default", "name": VoiceBox.BOOTSTRAP_NAME,
            "preset_engine": "kokoro", "preset_voice_id": "af_heart",
        }
        # An unrelated af_heart profile must not be adopted.
        client.profiles = Mock(side_effect=[
            [{"id": "other", "name": "Personal", "preset_engine": "kokoro",
              "preset_voice_id": "af_heart"}],
            [created],
        ])
        client.kokoro_voices = Mock(return_value=[{"voice_id": "af_heart", "name": "Heart"}])
        client.request = Mock(return_value=(200, b'{"id":"ot-default"}'))
        self.assertEqual(client.bootstrap_profile(), created)
        self.assertEqual(client.request.call_args.args[:2], ("POST", "/profiles"))
        self.assertEqual(client.request.call_args.args[2]["name"], VoiceBox.BOOTSTRAP_NAME)

    def test_bootstrap_reuses_exactly_one_existing_ot_profile(self):
        client = self.client()
        profile = {
            "id": "ot-default", "name": VoiceBox.BOOTSTRAP_NAME,
            "preset_engine": "kokoro", "preset_voice_id": "af_heart",
        }
        client.profiles = Mock(side_effect=[[profile], [profile]])
        client.request = Mock()
        self.assertEqual(client.bootstrap_profile(), profile)
        client.request.assert_not_called()

    def test_bootstrap_rejects_ambiguous_or_unverified_profile(self):
        client = self.client()
        client.profiles = Mock(return_value=[
            {"id": "one", "name": VoiceBox.BOOTSTRAP_NAME},
            {"id": "two", "name": VoiceBox.BOOTSTRAP_NAME},
        ])
        with self.assertRaisesRegex(ReaderError, "multiple"):
            client.bootstrap_profile()

        client.profiles = Mock(side_effect=[[], []])
        client.kokoro_voices = Mock(return_value=[{"voice_id": "af_heart", "name": "Heart"}])
        client.request = Mock(return_value=(200, b'{"id":"created"}'))
        with self.assertRaisesRegex(ReaderError, "verification failed"):
            client.bootstrap_profile()


class VoiceCliTests(unittest.TestCase):
    def test_voices_json_marks_active_voice(self):
        client = Mock()
        client.active_voice.return_value = {"voice_id": "af_heart", "profile_id": "p", "name": "Heart"}
        client.kokoro_voices.return_value = [
            {"voice_id": "af_bella", "name": "Bella", "language": "en", "gender": "female"},
            {"voice_id": "af_heart", "name": "Heart", "language": "en", "gender": "female"},
        ]
        output = io.StringIO()
        with patch("omarchy_talks.cli.Config.from_env", return_value=Config("http://localhost", "p")), \
             patch("omarchy_talks.cli.VoiceBox", return_value=client), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(["voices", "--json"]), 0)
        self.assertIn('"voice_id":"af_heart"', output.getvalue())
        self.assertIn('"active":true', output.getvalue())

    def test_voice_set_persists_profile_after_public_resolution(self):
        config = Mock()
        config.persist_profile_id.return_value = Config("http://localhost", "created")
        client = Mock()
        client.ensure_kokoro_profile.return_value = {
            "id": "created", "preset_voice_id": "af_bella", "name": "Omarchy Talks — Bella",
        }
        with patch("omarchy_talks.cli.Config.from_env", return_value=config), \
             patch("omarchy_talks.cli.VoiceBox", return_value=client), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["voice", "set", "af_bella"]), 0)
        client.ensure_kokoro_profile.assert_called_once_with("af_bella")
        config.persist_profile_id.assert_called_once_with("created")

    def test_invalid_voice_does_not_persist_or_change_active_config(self):
        config = Mock()
        client = Mock()
        client.ensure_kokoro_profile.side_effect = ReaderError("VoiceBox voice: unknown Kokoro voice 'missing'")
        with patch("omarchy_talks.cli.Config.from_env", return_value=config), \
             patch("omarchy_talks.cli.VoiceBox", return_value=client), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["voice", "set", "missing"]), 1)
        config.persist_profile_id.assert_not_called()

    def test_profile_bootstrap_preserves_a_valid_selected_profile(self):
        config = Config("http://localhost", "selected")
        client = Mock()
        client.profile_available.return_value = True
        output = io.StringIO()
        with patch("omarchy_talks.cli.Config.from_env", return_value=config), \
             patch("omarchy_talks.cli.VoiceBox", return_value=client), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(["profile", "bootstrap"]), 0)
        client.bootstrap_profile.assert_not_called()
        self.assertEqual(output.getvalue().strip(), "selected")


if __name__ == "__main__":
    unittest.main()
