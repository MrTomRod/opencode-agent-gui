"""Unit tests for config_store.py."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from config_store import (
    ConfigError,
    JsoncParseError,
    YamlValidationError,
    agent_to_yaml,
    create_agent,
    delete_agent,
    parse_agent_markdown,
    parse_jsonc,
    resolve_config_scope,
    save_agent_from_yaml,
    serialize_agent_markdown,
    strip_jsonc,
    update_jsonc_agent,
    yaml_to_agent_dict,
)


SAMPLE_JSONC = """{
  "$schema": "https://opencode.ai/config.json",
  // Keep this comment and URL string intact!
  "model": "claude-agent/opus",
  /* Multi-line
     block comment */
  "agents": {
    "title": {
      "model": "openrouter/deepseek/deepseek-v4.1-flash-1017", // inline comment
    },
    "worker": {
      "model": "google/gemini-3.8-flash",
      "description": "Worker agent"
    }
  },
  // Trailing section
  "plugins": [],
}
"""


class TestJsoncHandling(unittest.TestCase):
    def test_strip_and_parse_jsonc_preserves_strings_and_lines(self) -> None:
        parsed = parse_jsonc(SAMPLE_JSONC)
        self.assertEqual(parsed["$schema"], "https://opencode.ai/config.json")
        self.assertEqual(parsed["agents"]["worker"]["model"], "google/gemini-3.8-flash")

        stripped = strip_jsonc(SAMPLE_JSONC)
        self.assertEqual(len(stripped.splitlines()), len(SAMPLE_JSONC.splitlines()))

    def test_parse_jsonc_reports_line_and_column_on_error(self) -> None:
        bad = '{\n  // ok\n  "model": ,\n}\n'
        with self.assertRaises(JsoncParseError) as ctx:
            parse_jsonc(bad)
        self.assertEqual(ctx.exception.line, 3)

    def test_update_jsonc_agent_preserves_comments_and_unrelated_keys(self) -> None:
        updated = update_jsonc_agent(
            SAMPLE_JSONC,
            "agents",
            "worker",
            {"model": "anthropic/claude-sonnet-4-6", "description": "Updated worker"},
        )
        self.assertIn("// Keep this comment and URL string intact!", updated)
        self.assertIn("/* Multi-line", updated)
        self.assertIn("// Trailing section", updated)
        parsed = parse_jsonc(updated)
        self.assertEqual(parsed["agents"]["worker"]["model"], "anthropic/claude-sonnet-4-6")
        self.assertEqual(
            parsed["agents"]["title"]["model"],
            "openrouter/deepseek/deepseek-v4.1-flash-1017",
        )

    def test_insert_new_agent_and_create_section_if_missing(self) -> None:
        updated = update_jsonc_agent(
            SAMPLE_JSONC,
            "agents",
            "reviewer",
            {"model": "openrouter/openai/gpt-6.1-sol"},
        )
        parsed = parse_jsonc(updated)
        self.assertEqual(parsed["agents"]["reviewer"]["model"], "openrouter/openai/gpt-6.1-sol")
        self.assertIn("// Keep this comment and URL string intact!", updated)

        no_agents = '{\n  // header\n  "model": "claude-agent/opus"\n}\n'
        added_section = update_jsonc_agent(
            no_agents,
            "agents",
            "worker",
            {"model": "google/gemini-3.8-flash"},
        )
        self.assertIn("// header", added_section)
        self.assertEqual(parse_jsonc(added_section)["agents"]["worker"]["model"], "google/gemini-3.8-flash")

    def test_delete_jsonc_agent(self) -> None:
        deleted_first = update_jsonc_agent(SAMPLE_JSONC, "agents", "title", None)
        parsed1 = parse_jsonc(deleted_first)
        self.assertNotIn("title", parsed1["agents"])
        self.assertIn("worker", parsed1["agents"])

        deleted_last = update_jsonc_agent(deleted_first, "agents", "worker", None)
        parsed2 = parse_jsonc(deleted_last)
        self.assertEqual(parsed2["agents"], {})


class TestYamlAndMarkdown(unittest.TestCase):
    def test_yaml_roundtrip_and_literal_prompt(self) -> None:
        data = {
            "model": "claude-agent/sonnet",
            "description": "Handles UI QA",
            "mode": "subagent",
            "prompt": "Line one.\nLine two.",
        }
        yaml_text = agent_to_yaml(data)
        self.assertIn("prompt: |", yaml_text)
        parsed = yaml_to_agent_dict(yaml_text)
        self.assertEqual(parsed["model"], "claude-agent/sonnet")
        self.assertEqual(parsed["prompt"], "Line one.\nLine two.")

    def test_yaml_validation_errors(self) -> None:
        with self.assertRaises(YamlValidationError) as ctx1:
            yaml_to_agent_dict("model: [unclosed\n")
        self.assertIsNotNone(ctx1.exception.line)

        with self.assertRaises(YamlValidationError):
            yaml_to_agent_dict("description: missing model\n")

        with self.assertRaises(YamlValidationError):
            yaml_to_agent_dict("model: invalid-without-slash\n")

        with self.assertRaises(YamlValidationError):
            yaml_to_agent_dict("model: google/gemini pro\n")

        with self.assertRaises(YamlValidationError):
            yaml_to_agent_dict("model: google/gemini-3.8-flash\ntemperature: hot\n")

    def test_markdown_frontmatter_roundtrip(self) -> None:
        md = "---\ndescription: Fast worker\nmode: subagent\nmodel: google/gemini-3.8-flash\n---\n\nDo work.\n"
        parsed = parse_agent_markdown(md)
        self.assertEqual(parsed["model"], "google/gemini-3.8-flash")
        self.assertEqual(parsed["prompt"], "Do work.")
        serialized = serialize_agent_markdown(parsed)
        reparsed = parse_agent_markdown(serialized)
        self.assertEqual(reparsed, parsed)


class TestScopeAndPersistence(unittest.TestCase):
    def test_project_local_precedence_and_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_dir = root / "global_opencode"
            global_dir.mkdir()
            (global_dir / "opencode.jsonc").write_text(SAMPLE_JSONC, encoding="utf-8")

            proj_dir = root / "project"
            (proj_dir / ".opencode" / "agents").mkdir(parents=True)
            (proj_dir / ".opencode" / "agents" / "manager.md").write_text(
                "---\nmode: primary\nmodel: claude-agent/opus\n---\n\nLead.\n",
                encoding="utf-8",
            )
            (proj_dir / "opencode.jsonc").write_text(SAMPLE_JSONC, encoding="utf-8")

            scope = resolve_config_scope(cwd=proj_dir, global_dir=global_dir)
            names = [a.name for a in scope.agents]
            self.assertEqual(names, ["manager", "title", "worker"])

            # Save markdown agent via YAML
            manager = next(a for a in scope.agents if a.name == "manager")
            save_agent_from_yaml(
                manager,
                "model: google/gemini-4-hydrogen\nmode: primary\nprompt: |\n  Lead carefully.\n",
            )
            self.assertIn(
                "google/gemini-4-hydrogen",
                (proj_dir / ".opencode" / "agents" / "manager.md").read_text(encoding="utf-8"),
            )

            # Create new markdown agent and delete it
            created = create_agent(
                scope,
                name="qa",
                model="google/gemini-3.8-flash",
                origin="markdown",
                description="QA agent",
            )
            self.assertTrue((proj_dir / ".opencode" / "agents" / "qa.md").is_file())
            delete_agent(scope, created)
            self.assertFalse((proj_dir / ".opencode" / "agents" / "qa.md").exists())

            # Fallback to global when cwd has no project config
            empty_cwd = root / "empty_cwd"
            empty_cwd.mkdir()
            global_scope = resolve_config_scope(cwd=empty_cwd, global_dir=global_dir)
            self.assertEqual([a.name for a in global_scope.agents], ["title", "worker"])

            # Invalid agent name rejected
            with self.assertRaises(ConfigError):
                create_agent(scope, name="../escape", model="google/gemini-3.8-flash")


if __name__ == "__main__":
    unittest.main()
