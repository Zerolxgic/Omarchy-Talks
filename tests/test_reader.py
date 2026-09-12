import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave
import subprocess

from omarchy_talks.cli import main
from omarchy_talks.config import Config, ReaderError
from omarchy_talks.playback import play, validate_wav, check_player
from omarchy_talks.runtime import PreparedChunk, StreamTrace, speak
from omarchy_talks.segmenter import (
    MAX_CHUNK_CHARS, MIN_TRAILING_CHUNK_CHARS, normalize_text, segment,
)
from omarchy_talks.selection import SelectionError, capture_selected_text
from omarchy_talks.session import (
    ActiveSession, SessionRecord, SessionRegistry, SessionStopped,
    _process_start_ticks,
)
from omarchy_talks.voicebox import VoiceBox


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x01\x00' * 240)
    return stream.getvalue()


class ReaderTests(unittest.TestCase):
    def client(self, responses, timeout=120):
        client = VoiceBox(Config('http://localhost:17493', 'profile', timeout=timeout))
        client.request = Mock(side_effect=responses)
        return client

    def test_empty_input_before_configuration_or_network(self):
        with patch('omarchy_talks.cli.Config.from_env') as config, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['speak', ' \n\t']), 1)
            config.assert_not_called()

    def test_config_missing_profile_and_bad_values(self):
        for values in ({}, {'OMARCHY_TALKS_PROFILE_ID':'p', 'OMARCHY_TALKS_POLL_INTERVAL':'nan'},
                       {'OMARCHY_TALKS_PROFILE_ID':'p', 'OMARCHY_TALKS_VOICEBOX_URL':'file:///tmp'}):
            with self.subTest(values=values), \
                 patch.dict(os.environ, values, clear=True), \
                 patch.object(Config, 'default_path', return_value=Path('/tmp/ot-missing-config.toml')), \
                 self.assertRaises(ReaderError):
                Config.from_env()

    def test_config_defaults(self):
        with patch.dict(os.environ, {'OMARCHY_TALKS_PROFILE_ID':'p'}, clear=True), \
             patch.object(Config, 'default_path', return_value=Path('/tmp/ot-missing-config.toml')):
            self.assertEqual(Config.from_env().poll_interval, .05)

    def test_expected_poll_then_wav(self):
        audio = wav_bytes()
        client = self.client([(404, b'{"detail":"Audio file not found"}'), (200, audio)])
        with patch('omarchy_talks.voicebox.time.sleep') as sleep:
            self.assertEqual(client.audio('job'), audio)
            sleep.assert_called_once_with(.05)
        self.assertEqual(client.request.call_count, 2)

    def test_immediate_readiness(self):
        self.assertEqual(self.client([(200, b'audio')]).audio('job'), b'audio')

    def test_unexpected_status_and_wrong_404(self):
        for status, body in [(500,b'error'), (302,b'redirect'), (404,b'{"detail":"Generation not found"}'),
                             (404,b'{"detail":"Generation failed; no audio available"}'), (404,b'not json')]:
            with self.subTest(status=status,body=body):
                client = self.client([(status,body)])
                with self.assertRaises(ReaderError):
                    client.audio('job')
                self.assertEqual(client.request.call_count, 1)

    def test_timeout(self):
        client = self.client([(404,b'{"detail":"Audio file not found"}')], timeout=.1)
        with patch('omarchy_talks.voicebox.time.monotonic', side_effect=[0,0,.05,.06,.11]), patch('omarchy_talks.voicebox.time.sleep'):
            with self.assertRaisesRegex(ReaderError,'timed out'):
                client.audio('job')

    def test_generation_contract(self):
        client = self.client([(200,b'{"id":"abc"}')])
        self.assertEqual(client.generate('Hello.'), 'abc')
        client.request.assert_called_once_with('POST','/generate', {'text':'Hello.', 'profile_id':'profile', 'engine':'kokoro'})

    def test_submission_failures(self):
        for response in [(404,b'profile missing'), (400,b'mismatch'), (200,b'{}'), (200,b'[]'),(200,b'not json')]:
            with self.subTest(response=response), self.assertRaises(ReaderError):
                self.client([response]).generate('Hello.')

    def test_cancellation_contract(self):
        client = self.client([(200, b'{"message":"Generation cancellation requested"}')])
        self.assertEqual(client.cancel('job/id'), 'Generation cancellation requested')
        client.request.assert_called_once_with('POST', '/generate/job%2Fid/cancel')

    def test_cancellation_failures(self):
        for response in [(400, b'inactive'), (404, b'missing'), (200, b'{}'), (200, b'[]')]:
            with self.subTest(response=response), self.assertRaises(ReaderError):
                self.client([response]).cancel('job')

    def test_unreachable(self):
        from urllib.error import URLError
        client = VoiceBox(Config('http://localhost','p'))
        client.opener.open = Mock(side_effect=URLError('refused'))
        with self.assertRaisesRegex(ReaderError,'connection/read failed'):
            client.generate('Hello.')

    def test_bad_audio(self):
        for audio in [b'', b'RIFFbad', wav_bytes()[:-1]]:
            with self.subTest(audio=audio[:10]), self.assertRaises(ReaderError):
                validate_wav(audio)

    def test_missing_player(self):
        with patch('omarchy_talks.playback.shutil.which', return_value=None), self.assertRaisesRegex(ReaderError,'missing'):
            check_player()

    def test_playback_and_cleanup(self):
        for code in (0,1):
            paths = []
            process = Mock(returncode=code)
            process.communicate.return_value=(None,b'player error' if code else b'')
            process.poll.return_value=code
            def spawn(args, **kwargs):
                path=Path(args[1]); paths.append(path)
                self.assertEqual(path.read_bytes(), wav_bytes())
                return process
            with self.subTest(code=code), patch('omarchy_talks.playback.subprocess.Popen', side_effect=spawn):
                if code:
                    with self.assertRaisesRegex(ReaderError,'exited 1'): play(wav_bytes(), '/player')
                else:
                    play(wav_bytes(), '/player')
            self.assertFalse(paths[0].parent.exists())

    def test_spawn_failure_cleanup(self):
        paths=[]
        def spawn(args, **kwargs):
            paths.append(Path(args[1])); raise OSError('cannot spawn')
        with patch('omarchy_talks.playback.subprocess.Popen',side_effect=spawn), self.assertRaises(ReaderError):
            play(wav_bytes(), '/player')
        self.assertFalse(paths[0].parent.exists())


