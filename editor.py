"""YAML editor widget (GtkSourceView 5) with OpenCode model autocomplete."""

from __future__ import annotations

from dataclasses import dataclass
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, GtkSource


_MODEL_LINE_RE = re.compile(r"^(\s*(?:-\s*)?model\s*:\s*)(.*)$")
_EFFORT_LINE_RE = re.compile(r"^(\s*(?:-\s*)?(?:reasoningEffort|reasoning_effort|effort|variant)\s*:\s*)(.*)$")
_MODE_LINE_RE = re.compile(r"^(\s*(?:-\s*)?mode\s*:\s*)(.*)$")
_KEY_LINE_RE = re.compile(r"^(\s*(?:-\s*)?)([A-Za-z_][A-Za-z0-9_]*)$")

EFFORT_LEVELS_BY_PROVIDER: dict[str, list[str]] = {
    "google": ["low", "medium", "high", "minimal"],
    "anthropic": ["high", "max", "medium", "low"],
    "openai": ["none", "minimal", "low", "medium", "high", "xhigh"],
}
DEFAULT_EFFORT_LEVELS: list[str] = ["low", "medium", "high", "minimal", "none", "xhigh", "max"]
MODE_VALUES: list[str] = ["subagent", "primary", "all"]
YAML_KEY_SUGGESTIONS: list[str] = [
    "model: ",
    "reasoningEffort: ",
    "variant: ",
    "effort: ",
    "description: ",
    "mode: ",
    "temperature: ",
    "steps: ",
    "maxSteps: ",
    "tools: ",
    "permissions: ",
    "options: ",
    "prompt: ",
]


@dataclass(frozen=True)
class ModelLineMatch:
    """Parsed cursor context when editing a `model:` or effort/variant line in YAML."""

    prefix_end_col: int
    replace_end_col: int
    query: str
    quote_char: str = ""
    kind: str = "model"


def _parse_yaml_value_context(
    prefix: str,
    typed_val: str,
    line_before_cursor: str,
    full_line: str,
    kind: str,
) -> ModelLineMatch | None:
    # Do not trigger autocomplete inside an inline YAML comment.
    if not typed_val.startswith(('"', "'")):
        if typed_val.lstrip().startswith("#") or " #" in typed_val:
            return None

    # Support `model: provider/model#effort` inline variant/effort suffix.
    if kind == "model" and "#" in typed_val and not typed_val.startswith(('"', "'")):
        hash_idx = typed_val.rfind("#")
        prefix_end_col = len(prefix) + hash_idx + 1
        query = typed_val[hash_idx + 1:]
        tail = full_line[prefix_end_col:]
        comment_idx = tail.find(" #")
        if comment_idx != -1:
            replace_end_col = prefix_end_col + len(tail[:comment_idx].rstrip())
        else:
            replace_end_col = prefix_end_col + len(tail.rstrip())
        return ModelLineMatch(
            prefix_end_col=prefix_end_col,
            replace_end_col=max(replace_end_col, len(line_before_cursor)),
            query=query.strip(),
            quote_char="",
            kind="effort",
        )

    quote_char = ""
    if typed_val.startswith(('"', "'")):
        quote_char = typed_val[0]
        query = typed_val[1:]
        prefix_end_col = len(prefix) + 1
        rest_after_cursor = full_line[len(line_before_cursor):]
        close_pos = rest_after_cursor.find(quote_char)
        if close_pos != -1:
            replace_end_col = len(line_before_cursor) + close_pos
        else:
            replace_end_col = len(full_line.rstrip())
        return ModelLineMatch(
            prefix_end_col=prefix_end_col,
            replace_end_col=max(replace_end_col, len(line_before_cursor)),
            query=query,
            quote_char=quote_char,
            kind=kind,
        )

    prefix_end_col = len(prefix)
    tail = full_line[prefix_end_col:]
    comment_idx = tail.find(" #")
    if comment_idx != -1:
        tail_val = tail[:comment_idx].rstrip()
        replace_end_col = prefix_end_col + len(tail_val)
    else:
        replace_end_col = prefix_end_col + len(tail.rstrip())

    return ModelLineMatch(
        prefix_end_col=prefix_end_col,
        replace_end_col=max(replace_end_col, len(line_before_cursor)),
        query=typed_val.strip(),
        quote_char="",
        kind=kind,
    )


