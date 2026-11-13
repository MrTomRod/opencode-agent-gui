#!/usr/bin/env python3
"""Adw.Application entrypoint for OpenCode Agent Config GUI."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
from gi.repository import Adw, Gio, GLib

from window import MainWindow


APP_ID = "io.github.opencode.AgentGui"


class OpenCodeAgentApp(Adw.Application):
    """Libadwaita application managing OpenCode agent configurations."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._config_path: str | None = None
        self._window: MainWindow | None = None

        self.add_main_option(
            "config",
            ord("c"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING,
            "Path to opencode.json(c), agent .md file, or config directory",
            "PATH",
        )

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        save_act = Gio.SimpleAction.new("save", None)
        save_act.connect("activate", self._on_save)
        self.add_action(save_act)
        self.set_accels_for_action("app.save", ["<Control>s"])

        refresh_act = Gio.SimpleAction.new("refresh-models", None)
        refresh_act.connect("activate", self._on_refresh_models)
        self.add_action(refresh_act)
        self.set_accels_for_action("app.refresh-models", ["<Control>r", "F5"])

        new_act = Gio.SimpleAction.new("new-agent", None)
        new_act.connect("activate", self._on_new_agent)
        self.add_action(new_act)
        self.set_accels_for_action("app.new-agent", ["<Control>n"])

        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", self._on_quit)
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict()
        variant = options.lookup_value("config", GLib.VariantType.new("s"))
        if variant is not None:
            self._config_path = variant.get_string()
        else:
            args = command_line.get_arguments()
            if len(args) > 1 and not args[1].startswith("-"):
                self._config_path = args[1]

        self.activate()
        return 0

    def do_activate(self) -> None:
        if self._window is None:
            self._window = MainWindow(application=self, config_path=self._config_path)
        self._window.present()

    def _on_save(self, *_args: object) -> None:
        if self._window is not None:
            self._window.save_current_agent()

    def _on_refresh_models(self, *_args: object) -> None:
        if self._window is not None:
            self._window.refresh_models()

    def _on_new_agent(self, *_args: object) -> None:
        if self._window is not None:
            self._window.open_add_agent_dialog()

    def _on_quit(self, *_args: object) -> None:
        if self._window is not None:
            self._window.close()
        else:
            self.quit()


def main(argv: list[str] | None = None) -> int:
    app = OpenCodeAgentApp()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