class SegmenterTests(unittest.TestCase):
    def assert_content_preserved(self, source, chunks):
        normalized = normalize_text(source)
        cursor = 0
        for chunk in chunks:
            if normalized.startswith(chunk, cursor):
                cursor += len(chunk)
            elif normalized[cursor:cursor + 1] == " " and normalized.startswith(chunk, cursor + 1):
                cursor += len(chunk) + 1
            else:
                self.fail(f"chunk {chunk!r} does not continue normalized source at {cursor}")
        self.assertEqual(cursor, len(normalized))

    def test_one_short_sentence(self):
        self.assertEqual(segment("  One short sentence.\n"), ["One short sentence."])

    def test_multiple_sentences_remain_natural_units(self):
        self.assertEqual(segment("First one. Second one! Third?"),
                         ["First one.", "Second one!", "Third?"])

    def test_sentence_at_cap(self):
        source = "A" * 99 + "."
        self.assertEqual(segment(source), [source])

    def test_long_sentence_prefers_punctuation(self):
        source = "word " * 16 + "pause, " + "tail " * 12 + "end."
        chunks = segment(source)
        self.assertTrue(chunks[0].endswith(","))
        self.assert_content_preserved(source, chunks)

    def test_long_clause_uses_whitespace(self):
        source = " ".join(["word"] * 30)
        chunks = segment(source)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.endswith("word") for chunk in chunks))
        self.assert_content_preserved(source, chunks)

    def test_merges_tiny_fragment_with_next_sentence_without_exceeding_cap(self):
        source = (
            "The most useful thing you can do while using OT tonight is save 2–3 "
            "exact pieces of text whenever it reads something badly. "
            "Don’t paraphrase them; keep the exact punctuation."
        )
        chunks = segment(source)
        self.assertEqual(len(chunks), 2)
        self.assertLessEqual(max(map(len, chunks)), MAX_CHUNK_CHARS)
        self.assertGreaterEqual(len(chunks[-1]), MIN_TRAILING_CHUNK_CHARS)
        self.assertEqual(chunks[-1],
                         "it reads something badly. Don’t paraphrase them; keep the exact punctuation.")
        self.assert_content_preserved(source, chunks)

    def test_oversized_token_uses_hard_fallback(self):
        source = "x" * 237
        chunks = segment(source)
        self.assertEqual([len(chunk) for chunk in chunks], [100, 100, 37])
        self.assertEqual("".join(chunks), source)

    def test_invariants_and_punctuation(self):
        source = " Alpha,   beta gamma!\nDelta; " + "z" * 160 + "? "
        chunks = segment(source)
        self.assertTrue(chunks[0].endswith("!"))
        self.assertTrue(chunks[-1].endswith("?"))
        self.assertTrue(all(0 < len(chunk) <= MAX_CHUNK_CHARS for chunk in chunks))
        self.assert_content_preserved(source, chunks)

    def test_empty_input(self):
        self.assertEqual(segment(" \n\t "), [])