def extract_completion_context(line_before_cursor: str, full_line: str) -> ModelLineMatch | None:
    """Return ModelLineMatch if cursor is editing a `model:`, effort/variant, `mode:`, or key line."""
    model_match = _MODEL_LINE_RE.match(line_before_cursor)
    if model_match:
        return _parse_yaml_value_context(
            model_match.group(1),
            model_match.group(2),
            line_before_cursor,
            full_line,
            kind="model",
        )

    effort_match = _EFFORT_LINE_RE.match(line_before_cursor)
    if effort_match:
        return _parse_yaml_value_context(
            effort_match.group(1),
            effort_match.group(2),
            line_before_cursor,
            full_line,
            kind="effort",
        )

    mode_match = _MODE_LINE_RE.match(line_before_cursor)
    if mode_match:
        return _parse_yaml_value_context(
            mode_match.group(1),
            mode_match.group(2),
            line_before_cursor,
            full_line,
            kind="mode",
        )

    # Offer YAML key suggestions when typing a key name on a line without ':' yet.
    if ":" not in full_line:
        key_match = _KEY_LINE_RE.match(line_before_cursor)
        if key_match:
            prefix, key_query = key_match.group(1), key_match.group(2)
            return ModelLineMatch(
                prefix_end_col=len(prefix),
                replace_end_col=max(len(full_line.rstrip()), len(line_before_cursor)),
                query=key_query,
                quote_char="",
                kind="key",
            )
    return None


def extract_model_line_context(line_before_cursor: str, full_line: str) -> ModelLineMatch | None:
    """Return ModelLineMatch if the cursor is editing a YAML `model:` value, else None."""
    ctx = extract_completion_context(line_before_cursor, full_line)
    if ctx is not None and ctx.kind == "model":
        return ctx
    return None


def _detect_vendor_from_model(model_val: str) -> str:
    lower = model_val.strip().strip("\"'").lower()
    parts = [p for p in lower.split("/") if p]
    if not parts:
        return ""
    if "google" in parts or "gemini" in lower:
        return "google"
    if "anthropic" in parts or "claude-agent" in parts or "claude" in lower:
        return "anthropic"
    if "openai" in parts or any(p.startswith(("gpt-", "o1", "o3", "o4")) for p in parts):
        return "openai"
    return parts[0]


DEFAULT_EFFORT_BY_VENDOR: dict[str, str] = {
    "anthropic": "high",
    "openai": "medium",
    "google": "high",
}


def get_default_effort_for_yaml(yaml_text: str) -> str | None:
    """Return the default reasoning effort level for the buffer's `model:`, if known."""
    for line in yaml_text.splitlines():
        m = _MODEL_LINE_RE.match(line)
        if m:
            vendor = _detect_vendor_from_model(m.group(2))
            return DEFAULT_EFFORT_BY_VENDOR.get(vendor)
    return None


def get_effort_candidates_for_yaml(yaml_text: str) -> list[str]:
    """Return ordered effort/variant levels, prioritizing levels for the buffer's `model:`."""
    for line in yaml_text.splitlines():
        m = _MODEL_LINE_RE.match(line)
        if m:
            vendor = _detect_vendor_from_model(m.group(2))
            preferred = EFFORT_LEVELS_BY_PROVIDER.get(vendor)
            if preferred:
                rest = [lvl for lvl in DEFAULT_EFFORT_LEVELS if lvl not in preferred]
                return [*preferred, *rest]
            break
    return list(DEFAULT_EFFORT_LEVELS)


