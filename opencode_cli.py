"""Asynchronous OpenCode CLI wrappers built on Gio.Subprocess."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


def find_opencode_executable(override: str | None = None) -> str | None:
    """Locate the opencode binary on PATH or standard user install locations."""
    if override:
        return override
    on_path = shutil.which("opencode")
    if on_path:
        return on_path
    for candidate in (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".local" / "bin" / "opencode",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def parse_models_output(stdout: str) -> list[str]:
    """Parse `opencode models` stdout into an ordered, deduplicated list of model IDs."""
    seen: set[str] = set()
    models: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        models.append(line)
    return models


def _run_opencode_async(
    args: list[str],
    callback: Callable[[bool, str, str], None],
    cancellable: Gio.Cancellable | None = None,
    executable: str | None = None,
) -> Gio.Subprocess | None:
    exe = find_opencode_executable(executable)
    if not exe:
        GLib.idle_add(
            lambda: (callback(False, "", "Could not find 'opencode' executable on PATH or in ~/.opencode/bin."), False)[1]
        )
        return None

    flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
    try:
        proc = Gio.Subprocess.new([exe, *args], flags)
    except GLib.Error as exc:
        msg = exc.message
        GLib.idle_add(lambda: (callback(False, "", f"Failed to launch opencode: {msg}"), False)[1])
        return None

    def _on_communicate_done(source_proc: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            _ok, stdout, stderr = source_proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            callback(False, "", exc.message)
            return

        stdout_str = stdout or ""
        stderr_str = stderr or ""
        if source_proc.get_successful():
            callback(True, stdout_str, stderr_str)
        else:
            code = source_proc.get_exit_status()
            detail = stderr_str.strip() or stdout_str.strip() or f"Exited with status {code}"
            callback(False, stdout_str, detail)

    proc.communicate_utf8_async(None, cancellable, _on_communicate_done)
    return proc


def fetch_models_async(
    callback: Callable[[list[str] | None, str | None], None],
    cancellable: Gio.Cancellable | None = None,
    executable: str | None = None,
) -> Gio.Subprocess | None:
    """Run `opencode models` asynchronously and deliver (models, error) on the main loop."""

    def _on_finished(ok: bool, stdout: str, detail: str) -> None:
        if not ok:
            callback(None, f"`opencode models` failed: {detail}")
            return
        models = parse_models_output(stdout)
        callback(models, None)

    return _run_opencode_async(["models"], _on_finished, cancellable=cancellable, executable=executable)


def reload_opencode_async(
    callback: Callable[[bool, str | None], None],
    cancellable: Gio.Cancellable | None = None,
    executable: str | None = None,
) -> Gio.Subprocess | None:
    """Run `opencode reload` asynchronously and deliver (ok, detail) on the main loop."""

    def _on_finished(ok: bool, stdout: str, detail: str) -> None:
        if not ok:
            callback(False, f"`opencode reload` failed: {detail}")
            return
        callback(True, stdout.strip() or None)

    return _run_opencode_async(["reload"], _on_finished, cancellable=cancellable, executable=executable)