class SelectionTests(unittest.TestCase):
    def completed(self, stdout=b"selected", stderr=b"", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_successful_selected_text_capture_uses_primary_only(self):
        with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("omarchy_talks.selection.subprocess.run", return_value=self.completed()) as run:
            self.assertEqual(capture_selected_text(), "selected")
        run.assert_called_once_with(
            ["/usr/bin/wl-paste", "--primary", "--type", "text", "--no-newline"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1.0,
        )

    def test_multiline_and_surrounding_whitespace_are_preserved(self):
        value = b"  first line\nsecond line  \n"
        with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("omarchy_talks.selection.subprocess.run", return_value=self.completed(value)):
            self.assertEqual(capture_selected_text(), value.decode())

    def test_whitespace_only_selection_is_rejected(self):
        with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("omarchy_talks.selection.subprocess.run", return_value=self.completed(b" \n\t")), \
             self.assertRaisesRegex(SelectionError, "whitespace-only"):
            capture_selected_text()

    def test_missing_capture_executable(self):
        with patch("omarchy_talks.selection.shutil.which", return_value=None), \
             self.assertRaisesRegex(SelectionError, "wl-paste is missing"):
            capture_selected_text()

    def test_nonzero_capture_exit(self):
        result = self.completed(b"", b"selection owner failed", 3)
        with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("omarchy_talks.selection.subprocess.run", return_value=result), \
             self.assertRaisesRegex(SelectionError, "exited 3: selection owner failed"):
            capture_selected_text()

    def test_no_selection_exit_is_specific(self):
        for detail in (b"No selection", b"Nothing is copied"):
            with self.subTest(detail=detail):
                result = self.completed(b"", detail, 1)
                with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
                     patch("omarchy_talks.selection.subprocess.run", return_value=result), \
                     self.assertRaisesRegex(SelectionError, "no primary selection"):
                    capture_selected_text()

    def test_capture_timeout(self):
        with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("omarchy_talks.selection.subprocess.run", side_effect=subprocess.TimeoutExpired([], 1)), \
             self.assertRaisesRegex(SelectionError, "timed out after 1 seconds"):
            capture_selected_text()

    def test_non_utf8_selection_is_rejected(self):
        with patch("omarchy_talks.selection.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("omarchy_talks.selection.subprocess.run", return_value=self.completed(b"\xff")), \
             self.assertRaisesRegex(SelectionError, "not valid UTF-8"):
            capture_selected_text()

    def test_cli_selection_routes_through_existing_replacement_runtime(self):
        selected = "Selected first line.\nSelected second line."
        session = Mock()
        session.signal_guard.return_value = contextlib.nullcontext()
        with patch("omarchy_talks.cli.capture_selected_text", return_value=selected), \
             patch("omarchy_talks.cli.Config.from_env", return_value=Mock()) as config, \
             patch("omarchy_talks.cli.check_player", return_value="/usr/bin/pw-play"), \
             patch("omarchy_talks.cli.VoiceBox", return_value=Mock()) as voicebox, \
             patch("omarchy_talks.cli.ActiveSession", return_value=session) as active, \
             patch("omarchy_talks.cli.stream_speak") as stream:
            self.assertEqual(main(["speak-selection"]), 0)
        config.assert_called_once()
        voicebox.assert_called_once()
        active.assert_called_once()
        self.assertIs(stream.call_args.args[4], session)
        self.assertEqual(stream.call_args.args[2], selected)

    def test_empty_selection_does_not_configure_or_call_voicebox(self):
        with patch("omarchy_talks.cli.capture_selected_text", side_effect=SelectionError("Selection: empty")), \
             patch("omarchy_talks.cli.Config.from_env") as config, \
             patch("omarchy_talks.cli.VoiceBox") as voicebox, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["speak-selection"]), 1)
        config.assert_not_called()
        voicebox.assert_not_called()


class FakeSession:
    def __init__(self, on_wait=None, error=None):
        self.on_wait = on_wait
        self.error = error
        self.returncode = 1 if error else 0
        self.stop_calls = 0

    def wait(self):
        if self.on_wait:
            self.on_wait()
        if self.error:
            raise self.error

    def stop(self, timeout=0.2):
        self.stop_calls += 1


class RuntimeTests(unittest.TestCase):
    def test_one_chunk_reuses_generate_audio_playback(self):
        client = Mock()
        client.generate.return_value = "job-1"
        client.audio.return_value = b"wav-1"
        session = FakeSession()
        with patch("omarchy_talks.runtime.start_playback", return_value=session) as start:
            trace = speak(client, "/player", "Short sentence.")
        client.generate.assert_called_once_with("Short sentence.")
        client.audio.assert_called_once_with("job-1")
        start.assert_called_once_with(b"wav-1", "/player")
        self.assertEqual(trace.data["playback_order"], [1])
        self.assertEqual(trace.data["max_future_chunks"], 0)

    def test_multiple_chunks_overlap_and_play_in_order_one_ahead(self):
        source = "First sentence. Second sentence. Third sentence."
        log = []
        second_ready = threading.Event()

        class Client:
            def generate(self, text):
                number = ["First sentence.", "Second sentence.", "Third sentence."].index(text) + 1
                log.append(f"generate-{number}")
                return f"job-{number}"

            def audio(self, generation_id):
                number = int(generation_id.rsplit("-", 1)[1])
                log.append(f"ready-{number}")
                if number == 2:
                    second_ready.set()
                return f"wav-{number}".encode()

        def start(audio, player):
            number = int(audio.rsplit(b"-", 1)[1])
            log.append(f"play-{number}")
            if number == 1:
                return FakeSession(lambda: self.assertTrue(second_ready.wait(1)))
            return FakeSession()

        with patch("omarchy_talks.runtime.start_playback", side_effect=start):
            trace = speak(Client(), "/player", source)
        self.assertEqual(trace.data["playback_order"], [1, 2, 3])
        self.assertEqual(trace.data["max_future_chunks"], 1)
        self.assertLess(log.index("generate-2"), log.index("play-2"))
        self.assertGreater(log.index("generate-3"), log.index("play-2"))

    def test_next_generation_failure_waits_for_current_then_surfaces(self):
        played = []

        class Client:
            def generate(self, text):
                if text == "Second.":
                    raise ReaderError("next failed")
                return "job-1"

            def audio(self, generation_id):
                return b"wav-1"

        with patch("omarchy_talks.runtime.start_playback",
                   return_value=FakeSession(lambda: played.append("finished"))), \
             self.assertRaisesRegex(ReaderError, "next failed"):
            speak(Client(), "/player", "First. Second.")
        self.assertEqual(played, ["finished"])

    def test_playback_failure_stops_stream(self):
        client = Mock()
        client.generate.side_effect = ["job-1", "job-2"]
        client.audio.side_effect = [b"wav-1", b"wav-2"]
        with patch("omarchy_talks.runtime.start_playback",
                   return_value=FakeSession(error=ReaderError("player failed"))), \
             self.assertRaisesRegex(ReaderError, "player failed"):
            speak(client, "/player", "First. Second. Third.")
        self.assertLessEqual(client.generate.call_count, 2)

    def test_stop_blocks_future_promotion_and_cancels_known_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SessionRegistry(Path(directory) / 'runtime')
            client = Mock()
            client.generate.side_effect = ['job-1', 'job-2']
            client.audio.side_effect = [b'wav-1', b'wav-2']
            trace = StreamTrace('First. Second.', ['First.', 'Second.'])
            active = ActiveSession(registry, client, trace)
            active.claim()
            playback = FakeSession(
                on_wait=active.request_stop,
                error=ReaderError('player terminated for stop'),
            )
            with patch('omarchy_talks.runtime.start_playback', return_value=playback) as start, \
                 self.assertRaises(SessionStopped):
                speak(client, '/player', 'First. Second.', trace, active)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(playback.stop_calls, 1)
            self.assertFalse(registry.is_owner(active.token))
            self.assertGreaterEqual(client.cancel.call_count, 1)

    def test_epoch_mismatch_blocks_playback(self):
        session = Mock(token='new-token')
        session.assert_active = Mock()
        session.stopped.is_set.return_value = False
        prepared = PreparedChunk(0, 'First.', 'job', b'wav', 'old-token')
        with patch('omarchy_talks.runtime._prepare', return_value=prepared), \
             patch('omarchy_talks.runtime.start_playback') as start, \
             self.assertRaisesRegex(SessionStopped, 'stale'):
            speak(Mock(), '/player', 'First.', session=session)
        start.assert_not_called()

    def test_interrupt_reaps_and_cleans(self):
        paths=[]
        process=Mock()
        process.communicate.side_effect=KeyboardInterrupt
        process.poll.return_value=None
        def spawn(args, **kwargs): paths.append(Path(args[1])); return process
        with patch('omarchy_talks.playback.subprocess.Popen',side_effect=spawn), self.assertRaises(KeyboardInterrupt):
            play(wav_bytes(), '/player')
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        self.assertFalse(paths[0].parent.exists())


class SessionTests(unittest.TestCase):
    def registry(self, directory):
        return SessionRegistry(Path(directory) / 'runtime')

    def test_stop_with_no_active_session(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.registry(directory).stop_active(), ('inactive', None))

    def test_cli_stop_with_no_active_session(self):
        registry = Mock()
        registry.stop_active.return_value = ('inactive', None)
        output = io.StringIO()
        with patch('omarchy_talks.cli.SessionRegistry', return_value=registry), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(['stop']), 0)
        self.assertIn('no active speech', output.getvalue())

    def test_stale_pid_state_is_recovered_without_signalling(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            registry.state_path.write_text('{"pid":99999999,"start_ticks":1,"token":"old-old-old-old-token"}')
            with patch.object(registry, '_signal_record') as signal_record:
                self.assertEqual(registry.stop_active(), ('stale-recovered', None))
            signal_record.assert_not_called()
            self.assertFalse(registry.state_path.exists())

    def test_pid_reuse_mismatch_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            wrong = (_process_start_ticks(os.getpid()) or 0) + 1
            registry.state_path.write_text(
                f'{{"pid":{os.getpid()},"start_ticks":{wrong},"token":"old-old-old-old-token"}}'
            )
            self.assertEqual(registry.stop_active(), ('stale-recovered', None))

    def test_stop_terminates_only_owned_player_and_attempts_all_cancels(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            client = Mock()
            client.cancel.side_effect = ['current cancelled', ReaderError('already complete')]
            active = ActiveSession(registry, client)
            active.claim()
            owned = FakeSession()
            unrelated = FakeSession()
            active.set_playback(owned)
            active.register_generation('current-job')
            active.register_generation('future-job')
            active.request_stop()
            self.assertEqual(owned.stop_calls, 1)
            self.assertEqual(unrelated.stop_calls, 0)
            self.assertEqual({call.args[0] for call in client.cancel.call_args_list},
                             {'current-job', 'future-job'})
            self.assertEqual(len(active.cancellation_failures), 1)
            self.assertFalse(registry.state_path.exists())

    def test_old_cleanup_cannot_delete_new_session_state(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            ticks = _process_start_ticks(os.getpid())
            old = SessionRecord(os.getpid(), ticks, 'old-old-old-old-token')
            new = SessionRecord(os.getpid(), ticks, 'new-new-new-new-token')
            with registry._locked():
                registry._write_locked(old)
                registry._write_locked(new)
            self.assertFalse(registry.release(old.token))
            self.assertTrue(registry.is_owner(new.token))

    def test_repeated_claim_replaces_old_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            old = ActiveSession(registry, Mock())
            old.claim()
            new = ActiveSession(registry, Mock())

            def release_old(record):
                self.assertEqual(record.token, old.token)
                registry.release(old.token)
                return True

            with patch.object(registry, '_signal_record', side_effect=release_old):
                new.claim()
            self.assertTrue(new.replaced_existing)
            self.assertTrue(registry.is_owner(new.token))
            self.assertFalse(registry.release(old.token))

    def test_runtime_directory_and_state_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            active = ActiveSession(registry, Mock())
            active.claim()
            self.assertEqual(registry.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(registry.state_path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