def _fuzzy_subsequence_match(candidate_lower: str, query_lower: str) -> tuple[int, list[int]] | None:
    """Match characters of query_lower in order inside candidate_lower, returning (score, indices)."""
    indices: list[int] = []
    pos = 0
    gaps = 0
    prev_idx = -1
    for ch in query_lower:
        found = candidate_lower.find(ch, pos)
        if found == -1:
            return None
        if prev_idx != -1:
            gaps += found - prev_idx - 1
        indices.append(found)
        prev_idx = found
        pos = found + 1
    return (300 + gaps, indices)


def filter_models(models: list[str], query: str, limit: int = 80) -> list[tuple[str, list[int]]]:
    """Filter and rank model IDs against a user query, returning (model_id, matched_indices)."""
    clean_query = query.strip().lower()
    if not clean_query:
        return [(m, []) for m in models[:limit]]

    tokens = [tok for tok in clean_query.split() if tok]
    scored: list[tuple[int, str, list[int]]] = []

    if len(tokens) == 1:
        q = tokens[0]
        for model in models:
            ml = model.lower()
            if ml.startswith(q):
                scored.append((0, model, list(range(len(q)))))
                continue

            slash_idx = ml.find("/")
            if slash_idx != -1 and ml[slash_idx + 1:].startswith(q):
                start = slash_idx + 1
                scored.append((50, model, list(range(start, start + len(q)))))
                continue

            sub_idx = ml.find(q)
            if sub_idx != -1:
                scored.append((100 + sub_idx, model, list(range(sub_idx, sub_idx + len(q)))))
                continue

            fuzzy = _fuzzy_subsequence_match(ml, q)
            if fuzzy is not None:
                score, indices = fuzzy
                scored.append((score, model, indices))
    else:
        for model in models:
            ml = model.lower()
            matched_indices: set[int] = set()
            total_pos = 0
            all_found = True
            for tok in tokens:
                idx = ml.find(tok)
                if idx == -1:
                    all_found = False
                    break
                total_pos += idx
                matched_indices.update(range(idx, idx + len(tok)))
            if all_found:
                scored.append((100 + total_pos, model, sorted(matched_indices)))

    scored.sort(key=lambda item: (item[0], len(item[1]), item[1].lower()))
    return [(model, indices) for _, model, indices in scored[:limit]]


def format_model_markup(model_id: str, matched_indices: list[int]) -> str:
    """Format a model ID with Pango markup highlighting matched characters."""
    matched = set(matched_indices)
    parts: list[str] = []
    i = 0
    n = len(model_id)
    while i < n:
        is_hit = i in matched
        j = i + 1
        while j < n and (j in matched) == is_hit:
            j += 1
        chunk = GLib.markup_escape_text(model_id[i:j])
        if is_hit:
            parts.append(f"<b>{chunk}</b>")
        else:
            parts.append(chunk)
        i = j
    return "".join(parts)


