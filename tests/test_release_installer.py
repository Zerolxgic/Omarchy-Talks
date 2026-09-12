"""Regression checks for the user-facing release installer contracts."""
import shutil
import subprocess
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-user-runtime.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall-user-runtime.sh"


class ReleaseInstallerContractTests(unittest.TestCase):
    def test_scripts_are_valid_bash(self):
        for script in (INSTALLER, UNINSTALLER):
            subprocess.run(["bash", "-n", script], check=True)

    def test_plugin_registry_and_voicebox_gates_precede_mutation(self):
        source = INSTALLER.read_text()
        self.assertIn("omarchy-shell shell rescanPlugins", source)
        self.assertIn("omarchy-shell shell listPlugins", source)
        rescan = source.index("omarchy-shell shell rescanPlugins")
        readiness = source.index("wait_for_plugins", rescan)
        self.assertLess(rescan, readiness)
        self.assertLess(readiness, source.index('run omarchy-plugin-enable "$plugin"'))
        self.assertIn("http://127.0.0.1:17493/health", source)
        start = source.index('run systemctl --user restart "$service_name"')
        health = source.index("wait_for_voicebox", start)
        self.assertLess(health, source.index("run omarchy-talks profile bootstrap", health))

    def test_managed_lua_block_compiles_and_has_both_bindings(self):
        source = INSTALLER.read_text()
        self.assertIn('readonly binding_start="-- >>> Omarchy Talks managed bindings >>>"', source)
        self.assertIn('readonly binding_end="-- <<< Omarchy Talks managed bindings <<<"', source)
        self.assertIn('Read/replace selection', source)
        self.assertIn('Stop speech', source)
        lua = '\n'.join([
            'o = { bind = function(...) end }',
            'o.bind("SUPER + ALT + R", "Read/replace selection", "omarchy-talks speak-selection")',
            'o.bind("SUPER + ALT + SHIFT + R", "Stop speech", "omarchy-talks stop")',
            '',
        ])
        if shutil.which("luac") is None:
            self.skipTest("luac is unavailable")
        result = subprocess.run(["luac", "-p", "-"], input=lua, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_removes_only_managed_blocks_and_rescans(self):
        source = UNINSTALLER.read_text()
        self.assertIn('omarchy-shell shell rescanPlugins', source)
        self.assertIn('"setup.omarchy-talks"', source)
        self.assertIn('readonly binding_start="-- >>> Omarchy Talks managed bindings >>>"', source)


if __name__ == "__main__":
    unittest.main()
