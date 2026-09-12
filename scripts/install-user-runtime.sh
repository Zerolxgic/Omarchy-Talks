#!/usr/bin/env bash
# Install Omarchy Talks and its pinned VoiceBox runtime in user-owned XDG paths.
set -euo pipefail

readonly voicebox_url=https://github.com/jamiepine/voicebox.git
readonly voicebox_revision=51f49dea198384b4eb6087b72c17057c6eb1c1cd
readonly service_name=omarchy-talks-voicebox.service
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
runtime_root="$data_home/omarchy-talks"
voicebox_source="$runtime_root/voicebox-src"
voicebox_venv="$runtime_root/voicebox-venv"
unit_dir="$config_home/systemd/user"
config_dir="$config_home/omarchy-talks"
plugin_dir="$config_home/omarchy/plugins"
extension_dir="$config_home/omarchy/extensions"
menu_file="$extension_dir/omarchy-menu.jsonc"
hypr_dir="$config_home/hypr"
bindings_file="$hypr_dir/bindings.lua"
readonly binding_start="-- >>> Omarchy Talks managed bindings >>>"
readonly binding_end="-- <<< Omarchy Talks managed bindings <<<"
readonly legacy_binding_start="# >>> Omarchy Talks managed bindings >>>"
readonly legacy_binding_end="# <<< Omarchy Talks managed bindings <<<"
enable=false
dry_run=false

usage() { cat <<'EOF'
Usage: scripts/install-user-runtime.sh [--enable] [--dry-run]

Installs only user-owned files. It never invokes sudo or installs system
packages. Missing system prerequisites are reported for the user to install.
The VoiceBox runtime is pinned and deliberately installs only the proven
English Kokoro engine dependencies using CPU-only PyTorch.
EOF
}
run() { if "$dry_run"; then printf '+ '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }
require() { command -v "$1" >/dev/null || { echo "Missing required command: $1" >&2; return 1; }; }
wait_for_voicebox() {
  local attempt
  for attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:17493/health >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "VoiceBox API did not become ready at http://127.0.0.1:17493/health within 30 seconds; profile bootstrap was not run" >&2
  return 1
}
wait_for_plugins() {
  local snapshot attempt missing
  for attempt in {1..30}; do
    snapshot=$(omarchy-shell shell listPlugins 2>/dev/null || true)
    if printf '%s' "$snapshot" | jq -e 'map(.id) | index("omarchy-talks.controls") and index("omarchy-talks.settings")' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  missing=$(printf '%s' "$snapshot" | jq -r 'map(.id) | ["omarchy-talks.controls","omarchy-talks.settings"] - . | join(", ")' 2>/dev/null || printf 'unavailable')
  echo "Omarchy Shell plugin registry did not register: $missing" >&2
  return 1
}
resolve_omarchy_path() {
  local candidate=${OMARCHY_PATH:-/usr/share/omarchy}
  if [[ -d "$candidate" && -x "$candidate/bin/omarchy-plugin-enable" ]]; then
    export OMARCHY_PATH="$candidate"
    return 0
  fi
  echo "Omarchy installation not found: OMARCHY_PATH=${OMARCHY_PATH:-unset}; expected a valid /usr/share/omarchy with bin/omarchy-plugin-enable" >&2
  return 1
}
while (($#)); do
  case "$1" in
    --enable) enable=true ;;
    --dry-run) dry_run=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
resolve_omarchy_path
for command in git uv curl wl-paste pw-play omarchy-shell systemctl; do require "$command"; done
for plugin in omarchy-talks.controls omarchy-talks.settings; do
  test -f "$project_dir/plugins/$plugin/manifest.json" || { echo "Missing plugin: $plugin" >&2; exit 1; }
done
run install -d -m 0755 "$runtime_root" "$unit_dir" "$config_dir" "$plugin_dir" "$extension_dir"
if "$dry_run"; then
  run git clone "$voicebox_url" "$voicebox_source"
  run git -C "$voicebox_source" fetch --depth=1 origin "$voicebox_revision"
  run git -C "$voicebox_source" checkout --detach "$voicebox_revision"
  run uv venv --python 3.12 "$voicebox_venv"
  run uv pip install --torch-backend cpu --python "$voicebox_venv/bin/python" -r "$project_dir/resources/voicebox-kokoro-requirements.txt"
  run uv tool install --editable "$project_dir" --force
  run install -m 0644 "$project_dir/resources/systemd/$service_name" "$unit_dir/$service_name"
  printf 'Would preserve an existing config, link both plugins, and reload systemd.\n'
  exit 0
