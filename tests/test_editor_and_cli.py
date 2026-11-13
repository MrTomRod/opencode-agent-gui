"""Unit and GTK integration tests for editor.py, opencode_cli.py, and window.py."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, GLib, Gtk

from editor import (
    YamlEditorView,
    extract_completion_context,
    extract_model_line_context,
    filter_models,
    format_model_markup,
    get_default_effort_for_yaml,
    get_effort_candidates_for_yaml,
)
from opencode_cli import fetch_models_async, parse_models_output, reload_opencode_async
from window import MainWindow


SAMPLE_MODELS = [
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-opus-4-7",
    "google/gemini-3.8-flash",
    "google/gemini-3.1-pro-preview",
    "openrouter/deepseek/deepseek-v4.1-flash-1017",
]


class TestCompletionMatching(unittest.TestCase):
    def test_extract_model_line_context(self) -> None:
        m = extract_model_line_context("model: goog", "model: goog")
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.prefix_end_col, 7)
        self.assertEqual(m.replace_end_col, 11)
        self.assertEqual(m.query, "goog")

        # Replacing an existing model mid-line
        line2 = "  model: anthropic/claude-sonnet-4-6"
        m2 = extract_model_line_context("  model: anth", line2)
        self.assertIsNotNone(m2)
        assert m2 is not None
        self.assertEqual(m2.prefix_end_col, 9)
        self.assertEqual(m2.replace_end_col, len(line2))
        self.assertEqual(m2.query, "anth")

        # Quoted value
        line3 = 'model: "google/gemini-3.8-flash"'
        m3 = extract_model_line_context('model: "goog', line3)
        self.assertIsNotNone(m3)
        assert m3 is not None
        self.assertEqual(m3.prefix_end_col, 8)
        self.assertEqual(m3.replace_end_col, len(line3) - 1)
        self.assertEqual(m3.query, "goog")

        # Non-model lines ignored
        self.assertIsNone(extract_model_line_context("description: model", "description: model"))
        self.assertIsNone(extract_model_line_context("model: # comment", "model: # comment"))

    def test_extract_effort_and_variant_context(self) -> None:
        for key in ("reasoningEffort", "effort", "variant", "  reasoning_effort"):
            line = f"{key}: med"
            ctx = extract_completion_context(line, line)
            self.assertIsNotNone(ctx)
            assert ctx is not None
            self.assertEqual(ctx.kind, "effort")
            self.assertEqual(ctx.query, "med")

        google_lvls = get_effort_candidates_for_yaml("model: google/gemini-4-hydrogen\n")
        self.assertEqual(google_lvls[:4], ["low", "medium", "high", "minimal"])

        anthropic_lvls = get_effort_candidates_for_yaml("model: anthropic/claude-opus-4-7\n")
        self.assertEqual(anthropic_lvls[:2], ["high", "max"])

        self.assertEqual(get_default_effort_for_yaml("model: google/gemini-4-hydrogen\n"), "high")
        self.assertEqual(get_default_effort_for_yaml("model: openrouter/openai/gpt-6.1-sol\n"), "medium")
        self.assertEqual(get_default_effort_for_yaml("model: claude-agent/opus\n"), "high")

        # Inline #effort suffix on model line
        inline_hash = "model: google/gemini-4-hydrogen#hi"
        ctx_hash = extract_completion_context(inline_hash, inline_hash)
        self.assertIsNotNone(ctx_hash)
        assert ctx_hash is not None
        self.assertEqual(ctx_hash.kind, "effort")
        self.assertEqual(ctx_hash.query, "hi")
        self.assertEqual(ctx_hash.prefix_end_col, len("model: google/gemini-4-hydrogen#"))

        # YAML key prefix completion
        ctx_key = extract_completion_context("reas", "reas")
        self.assertIsNotNone(ctx_key)
        assert ctx_key is not None
        self.assertEqual(ctx_key.kind, "key")
        self.assertEqual(ctx_key.query, "reas")

    def test_filter_models_ranking_and_markup(self) -> None:
        hits = filter_models(SAMPLE_MODELS, "flash")
        names = [h[0] for h in hits]
        self.assertIn("google/gemini-3.8-flash", names)
        self.assertIn("openrouter/deepseek/deepseek-v4.1-flash-1017", names)

        multi = filter_models(SAMPLE_MODELS, "google flash")
        self.assertEqual([m[0] for m in multi], ["google/gemini-3.8-flash"])

        markup = format_model_markup("google/gemini-3.8-flash", [0, 1, 2])
        self.assertIn("<b>goo</b>gle/gemini-3.8-flash", markup)


class TestOpenCodeCli(unittest.TestCase):
    def test_parse_models_output_dedupes_and_strips(self) -> None:
        parsed = parse_models_output("  google/a \n\nanthropic/b\ngoogle/a\n")
        self.assertEqual(parsed, ["google/a", "anthropic/b"])

    def test_async_fetch_models_and_reload_via_fake_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "opencode"
            fake_bin.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "models" ]; then\n'
                '  echo "google/gemini-3.8-flash"\n'
                '  echo "anthropic/claude-sonnet-4-6"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "reload" ]; then\n'
                '  echo "Reloaded"\n'
                "  exit 0\n"
                "fi\n"
                'echo "unknown cmd" >&2\n'
                "exit 2\n",
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}:{old_path}"
            try:
                loop = GLib.MainLoop()
                results: dict[str, object] = {}

                def _on_models(models: list[str] | None, err: str | None) -> None:
                    results["models"] = models
                    results["models_err"] = err
                    reload_opencode_async(_on_reload)

                def _on_reload(ok: bool, detail: str | None) -> None:
                    results["reload_ok"] = ok
                    results["reload_detail"] = detail
                    loop.quit()

                fetch_models_async(_on_models)
                GLib.timeout_add(3000, loop.quit)
                loop.run()

                self.assertEqual(
                    results.get("models"),
                    ["google/gemini-3.8-flash", "anthropic/claude-sonnet-4-6"],
                )
                self.assertIsNone(results.get("models_err"))
                self.assertTrue(results.get("reload_ok"))
                self.assertEqual(results.get("reload_detail"), "Reloaded")
            finally:
                os.environ["PATH"] = old_path


class TestGtkWidgets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Gtk.init()
        Adw.init()

    def test_yaml_editor_dirty_and_completion_apply(self) -> None:
        editor = YamlEditorView()
        editor.set_models(SAMPLE_MODELS)

        dirty_events: list[bool] = []
        editor.connect("dirty-changed", lambda _ed, val: dirty_events.append(val))

        editor.load_text("model: google/old-model\ndescription: Test\n")
        self.assertFalse(editor.is_dirty())

        line_match = extract_model_line_context("model: google/", "model: google/old-model")
        assert line_match is not None
        editor.apply_model_completion(0, line_match, "google/gemini-3.8-flash")
        self.assertTrue(editor.is_dirty())
        self.assertTrue(dirty_events[-1])
        self.assertIn("model: google/gemini-3.8-flash\n", editor.get_text())

    def test_main_window_loads_temp_scope_and_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "opencode.jsonc"
            cfg.write_text(
                '{\n  // comment preserved\n  "agents": {\n    "worker": { "model": "google/gemini-3.8-flash" }\n  }\n}\n',
                encoding="utf-8",
            )
            fake_bin = Path(tmp) / "opencode"
            fake_bin.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "models" ]; then\n'
                '  echo "google/gemini-3.8-flash"\n'
                '  echo "anthropic/claude-sonnet-4-6"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "reload" ]; then\n'
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}:{old_path}"
            try:
                from app import OpenCodeAgentApp

                app = OpenCodeAgentApp()
                captured: dict[str, object] = {}

                def _on_activate(_app: Adw.Application) -> None:
                    GLib.timeout_add(3000, app.quit)
                    win = app._window
                    assert win is not None
                    captured["agent"] = win._selected_entry.name if win._selected_entry else None
                    captured["editor_text"] = win._editor.get_text()
                    win._editor.get_buffer().set_text(
                        "model: anthropic/claude-sonnet-4-6\ndescription: Updated\n"
                    )
                    captured["dirty_before_save"] = win._editor.is_dirty()
                    captured["save_ok"] = win.save_current_agent()
                    captured["dirty_after_save"] = win._editor.is_dirty()

                    def _finish() -> bool:
                        captured["models"] = list(win._models)
                        win.close()
                        app.quit()
                        return False

                    GLib.timeout_add(250, _finish)

                app.connect_after("activate", _on_activate)
                app.run(["opencode-agent-gui", "--config", str(cfg)])

                self.assertEqual(captured.get("agent"), "worker")
                self.assertIn("model: google/gemini-3.8-flash", str(captured.get("editor_text")))
                self.assertTrue(captured.get("dirty_before_save"))
                self.assertTrue(captured.get("save_ok"))
                self.assertFalse(captured.get("dirty_after_save"))
                self.assertEqual(
                    captured.get("models"),
                    ["google/gemini-3.8-flash", "anthropic/claude-sonnet-4-6"],
                )
                saved_raw = cfg.read_text(encoding="utf-8")
                self.assertIn("// comment preserved", saved_raw)
                self.assertIn("anthropic/claude-sonnet-4-6", saved_raw)
            finally:
                os.environ["PATH"] = old_path


if __name__ == "__main__":
    unittest.main()