class ModelCompletionPopover(Gtk.Popover):
    """Non-focus-stealing completion popup anchored to the cursor on `model:` lines."""

    def __init__(self, editor: "YamlEditorView") -> None:
        super().__init__()
        self._editor = editor
        self._current_match: ModelLineMatch | None = None
        self._current_line: int = -1

        self.set_parent(editor)
        self.set_autohide(False)
        self.set_has_arrow(False)
        self.set_can_focus(False)
        self.set_position(Gtk.PositionType.BOTTOM)
        self.add_css_class("menu")

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        container.set_margin_top(6)
        container.set_margin_bottom(6)
        container.set_margin_start(6)
        container.set_margin_end(6)

        self._header_label = Gtk.Label(xalign=0.0)
        self._header_label.add_css_class("dim-label")
        self._header_label.add_css_class("caption")
        self._header_label.set_margin_start(6)
        self._header_label.set_margin_end(6)
        container.append(self._header_label)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.set_activate_on_single_click(True)
        self._listbox.set_can_focus(False)
        self._listbox.add_css_class("navigation-sidebar")
        self._listbox.connect("row-activated", self._on_row_activated)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scrolled.set_propagate_natural_height(True)
        self._scrolled.set_max_content_height(260)
        self._scrolled.set_min_content_width(420)
        self._scrolled.set_can_focus(False)
        self._scrolled.set_child(self._listbox)
        container.append(self._scrolled)

        self.set_child(container)

    def show_matches(
        self,
        matches: list[tuple[str, list[int]]],
        total_models: int,
        line_num: int,
        line_match: ModelLineMatch,
        pointing_rect: Gdk.Rectangle,
        default_value: str | None = None,
    ) -> None:
        """Populate and display completion matches at the cursor rectangle."""
        self._current_line = line_num
        self._current_match = line_match

        while (child := self._listbox.get_first_child()) is not None:
            self._listbox.remove(child)

        if not matches:
            self.popdown()
            return

        for model_id, indices in matches:
            row = Gtk.ListBoxRow()
            row._model_id = model_id  # type: ignore[attr-defined]
            row.set_can_focus(False)

            markup = format_model_markup(model_id, indices)
            if default_value is not None and model_id == default_value:
                markup += ' <span alpha="60%">[default]</span>'

            label = Gtk.Label(xalign=0.0)
            label.set_use_markup(True)
            label.set_markup(markup)
            label.add_css_class("monospace")
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            label.set_margin_start(6)
            label.set_margin_end(6)
            row.set_child(label)
            self._listbox.append(row)

        noun_map = {"effort": "level", "mode": "mode", "key": "field"}
        noun = noun_map.get(line_match.kind, "model")
        default_hint = f" [default: {default_value}]" if default_value else ""
        if len(matches) == 1:
            self._header_label.set_text(f"1 matching {noun}{default_hint} (Enter/Tab to insert)")
        else:
            self._header_label.set_text(
                f"{len(matches)} of {total_models} {noun}s{default_hint} (↑/↓ select, Enter/Tab insert)"
            )

        first = self._listbox.get_row_at_index(0)
        if first is not None:
            self._listbox.select_row(first)
            self._scrolled.get_vadjustment().set_value(0)

        self.set_pointing_to(pointing_rect)
        if not self.get_visible():
            self.popup()

    def move_selection(self, delta: int) -> bool:
        """Move highlighted row by delta (+1/-1/+5/-5) without stealing focus."""
        selected = self._listbox.get_selected_row()
        count = 0
        while self._listbox.get_row_at_index(count) is not None:
            count += 1
        if count == 0:
            return False

        cur_idx = selected.get_index() if selected is not None else 0
        new_idx = max(0, min(count - 1, cur_idx + delta))
        target = self._listbox.get_row_at_index(new_idx)
        if target is None:
            return False

        self._listbox.select_row(target)
        self._ensure_row_visible(target)
        return True

    def _ensure_row_visible(self, row: Gtk.ListBoxRow) -> None:
        vadj = self._scrolled.get_vadjustment()
        ok, rect = row.compute_bounds(self._listbox)
        if not ok:
            return
        y = rect.origin.y
        h = rect.size.height
        val = vadj.get_value()
        page = vadj.get_page_size()
        if y < val:
            vadj.set_value(y)
        elif y + h > val + page:
            vadj.set_value(y + h - page)

    def activate_selected(self) -> bool:
        """Insert the currently selected model into the editor buffer."""
        row = self._listbox.get_selected_row()
        if row is None:
            return False
        model_id = getattr(row, "_model_id", None)
        if not model_id or self._current_match is None:
            return False
        self._editor.apply_model_completion(self._current_line, self._current_match, model_id)
        self.popdown()
        return True

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        model_id = getattr(row, "_model_id", None)
        if not model_id or self._current_match is None:
            return
        self._editor.apply_model_completion(self._current_line, self._current_match, model_id)
        self.popdown()
        self._editor.grab_focus()