fi
if [[ ! -d "$voicebox_source/.git" ]]; then run git clone "$voicebox_url" "$voicebox_source"; fi
if [[ -n $(git -C "$voicebox_source" status --porcelain) ]]; then
  echo "Refusing to alter a modified VoiceBox checkout: $voicebox_source" >&2; exit 1
fi
run git -C "$voicebox_source" fetch --depth=1 origin "$voicebox_revision"
run git -C "$voicebox_source" checkout --detach "$voicebox_revision"
if [[ ! -x "$voicebox_venv/bin/python" ]]; then run uv venv --python 3.12 "$voicebox_venv"; fi
run uv pip install --torch-backend cpu --python "$voicebox_venv/bin/python" -r "$project_dir/resources/voicebox-kokoro-requirements.txt"
run uv tool install --editable "$project_dir" --force
run install -m 0644 "$project_dir/resources/systemd/$service_name" "$unit_dir/$service_name"
if [[ ! -e "$config_dir/config.toml" ]]; then
  run install -m 0600 "$project_dir/config/config.toml.example" "$config_dir/config.toml"
else
  echo "Keeping existing configuration: $config_dir/config.toml"
fi
for plugin in omarchy-talks.controls omarchy-talks.settings; do
  target="$plugin_dir/$plugin"
  if [[ -e "$target" && ! -L "$target" ]]; then
    echo "Refusing to replace non-symlink plugin: $target" >&2; exit 1
  fi
  run ln -sfn "$project_dir/plugins/$plugin" "$target"
done
run omarchy-shell shell rescanPlugins
wait_for_plugins
for plugin in omarchy-talks.controls omarchy-talks.settings; do
  run omarchy-plugin-enable "$plugin"
done
if [[ ! -f "$menu_file" ]]; then
  printf '{\n}\n' > "$menu_file"
fi
if ! grep -Fq '"setup.omarchy-talks"' "$menu_file"; then
  run cp "$menu_file" "$menu_file.omarchy-talks.bak"
  if ! "$dry_run"; then sed -i '$i\  "setup.omarchy-talks": {"icon":"󰔊","label":"Omarchy Talks","description":"Choose the reader voice","action":"omarchy-shell shell summon omarchy-talks.settings"},' "$menu_file"; fi
fi
if [[ ! -f "$bindings_file" ]]; then
  echo "Missing Omarchy bindings file: $bindings_file" >&2; exit 1
fi
if ! grep -Fqx -- "$binding_start" "$bindings_file"; then
  run cp "$bindings_file" "$bindings_file.omarchy-talks.bak"
  if ! "$dry_run"; then
    if grep -Fqx -- "$legacy_binding_start" "$bindings_file"; then
      sed -i "\\|^${legacy_binding_start}$|,\\|^${legacy_binding_end}$|d" "$bindings_file"
    fi
    {
      printf '\n%s\n' "$binding_start"
      printf '%s\n' 'o.bind("SUPER + ALT + R", "Read/replace selection", "omarchy-talks speak-selection")'
      printf '%s\n' 'o.bind("SUPER + ALT + SHIFT + R", "Stop speech", "omarchy-talks stop")'
      printf '%s\n' "$binding_end"
    } >> "$bindings_file"
  else
    printf 'Would add the documented read/stop bindings to %s.\n' "$bindings_file"
  fi
fi
run systemctl --user daemon-reload
if "$enable"; then
  run systemctl --user enable "$service_name"
  run systemctl --user restart "$service_name"
  wait_for_voicebox
  run omarchy-talks profile bootstrap
fi
echo "Installed Omarchy Talks. Runtime: $runtime_root"
echo "VoiceBox pinned at: $voicebox_revision"
echo "Run 'omarchy-talks doctor' after service startup."
