"""Main GNOME/Libadwaita window for editing OpenCode agents and hot-reloading."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
from gi.repository import Adw, Gio, GLib, Gtk

from config_store import (
    AGENT_NAME_RE,
    AgentEntry,
    ConfigError,
    ConfigScope,
    YamlValidationError,
    agent_to_yaml,
    create_agent,
    delete_agent,
    load_scope,
    resolve_config_scope,
    save_agent_from_yaml,
)
from editor import YamlEditorView, filter_models
from opencode_cli import fetch_models_async, reload_opencode_async


def _shorten_path(path: Path) -> str:
    home = Path.home()
    try:
        rel = path.relative_to(home)
        return f"~/{rel}"
    except ValueError:
        return str(path)


class AddAgentDialog(Adw.Dialog):
    """Modal dialog to create a new OpenCode agent with live model search."""

    def __init__(
        self,
        parent: "MainWindow",
        scope: ConfigScope,
        models: list[str],
        on_created: callable,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._scope = scope
        self._models = models
        self._on_created = on_created

        self.set_title("Add Agent")
        self.set_content_width(480)
        self.set_content_height(520)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._on_create_clicked)
        header.pack_end(self._create_btn)

        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        self._error_label = Gtk.Label(xalign=0.0, wrap=True)
        self._error_label.add_css_class("error")
        self._error_label.set_visible(False)
        content.append(self._error_label)

        group = Adw.PreferencesGroup()

        self._name_row = Adw.EntryRow(title="Agent Name (e.g. worker, reviewer)")
        self._name_row.connect("changed", self._on_inputs_changed)
        group.add(self._name_row)

        self._desc_row = Adw.EntryRow(title="Description (optional)")
        group.add(self._desc_row)

        mode_model = Gtk.StringList.new(["subagent", "primary", "all"])
        self._mode_row = Adw.ComboRow(title="Mode", model=mode_model)
        self._mode_row.set_selected(0)
        group.add(self._mode_row)

        format_model = Gtk.StringList.new(
            [
                "Markdown (agents/<name>.md)",
                f"Config ({scope.json_path.name if scope.json_path else 'opencode.jsonc'})",
            ]
        )
        self._format_row = Adw.ComboRow(title="Storage Format", model=format_model)
        default_is_md = scope.agents_dir is not None and scope.agents_dir.is_dir()
        self._format_row.set_selected(0 if default_is_md else 1)
        group.add(self._format_row)

        self._model_row = Adw.EntryRow(title="Model (provider/model)")
        self._model_row.connect("changed", self._on_model_entry_changed)
        self._model_row.connect("entry-activated", self._on_create_clicked)
        group.add(self._model_row)

        content.append(group)

        suggestions_label = Gtk.Label(label="Available Models", xalign=0.0)
        suggestions_label.add_css_class("heading")
        content.append(suggestions_label)

        self._model_list = Gtk.ListBox()
        self._model_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._model_list.set_activate_on_single_click(True)
        self._model_list.add_css_class("boxed-list")
        self._model_list.connect("row-activated", self._on_model_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(150)
        scrolled.set_child(self._model_list)
        content.append(scrolled)

        toolbar_view.set_content(content)
        self.set_child(toolbar_view)

        self._refresh_model_suggestions()
        self._name_row.grab_focus()

    def _on_inputs_changed(self, *_args: object) -> None:
        self._error_label.set_visible(False)

    def _on_model_entry_changed(self, *_args: object) -> None:
        self._error_label.set_visible(False)
        self._refresh_model_suggestions()

    def _refresh_model_suggestions(self) -> None:
        while (child := self._model_list.get_first_child()) is not None:
            self._model_list.remove(child)

        query = self._model_row.get_text().strip()
        matches = filter_models(self._models, query, limit=40)
        if not matches:
            empty_row = Gtk.ListBoxRow(selectable=False, activatable=False)
            lbl = Gtk.Label(
                label="No matching models (type a provider/model ID manually)"
                if self._models
                else "No cached models loaded yet (type provider/model manually)",
                xalign=0.0,
            )
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            lbl.set_margin_start(12)
            lbl.set_margin_end(12)
            empty_row.set_child(lbl)
            self._model_list.append(empty_row)
            return

        for model_id, _indices in matches:
            row = Gtk.ListBoxRow()
            row._model_id = model_id  # type: ignore[attr-defined]
            lbl = Gtk.Label(label=model_id, xalign=0.0)
            lbl.add_css_class("monospace")
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            lbl.set_margin_start(12)
            lbl.set_margin_end(12)
            row.set_child(lbl)
            self._model_list.append(row)

    def _on_model_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        model_id = getattr(row, "_model_id", None)
        if model_id:
            self._model_row.set_text(model_id)
            self._model_row.set_position(-1)

    def _show_error(self, msg: str) -> None:
        self._error_label.set_text(msg)
        self._error_label.set_visible(True)

    def _on_create_clicked(self, *_args: object) -> None:
        name = self._name_row.get_text().strip()
        if not name or not AGENT_NAME_RE.match(name):
            self._show_error(
                "Agent name must start with a letter or digit and use only letters, digits, '.', '_', or '-'."
            )
            self._name_row.grab_focus()
            return

        model = self._model_row.get_text().strip()
        if not model or "/" not in model or any(ch.isspace() for ch in model):
            self._show_error("Model must be a non-empty 'provider/model' string.")
            self._model_row.grab_focus()
            return

        mode_item = self._mode_row.get_selected_item()
        mode = mode_item.get_string() if mode_item is not None else "subagent"
        origin = "markdown" if self._format_row.get_selected() == 0 else "jsonc"
        description = self._desc_row.get_text().strip()

        try:
            entry = create_agent(
                self._scope,
                name=name,
                model=model,
                origin=origin,
                description=description,
                mode=mode,
            )
        except ConfigError as exc:
            self._show_error(str(exc))
            return

        self.close()
        self._on_created(entry)


class MainWindow(Adw.ApplicationWindow):
    """Primary two-pane Libadwaita window for OpenCode agent configuration."""

    def __init__(self, application: Adw.Application, config_path: str | None = None) -> None:
        super().__init__(application=application)
        self.set_title("OpenCode Agent Config")
        self.set_default_size(1080, 720)
        self.set_size_request(680, 460)

        self._models: list[str] = []
        self._models_cancellable: Gio.Cancellable | None = None
        self._scope: ConfigScope | None = None
        self._selected_entry: AgentEntry | None = None
        self._row_map: dict[str, Adw.ActionRow] = {}
        self._suppress_row_selection = False
        self._allow_close = False

        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        self._split_view = Adw.NavigationSplitView()
        self._split_view.set_min_sidebar_width(250)
        self._split_view.set_max_sidebar_width(360)
        self._toast_overlay.set_child(self._split_view)

        self._build_sidebar()
        self._build_content()

        self.connect("close-request", self._on_close_request)

        self._load_initial_scope(config_path)
        self.refresh_models()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_sidebar(self) -> None:
        sidebar_toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        self._sidebar_title = Adw.WindowTitle(title="Agents", subtitle="Loading...")
        header.set_title_widget(self._sidebar_title)

        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Agent (Ctrl+N)")
        add_btn.connect("clicked", lambda *_: self.open_add_agent_dialog())
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append("Open Config File…", "win.open-file")
        menu.append("Open Config Folder…", "win.open-folder")
        menu.append("Reload from Disk", "win.reload-disk")
        menu.append("Refresh Models", "win.refresh-models")

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Config Menu")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)

        sidebar_toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self._search_entry = Gtk.SearchEntry(placeholder_text="Filter agents…")
        self._search_entry.set_margin_top(6)
        self._search_entry.set_margin_start(10)
        self._search_entry.set_margin_end(10)
        self._search_entry.connect("search-changed", lambda *_: self._apply_sidebar_filter())
        box.append(self._search_entry)

        self._agent_list = Gtk.ListBox()
        self._agent_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._agent_list.add_css_class("navigation-sidebar")
        self._agent_list.set_filter_func(self._filter_agent_row)
        self._agent_list.connect("row-selected", self._on_sidebar_row_selected)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(self._agent_list)
        box.append(scrolled)

        sidebar_toolbar.set_content(box)
        sidebar_page = Adw.NavigationPage.new(sidebar_toolbar, "Agents")
        self._split_view.set_sidebar(sidebar_page)

    def _build_content(self) -> None:
        content_toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        self._content_title = Adw.WindowTitle(title="No Agent Selected", subtitle="")
        header.set_title_widget(self._content_title)

        refresh_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._refresh_stack = Gtk.Stack()
        self._refresh_stack.add_named(Gtk.Image.new_from_icon_name("view-refresh-symbolic"), "icon")
        spinner = Gtk.Spinner()
        spinner.start()
        self._refresh_stack.add_named(spinner, "spinner")
        self._refresh_stack.set_visible_child_name("icon")
        refresh_box.append(self._refresh_stack)
        refresh_box.append(Gtk.Label(label="Models"))

        self._refresh_btn = Gtk.Button(tooltip_text="Refresh available models via `opencode models` (Ctrl+R)")
        self._refresh_btn.set_child(refresh_box)
        self._refresh_btn.connect("clicked", lambda *_: self.refresh_models())
        header.pack_start(self._refresh_btn)

        self._delete_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text="Delete Selected Agent",
        )
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", lambda *_: self._confirm_delete_agent())
        header.pack_end(self._delete_btn)

        self._save_btn = Gtk.Button(label="Save & Reload", tooltip_text="Validate, save atomically, and run `opencode reload` (Ctrl+S)")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", lambda *_: self.save_current_agent())
        header.pack_end(self._save_btn)

        content_toolbar.add_top_bar(header)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._banner = Adw.Banner()
        self._banner.set_button_label("Dismiss")
        self._banner.set_revealed(False)
        self._banner.connect("button-clicked", lambda *_: self._banner.set_revealed(False))
        main_box.append(self._banner)

        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)

        # Empty / status state
        empty_status = Adw.StatusPage(
            title="No Agent Selected",
            description="Select an agent from the sidebar or create a new one to edit its YAML configuration.",
            icon_name="document-edit-symbolic",
        )
        empty_add_btn = Gtk.Button(label="Add Agent")
        empty_add_btn.add_css_class("pill")
        empty_add_btn.add_css_class("suggested-action")
        empty_add_btn.set_halign(Gtk.Align.CENTER)
        empty_add_btn.connect("clicked", lambda *_: self.open_add_agent_dialog())
        empty_status.set_child(empty_add_btn)
        self._content_stack.add_named(empty_status, "empty")

        # Editor page
        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._editor = YamlEditorView()
        self._editor.connect("dirty-changed", self._on_editor_dirty_changed)
        self._editor.connect("cursor-moved", self._on_editor_cursor_moved)

        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.set_vexpand(True)
        editor_scroll.set_hexpand(True)
        editor_scroll.set_child(self._editor)
        editor_box.append(editor_scroll)

        # Status bar
        status_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        editor_box.append(status_sep)

        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status_bar.set_margin_top(6)
        status_bar.set_margin_bottom(6)
        status_bar.set_margin_start(12)
        status_bar.set_margin_end(12)

        self._cursor_label = Gtk.Label(label="Ln 1, Col 1", xalign=0.0)
        self._cursor_label.add_css_class("dim-label")
        self._cursor_label.add_css_class("caption")
        status_bar.append(self._cursor_label)

        self._hint_label = Gtk.Label(label="", xalign=0.0, hexpand=True)
        self._hint_label.add_css_class("caption")
        self._hint_label.add_css_class("dim-label")
        status_bar.append(self._hint_label)

        self._models_status_label = Gtk.Label(label="Loading models…", xalign=1.0)
        self._models_status_label.add_css_class("dim-label")
        self._models_status_label.add_css_class("caption")
        status_bar.append(self._models_status_label)

        editor_box.append(status_bar)
        self._content_stack.add_named(editor_box, "editor")
        self._content_stack.set_visible_child_name("empty")

        main_box.append(self._content_stack)
        content_toolbar.set_content(main_box)

        content_page = Adw.NavigationPage.new(content_toolbar, "Editor")
        self._split_view.set_content(content_page)

        # Register window actions
        self._register_actions()

    def _register_actions(self) -> None:
        actions = {
            "open-file": self._on_action_open_file,
            "open-folder": self._on_action_open_folder,
            "reload-disk": lambda *_: self._confirm_then_run(self.reload_scope_from_disk),
            "refresh-models": lambda *_: self.refresh_models(),
        }
        for name, cb in actions.items():
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

    # ------------------------------------------------------------------
    # Scope loading & sidebar management
    # ------------------------------------------------------------------

    def _load_initial_scope(self, config_path: str | None) -> None:
        try:
            self._scope = resolve_config_scope(config_arg=config_path)
        except ConfigError as exc:
            self._scope = ConfigScope(label="Unavailable", root_dir=Path.cwd())
            self._show_banner(str(exc))
        self._populate_sidebar(select_entry_id=None)

    def reload_scope_from_disk(self) -> None:
        if self._scope is None:
            return
        prev_id = self._selected_entry.entry_id if self._selected_entry else None
        try:
            load_scope(self._scope)
        except ConfigError as exc:
            self._show_banner(str(exc))
            return
        self._populate_sidebar(select_entry_id=prev_id)
        self.show_toast("Reloaded configuration from disk")

    def _populate_sidebar(self, select_entry_id: str | None) -> None:
        self._suppress_row_selection = True
        try:
            while (child := self._agent_list.get_first_child()) is not None:
                self._agent_list.remove(child)
            self._row_map.clear()

            if self._scope is None:
                self._sidebar_title.set_subtitle("No config")
                self._show_empty_editor()
                return

            self._sidebar_title.set_subtitle(self._scope.label)

            for entry in self._scope.agents:
                row = self._build_agent_row(entry)
                self._agent_list.append(row)
                self._row_map[entry.entry_id] = row

            target_row = None
            if select_entry_id and select_entry_id in self._row_map:
                target_row = self._row_map[select_entry_id]
            elif self._scope.agents:
                target_row = self._row_map.get(self._scope.agents[0].entry_id)

            if target_row is not None:
                self._agent_list.select_row(target_row)
                entry = target_row._agent_entry  # type: ignore[attr-defined]
                self._load_entry_into_editor(entry)
            else:
                self._show_empty_editor()
        finally:
            self._suppress_row_selection = False

    def _build_agent_row(self, entry: AgentEntry) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row._agent_entry = entry  # type: ignore[attr-defined]
        self._update_row_labels(row, entry, dirty=False)

        badge = Gtk.Label(label=entry.format_label)
        badge.add_css_class("dim-label")
        badge.add_css_class("caption")
        badge.set_valign(Gtk.Align.CENTER)
        row.add_suffix(badge)
        return row

    def _update_row_labels(self, row: Adw.ActionRow, entry: AgentEntry, dirty: bool) -> None:
        title = f"{entry.name} •" if dirty else entry.name
        subtitle = entry.model_display or "(no model)"
        row.set_title(GLib.markup_escape_text(title))
        row.set_subtitle(GLib.markup_escape_text(subtitle))

    def _filter_agent_row(self, row: Gtk.ListBoxRow) -> bool:
        query = self._search_entry.get_text().strip().lower()
        if not query:
            return True
        entry: AgentEntry | None = getattr(row, "_agent_entry", None)
        if entry is None:
            return True
        return query in entry.name.lower() or query in entry.model_display.lower()

    def _apply_sidebar_filter(self) -> None:
        self._agent_list.invalidate_filter()

    def _show_empty_editor(self) -> None:
        self._selected_entry = None
        self._content_title.set_title("No Agent Selected")
        self._content_title.set_subtitle(_shorten_path(self._scope.root_dir) if self._scope else "")
        self._save_btn.set_sensitive(False)
        self._delete_btn.set_sensitive(False)
        self._editor.load_text("")
        self._content_stack.set_visible_child_name("empty")

    def _load_entry_into_editor(self, entry: AgentEntry) -> None:
        self._selected_entry = entry
        self._banner.set_revealed(False)
        self._content_title.set_title(entry.name)
        self._content_title.set_subtitle(_shorten_path(entry.source_path))
        self._editor.load_text(agent_to_yaml(entry.data))
        self._save_btn.set_sensitive(True)
        self._delete_btn.set_sensitive(True)
        self._content_stack.set_visible_child_name("editor")
        self._split_view.set_show_content(True)

    # ------------------------------------------------------------------
    # Selection, dirty tracking & unsaved changes guard
    # ------------------------------------------------------------------

    def _on_sidebar_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._suppress_row_selection or row is None:
            return

        next_entry: AgentEntry | None = getattr(row, "_agent_entry", None)
        if next_entry is None:
            return
        if self._selected_entry is not None and next_entry.entry_id == self._selected_entry.entry_id:
            return

        if self._selected_entry is not None and self._editor.is_dirty():
            # Restore previous visual selection while confirming with user.
            prev_row = self._row_map.get(self._selected_entry.entry_id)
            self._suppress_row_selection = True
            try:
                self._agent_list.select_row(prev_row)
            finally:
                self._suppress_row_selection = False
            self._prompt_unsaved_changes(
                self._selected_entry.name,
                on_proceed=lambda: self._switch_to_entry(next_entry),
            )
            return

        self._load_entry_into_editor(next_entry)

    def _switch_to_entry(self, entry: AgentEntry) -> None:
        self._suppress_row_selection = True
        try:
            row = self._row_map.get(entry.entry_id)
            if row is not None:
                self._agent_list.select_row(row)
        finally:
            self._suppress_row_selection = False
        self._load_entry_into_editor(entry)

    def _on_editor_dirty_changed(self, _editor: YamlEditorView, dirty: bool) -> None:
        if self._selected_entry is None:
            return
        row = self._row_map.get(self._selected_entry.entry_id)
        if row is not None:
            self._update_row_labels(row, self._selected_entry, dirty=dirty)

    def _on_editor_cursor_moved(
        self,
        _editor: YamlEditorView,
        line: int,
        col: int,
        on_completable_line: bool,
    ) -> None:
        self._cursor_label.set_text(f"Ln {line}, Col {col}")
        if on_completable_line:
            self._hint_label.set_text("Type to filter or Ctrl+Space for suggestions")
        else:
            self._hint_label.set_text("")

    def _prompt_unsaved_changes(self, agent_name: str, on_proceed: callable) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Unsaved changes for '{agent_name}'",
            body="You have modified this agent's YAML without saving. Save and reload OpenCode before continuing?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save & Reload")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def _on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response == "discard":
                self._editor.mark_clean()
                if self._selected_entry is not None:
                    row = self._row_map.get(self._selected_entry.entry_id)
                    if row is not None:
                        self._update_row_labels(row, self._selected_entry, dirty=False)
                on_proceed()
            elif response == "save":
                if self.save_current_agent():
                    on_proceed()

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _confirm_then_run(self, action: callable) -> None:
        if self._selected_entry is not None and self._editor.is_dirty():
            self._prompt_unsaved_changes(self._selected_entry.name, on_proceed=action)
        else:
            action()

    def _on_close_request(self, *_args: object) -> bool:
        if self._allow_close:
            return False
        if self._selected_entry is not None and self._editor.is_dirty():
            def _finish_close() -> None:
                self._allow_close = True
                self.close()

            self._prompt_unsaved_changes(self._selected_entry.name, on_proceed=_finish_close)
            return True
        return False

    # ------------------------------------------------------------------
    # Save, reload, CLI integration, and CRUD actions
    # ------------------------------------------------------------------

    def save_current_agent(self) -> bool:
        """Validate YAML, write atomically, update sidebar, and trigger `opencode reload`."""
        if self._selected_entry is None:
            return False

        entry = self._selected_entry
        yaml_text = self._editor.get_text()
        try:
            save_agent_from_yaml(entry, yaml_text)
        except YamlValidationError as exc:
            self._show_banner(str(exc))
            if exc.line is not None:
                self._editor.jump_to_line(exc.line)
            return False
        except ConfigError as exc:
            self._show_banner(str(exc))
            return False

        self._banner.set_revealed(False)
        self._editor.mark_clean()

        row = self._row_map.get(entry.entry_id)
        if row is not None:
            self._update_row_labels(row, entry, dirty=False)

        model_label = entry.model_display
        self._save_btn.set_sensitive(False)

        def _on_reloaded(ok: bool, detail: str | None) -> None:
            self._save_btn.set_sensitive(True)
            if ok:
                self.show_toast(f"Saved {entry.name} → {model_label} and reloaded OpenCode")
            else:
                self.show_toast(f"Saved {entry.name} → {model_label} (reload warning)")
                self._show_banner(detail or "Saved file, but `opencode reload` failed.")

        reload_opencode_async(_on_reloaded)
        return True

    def refresh_models(self) -> None:
        """Asynchronously fetch available models from `opencode models`."""
        if self._models_cancellable is not None:
            self._models_cancellable.cancel()

        self._models_cancellable = Gio.Cancellable()
        self._refresh_stack.set_visible_child_name("spinner")
        self._refresh_btn.set_sensitive(False)
        self._models_status_label.set_text("Refreshing models…")

        def _on_models_done(models: list[str] | None, error: str | None) -> None:
            self._refresh_stack.set_visible_child_name("icon")
            self._refresh_btn.set_sensitive(True)
            if error is not None or models is None:
                self._models_status_label.set_text("Model refresh failed")
                self._show_banner(error or "Failed to load models from `opencode models`.")
                return

            self._models = models
            self._editor.set_models(models)
            self._models_status_label.set_text(f"{len(models)} models ready")

        fetch_models_async(_on_models_done, cancellable=self._models_cancellable)

    def open_add_agent_dialog(self) -> None:
        if self._scope is None:
            return

        def _show_dialog() -> None:
            dlg = AddAgentDialog(
                parent=self,
                scope=self._scope,
                models=self._models,
                on_created=self._on_agent_created,
            )
            dlg.present(self)

        self._confirm_then_run(_show_dialog)

    def _on_agent_created(self, entry: AgentEntry) -> None:
        self._populate_sidebar(select_entry_id=entry.entry_id)
        self.show_toast(f"Created agent '{entry.name}' → {entry.model_display}")
        reload_opencode_async(lambda ok, detail: None if ok else self._show_banner(detail or "Reload failed"))

    def _confirm_delete_agent(self) -> None:
        if self._scope is None or self._selected_entry is None:
            return
        entry = self._selected_entry
        dialog = Adw.AlertDialog(
            heading=f"Delete '{entry.name}'?",
            body=f"This will remove '{entry.name}' from {_shorten_path(entry.source_path)}.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_resp(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "delete" or self._scope is None:
                return
            try:
                delete_agent(self._scope, entry)
            except ConfigError as exc:
                self._show_banner(str(exc))
                return
            self._editor.mark_clean()
            self._populate_sidebar(select_entry_id=None)
            self.show_toast(f"Deleted agent '{entry.name}'")
            reload_opencode_async(lambda ok, detail: None if ok else self._show_banner(detail or "Reload failed"))

        dialog.connect("response", _on_resp)
        dialog.present(self)

    def _on_action_open_file(self, *_args: object) -> None:
        def _open() -> None:
            dialog = Gtk.FileDialog(title="Open OpenCode Config File")
            file_filter = Gtk.FileFilter()
            file_filter.set_name("OpenCode Config (*.json, *.jsonc, *.md)")
            file_filter.add_pattern("*.json")
            file_filter.add_pattern("*.jsonc")
            file_filter.add_pattern("*.md")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(file_filter)
            dialog.set_filters(filters)
            dialog.open(self, None, self._on_file_chosen)

        self._confirm_then_run(_open)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        if path:
            self._open_custom_path(path)

    def _on_action_open_folder(self, *_args: object) -> None:
        def _open() -> None:
            dialog = Gtk.FileDialog(title="Open OpenCode Config Directory")
            dialog.select_folder(self, None, self._on_folder_chosen)

        self._confirm_then_run(_open)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        if path:
            self._open_custom_path(path)

    def _open_custom_path(self, path: str) -> None:
        try:
            self._scope = resolve_config_scope(config_arg=path)
            self._banner.set_revealed(False)
            self._populate_sidebar(select_entry_id=None)
            self.show_toast(f"Loaded {self._scope.label}")
        except ConfigError as exc:
            self._show_banner(str(exc))

    def _show_banner(self, message: str) -> None:
        self._banner.set_title(message)
        self._banner.set_revealed(True)

    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)