class YamlEditorView(GtkSource.View):
    """GtkSourceView 5 YAML editor with automatic Adwaita theming and model completion."""

    __gsignals__ = {
        "dirty-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "cursor-moved": (GObject.SignalFlags.RUN_FIRST, None, (int, int, bool)),
    }

    def __init__(self) -> None:
        buffer = GtkSource.Buffer()
        super().__init__(buffer=buffer)

        self._models: list[str] = []
        self._dirty = False
        self._loading = False
        self._applying_completion = False
        self._completion_suppressed = False

        lang_mgr = GtkSource.LanguageManager.get_default()
        yaml_lang = lang_mgr.get_language("yaml")
        if yaml_lang is not None:
            buffer.set_language(yaml_lang)
        buffer.set_highlight_syntax(True)
        buffer.set_highlight_matching_brackets(True)

        self.set_show_line_numbers(True)
        self.set_highlight_current_line(True)
        self.set_tab_width(2)
        self.set_indent_width(2)
        self.set_insert_spaces_instead_of_tabs(True)
        self.set_auto_indent(True)
        self.set_monospace(True)
        self.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.set_top_margin(12)
        self.set_bottom_margin(12)
        self.set_left_margin(8)
        self.set_right_margin(12)

        self._style_manager = Adw.StyleManager.get_default()
        self._style_manager.connect("notify::dark", self._on_dark_changed)
        self._apply_color_scheme()

        self._popover = ModelCompletionPopover(self)

        buffer.connect("changed", self._on_buffer_changed)
        buffer.connect("notify::cursor-position", self._on_cursor_moved)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect("leave", self._on_focus_leave)
        self.add_controller(focus_ctrl)

    def _apply_color_scheme(self) -> None:
        scheme_mgr = GtkSource.StyleSchemeManager.get_default()
        preferred = ("Adwaita-dark", "classic-dark") if self._style_manager.get_dark() else ("Adwaita", "classic")
        for scheme_id in preferred:
            scheme = scheme_mgr.get_scheme(scheme_id)
            if scheme is not None:
                self.get_buffer().set_style_scheme(scheme)
                return

    def _on_dark_changed(self, *_args: object) -> None:
        self._apply_color_scheme()

    def set_models(self, models: list[str]) -> None:
        """Update cached available model list and refresh popup if currently open."""
        self._models = list(models)
        if self._popover.get_visible():
            self._update_completion(force=True)

    def load_text(self, text: str) -> None:
        """Replace editor buffer contents without marking the editor dirty."""
        self._loading = True
        self._popover.popdown()
        self._completion_suppressed = False
        buf = self.get_buffer()
        buf.begin_irreversible_action()
        try:
            buf.set_text(text)
            buf.place_cursor(buf.get_start_iter())
        finally:
            buf.end_irreversible_action()
            self._loading = False
        self._set_dirty(False)
        self._emit_cursor_status()

    def get_text(self) -> str:
        buf = self.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def is_dirty(self) -> bool:
        return self._dirty

    def mark_clean(self) -> None:
        self._set_dirty(False)

    def jump_to_line(self, line_one_based: int) -> None:
        """Move cursor and scroll viewport to a 1-based line number."""
        buf = self.get_buffer()
        line_idx = max(0, line_one_based - 1)
        ok, target_iter = buf.get_iter_at_line(line_idx)
        if not ok:
            target_iter = buf.get_end_iter()
        buf.place_cursor(target_iter)
        self.scroll_to_iter(target_iter, 0.2, False, 0.0, 0.3)
        self.grab_focus()

    def _set_dirty(self, dirty: bool) -> None:
        if self._dirty != dirty:
            self._dirty = dirty
            self.emit("dirty-changed", dirty)

    def _get_current_line_state(self) -> tuple[int, int, str, str]:
        buf = self.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        line_num = cursor.get_line()
        col_num = cursor.get_line_offset()

        line_start = cursor.copy()
        line_start.set_line_offset(0)
        line_end = cursor.copy()
        if not line_end.ends_line():
            line_end.forward_to_line_end()

        line_before = buf.get_text(line_start, cursor, False)
        full_line = buf.get_text(line_start, line_end, False)
        return line_num, col_num, line_before, full_line

    def _emit_cursor_status(self) -> None:
        line_num, col_num, line_before, full_line = self._get_current_line_state()
        on_completable = extract_completion_context(line_before, full_line) is not None
        self.emit("cursor-moved", line_num + 1, col_num + 1, on_completable)

    def _on_buffer_changed(self, _buf: Gtk.TextBuffer) -> None:
        if self._loading:
            return
        self._set_dirty(True)
        if not self._applying_completion:
            self._completion_suppressed = False
            self._update_completion(force=False)

    def _on_cursor_moved(self, _buf: Gtk.TextBuffer, _pspec: object) -> None:
        self._emit_cursor_status()
        if self._loading or self._applying_completion:
            return
        if self._popover.get_visible():
            self._update_completion(force=False)

    def _on_focus_leave(self, _ctrl: Gtk.EventControllerFocus) -> None:
        # Defer popdown slightly so clicks inside the popover can register cleanly.
        GLib.idle_add(self._hide_if_unfocused)

    def _hide_if_unfocused(self) -> bool:
        if not self.has_focus():
            self._popover.popdown()
        return False

    def _update_completion(self, force: bool) -> None:
        if not self.get_editable():
            self._popover.popdown()
            return
        if self._completion_suppressed and not force:
            self._popover.popdown()
            return

        line_num, _col_num, line_before, full_line = self._get_current_line_state()
        line_match = extract_completion_context(line_before, full_line)
        if line_match is None:
            self._popover.popdown()
            return

        default_value: str | None = None
        if line_match.kind == "effort":
            yaml_text = self.get_text()
            candidates = get_effort_candidates_for_yaml(yaml_text)
            default_value = get_default_effort_for_yaml(yaml_text)
        elif line_match.kind == "mode":
            candidates = list(MODE_VALUES)
        elif line_match.kind == "key":
            candidates = list(YAML_KEY_SUGGESTIONS)
        else:
            if not self._models:
                self._popover.popdown()
                return
            candidates = self._models

        matches = filter_models(candidates, line_match.query)
        if not matches:
            self._popover.popdown()
            return

        # Don't stay open if the exact full value is already typed and it's the sole exact match (unless forced).
        if (
            not force
            and len(matches) == 1
            and matches[0][0] == line_match.query
        ):
            self._popover.popdown()
            return

        buf = self.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        loc = self.get_iter_location(cursor)
        wx, wy = self.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, loc.x, loc.y)
        rect = Gdk.Rectangle()
        rect.x = wx
        rect.y = wy
        rect.width = max(1, loc.width)
        rect.height = max(16, loc.height)

        self._popover.show_matches(
            matches,
            len(candidates),
            line_num,
            line_match,
            rect,
            default_value=default_value,
        )

    def apply_model_completion(self, line_num: int, line_match: ModelLineMatch, model_id: str) -> None:
        """Replace the model value on `line_num` with `model_id` cleanly."""
        buf = self.get_buffer()
        ok_start, start_iter = buf.get_iter_at_line_offset(line_num, line_match.prefix_end_col)
        ok_end, end_iter = buf.get_iter_at_line_offset(line_num, line_match.replace_end_col)
        if not ok_start or not ok_end:
            return

        self._applying_completion = True
        try:
            buf.begin_user_action()
            buf.delete(start_iter, end_iter)
            buf.insert(start_iter, model_id)
            buf.end_user_action()
        finally:
            self._applying_completion = False

        if line_match.kind == "key" and model_id in (
            "model: ",
            "reasoningEffort: ",
            "variant: ",
            "effort: ",
            "mode: ",
        ):
            self._completion_suppressed = False
            GLib.idle_add(lambda: (self._update_completion(force=True), False)[1])
        else:
            self._completion_suppressed = True

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        ctrl_pressed = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl_pressed and keyval == Gdk.KEY_space:
            self._completion_suppressed = False
            self._update_completion(force=True)
            return True

        if not self._popover.get_visible():
            return False

        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            return self._popover.move_selection(1)
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            return self._popover.move_selection(-1)
        if keyval in (Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down):
            return self._popover.move_selection(6)
        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
            return self._popover.move_selection(-6)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            return self._popover.activate_selected()
        if keyval == Gdk.KEY_Escape:
            self._completion_suppressed = True
            self._popover.popdown()
            return True
        return False
