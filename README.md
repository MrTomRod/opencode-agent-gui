# OpenCode Agent Config GUI

A native GNOME / Libadwaita GUI for editing OpenCode v2 agent definitions in YAML with live `opencode models` autocomplete and hot-reload (`opencode reload`).

## Features

- **Two-Pane Libadwaita Interface (`Adw.NavigationSplitView`)**:
  - **Sidebar**: Lists every agent from `opencode.json` / `opencode.jsonc` (`"agents"` or `"agent"`) and `agents/*.md` / `.opencode/agents/*.md`, showing each agent's current model, storage format badge (`md` / `jsonc`), search filtering, and unsaved indicator (`•`).
  - **YAML Editor (`GtkSourceView 5`)**: Syntax highlighting, automatic `Adwaita` / `Adwaita-dark` color scheme switching, and cursor-anchored autocomplete for `model:` lines backed by `opencode models`.
- **Safe Persistence**:
  - Fail-fast YAML and agent schema validation with exact line/column error banners.
  - Comment-preserving surgical JSONC updates (`opencode.jsonc`) and clean Markdown frontmatter serialization (`agents/*.md`).
  - Atomic writes via `tempfile` + `os.fsync` + `os.replace`.
- **Async CLI Integration (`Gio.Subprocess`)**:
  - Non-blocking `opencode models` refresh on startup and on demand.
  - Non-blocking `opencode reload` after saving or creating/deleting an agent.
- **Unsaved Changes Guard**: Prompts before discarding edits when switching agents, reloading from disk, or closing the window.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/MrTomRod/opencode-agent-gui/main/install.sh | bash
```

Or run locally from this directory:

```bash
./install.sh
```

This clones/updates `~/Projects/opencode-agent-gui`, ensures Fedora GTK4/Libadwaita/GtkSourceView5/PyYAML dependencies are present, links `~/.local/bin/opencode-agent-gui`, and writes `~/.local/share/applications/io.github.opencode.AgentGui.desktop`.

## Dependencies (Fedora)

```bash
sudo dnf install python3-gobject gtk4 libadwaita gtksourceview5 python3-pyyaml
```

## Usage

```bash
# Auto-detects project-local config (./opencode.json(c) or ./.opencode/agents/*.md),
# falling back to global ~/.config/opencode/
./app.py

# Or target a specific config file or directory:
./app.py --config /path/to/opencode.jsonc
./app.py --config /path/to/.opencode
```

### Shortcuts

- `Ctrl+Space` (on a `model:` line): Open/refresh model autocomplete popup
- `Ctrl+S`: Validate, save atomically, and run `opencode reload`
- `Ctrl+R` / `F5`: Refresh models from `opencode models`
- `Ctrl+N`: Add a new agent
- `Ctrl+Q`: Quit

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```
