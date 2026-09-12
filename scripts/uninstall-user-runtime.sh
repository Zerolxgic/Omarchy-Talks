#!/usr/bin/env bash
# Remove only files installed by install-user-runtime.sh; retain user data/config.
set -euo pipefail
readonly service_name=omarchy-talks-voicebox.service
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
purge=false
dry_run=false
while (($#)); do
  case "$1" in
    --purge) purge=true ;;
    --dry-run) dry_run=true ;;
    -h|--help) echo 'Usage: scripts/uninstall-user-runtime.sh [--dry-run] [--purge]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
run() { if "$dry_run"; then printf '+ '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }
resolve_omarchy_path() {
  local candidate=${OMARCHY_PATH:-/usr/share/omarchy}
  if [[ -d "$candidate" && -x "$candidate/bin/omarchy-plugin-disable" ]]; then
    export OMARCHY_PATH="$candidate"
    return 0
  fi
  echo "Omarchy installation not found: OMARCHY_PATH=${OMARCHY_PATH:-unset}; expected a valid /usr/share/omarchy with bin/omarchy-plugin-disable" >&2
  return 1
}
resolve_omarchy_path
unit="$config_home/systemd/user/$service_name"
bindings_file="$config_home/hypr/bindings.lua"
menu_file="$config_home/omarchy/extensions/omarchy-menu.jsonc"
readonly binding_start="-- >>> Omarchy Talks managed bindings >>>"
readonly binding_end="-- <<< Omarchy Talks managed bindings <<<"
readonly legacy_binding_start="# >>> Omarchy Talks managed bindings >>>"
readonly legacy_binding_end="# <<< Omarchy Talks managed bindings <<<"
run systemctl --user disable --now "$service_name" 2>/dev/null || true
run rm -f "$unit"
for plugin in omarchy-talks.controls omarchy-talks.settings; do
  link="$config_home/omarchy/plugins/$plugin"
  [[ -L "$link" ]] && run rm -f "$link"
  run omarchy-plugin-disable "$plugin" 2>/dev/null || true
done
run omarchy-shell shell rescanPlugins
if [[ -f "$menu_file" ]] && grep -Fq '"setup.omarchy-talks"' "$menu_file"; then
  run cp "$menu_file" "$menu_file.omarchy-talks-uninstall.bak"
  run sed -i '/"setup.omarchy-talks"/d' "$menu_file"
fi
if [[ -f "$bindings_file" ]] && grep -Fqx -- "$binding_start" "$bindings_file"; then
  run cp "$bindings_file" "$bindings_file.omarchy-talks-uninstall.bak"
  run sed -i "\\|^${binding_start}$|,\\|^${binding_end}$|d" "$bindings_file"
fi
if [[ -f "$bindings_file" ]] && grep -Fqx -- "$legacy_binding_start" "$bindings_file"; then
  run sed -i "\\|^${legacy_binding_start}$|,\\|^${legacy_binding_end}$|d" "$bindings_file"
fi
run systemctl --user daemon-reload
if "$purge"; then run rm -rf "$data_home/omarchy-talks" "$config_home/omarchy-talks"; else echo "Kept user data and config. Re-run with --purge to remove them."; fi
