#!/usr/bin/env bash
set -euo pipefail

# Default repository URL (override via OPENCODE_AGENT_GUI_REPO)
REPO_URL="${OPENCODE_AGENT_GUI_REPO:-https://github.com/MrTomRod/opencode-agent-gui.git}"
BRANCH="${OPENCODE_AGENT_GUI_BRANCH:-main}"
INSTALL_DIR="${OPENCODE_AGENT_GUI_DIR:-$HOME/Projects/opencode-agent-gui}"

APP_ID="io.github.opencode.AgentGui"
BIN_DIR="$HOME/.local/bin"
BIN_LINK="$BIN_DIR/opencode-agent-gui"
APPS_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APPS_DIR/$APP_ID.desktop"

log() {
  printf '[opencode-agent-gui] %s\n' "$*"
}

fail() {
  printf '[opencode-agent-gui] ERROR: %s\n' "$*" >&2
  exit 1
}

check_deps() {
  python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Gtk, Adw, GtkSource  # noqa: F401
import yaml  # noqa: F401
PY
}

install_deps_if_needed() {
  if check_deps; then
    log "Python GTK4 / Libadwaita / GtkSourceView 5 / PyYAML dependencies are already installed."
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    log "Installing missing Fedora packages via dnf..."
    sudo dnf install -y python3-gobject gtk4 libadwaita gtksourceview5 python3-pyyaml
    check_deps || fail "Dependencies still missing after dnf install."
  else
    fail "Missing required Python/GTK4 dependencies (python3-gobject, gtk4, libadwaita, gtksourceview5, python3-pyyaml)."
  fi
}

sync_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    if git -C "$INSTALL_DIR" remote get-url origin >/dev/null 2>&1; then
      log "Updating existing repository in $INSTALL_DIR..."
      git -C "$INSTALL_DIR" pull --ff-only
    else
      log "Local git repo at $INSTALL_DIR has no 'origin' remote; skipping pull."
    fi
    return 0
  fi

  if [ -f "$INSTALL_DIR/app.py" ]; then
    log "Using existing checkout at $INSTALL_DIR (not a git repo; skipping clone/pull)."
    return 0
  fi

  if ! command -v git >/dev/null 2>&1; then
    fail "git is required to clone $REPO_URL"
  fi

  if [[ "$REPO_URL" == *"YOUR_USERNAME"* ]]; then
    fail "Set OPENCODE_AGENT_GUI_REPO or update REPO_URL in install.sh to your GitHub repository URL."
  fi

  log "Cloning $REPO_URL ($BRANCH) into $INSTALL_DIR..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
}

install_launcher_and_desktop() {
  chmod +x "$INSTALL_DIR/app.py"

  mkdir -p "$BIN_DIR"
  ln -sfn "$INSTALL_DIR/app.py" "$BIN_LINK"
  log "Linked CLI launcher: $BIN_LINK -> $INSTALL_DIR/app.py"

  mkdir -p "$APPS_DIR"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=OpenCode Agents
GenericName=Agent Configuration Editor
Comment=Edit OpenCode v2 agents in YAML with model autocomplete and hot-reload
Exec=$BIN_LINK
Icon=preferences-system-symbolic
Terminal=false
Categories=Development;GTK;
StartupNotify=true
StartupWMClass=$APP_ID
EOF

  if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$DESKTOP_FILE"
  fi

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  fi

  log "Installed desktop entry: $DESKTOP_FILE"
}

main() {
  sync_repo
  install_deps_if_needed
  install_launcher_and_desktop
  log "Done! Launch 'OpenCode Agents' from GNOME or run: opencode-agent-gui"
}

main "$@"
