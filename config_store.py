"""Configuration loading, validation, and atomic persistence for OpenCode v2 agents."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import yaml


AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PREFERRED_KEY_ORDER = (
    "model",
    "reasoningEffort",
    "effort",
    "variant",
    "description",
    "mode",
    "temperature",
    "steps",
    "maxSteps",
    "tools",
    "permissions",
)


class ConfigError(Exception):
    """Base error for OpenCode configuration loading, validation, and saving."""


class JsoncParseError(ConfigError):
    """Raised when a JSON or JSONC file contains syntax errors."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        self.line = line
        self.column = column
        location = f" at line {line}, column {column}" if line is not None and column is not None else ""
        super().__init__(f"JSONC syntax error{location}: {message}")


class YamlValidationError(ConfigError):
    """Raised when YAML syntax or agent schema validation fails."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        self.line = line
        self.column = column
        location = f" (line {line}" + (f", col {column})" if column is not None else ")") if line is not None else ""
        super().__init__(f"{message}{location}")


@dataclass
class AgentEntry:
    """Represents a single agent configuration loaded from JSONC or Markdown."""

    name: str
    data: dict[str, Any]
    origin: str  # "jsonc" or "markdown"
    source_path: Path
    json_key: str = "agents"

    @property
    def entry_id(self) -> str:
        return f"{self.origin}:{self.source_path}:{self.name}"

    @property
    def model_display(self) -> str:
        """Return a human-readable model string for sidebar display."""
        model = self.data.get("model")
        if isinstance(model, str):
            return model.strip()
        if isinstance(model, dict):
            provider = str(model.get("providerID", "")).strip()
            model_id = str(model.get("model", "")).strip()
            if provider and model_id:
                return f"{provider}/{model_id}"
            return model_id or provider
        return ""

    @property
    def format_label(self) -> str:
        if self.origin == "markdown":
            return "md"
        return self.source_path.suffix.lstrip(".") or "json"


@dataclass
class ConfigScope:
    """Represents the active OpenCode configuration root and all discovered agents."""

    label: str
    root_dir: Path
    json_path: Path | None = None
    agents_dir: Path | None = None
    agents_json_key: str = "agents"
    agents: list[AgentEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

class _LiteralSafeDumper(yaml.SafeDumper):
    """SafeDumper that formats multiline strings with YAML literal block style."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralSafeDumper.add_representer(str, _represent_str)


def _order_agent_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of agent dict with consistent, readable key order."""
    ordered: dict[str, Any] = {}
    for key in PREFERRED_KEY_ORDER:
        if key in data:
            ordered[key] = data[key]
    for key, val in data.items():
        if key not in ordered and key != "prompt":
            ordered[key] = val
    if "prompt" in data:
        ordered["prompt"] = data["prompt"]
    return ordered


def agent_to_yaml(data: dict[str, Any]) -> str:
    """Serialize an agent dictionary to clean, human-editable YAML."""
    ordered = _order_agent_dict(data)
    if not ordered:
        return "{}\n"
    return yaml.dump(
        ordered,
        Dumper=_LiteralSafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


def _find_yaml_key_line(yaml_text: str, key: str) -> int | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for idx, line in enumerate(yaml_text.splitlines(), start=1):
        if pattern.match(line):
            return idx
    return None


def _is_valid_model_string(value: str) -> bool:
    if not value or any(ch.isspace() for ch in value) or "/" not in value:
        return False
    provider, model_id = value.split("/", 1)
    return bool(provider.strip()) and bool(model_id.strip())


def yaml_to_agent_dict(yaml_text: str) -> dict[str, Any]:
    """Parse and validate YAML text into a normalized agent dictionary.

    Raises YamlValidationError with exact line/column on syntax or schema errors.
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        col = (mark.column + 1) if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc)
        raise YamlValidationError(f"YAML syntax error: {problem}", line=line, column=col) from exc

    if not isinstance(parsed, dict):
        raise YamlValidationError("Agent configuration must be a YAML mapping (key: value pairs).", line=1, column=1)

    for key in parsed:
        if not isinstance(key, str) or not key.strip():
            raise YamlValidationError("All top-level YAML keys must be non-empty strings.")

    if "model" not in parsed:
        raise YamlValidationError("Missing required 'model' field (expected 'provider/model').", line=1)

    model_val = parsed["model"]
    model_line = _find_yaml_key_line(yaml_text, "model")
    if isinstance(model_val, str):
        if not _is_valid_model_string(model_val.strip()):
            raise YamlValidationError(
                "Invalid 'model' value: expected a non-empty 'provider/model' string without spaces.",
                line=model_line,
            )
        parsed["model"] = model_val.strip()
    elif isinstance(model_val, dict):
        provider_id = model_val.get("providerID")
        model_id = model_val.get("model")
        if not (isinstance(provider_id, str) and provider_id.strip() and isinstance(model_id, str) and model_id.strip()):
            raise YamlValidationError(
                "Invalid 'model' object: requires non-empty 'providerID' and 'model' strings.",
                line=model_line,
            )
    else:
        raise YamlValidationError(
            "Invalid 'model' field: expected a 'provider/model' string.",
            line=model_line,
        )

    if "description" in parsed and parsed["description"] is not None and not isinstance(parsed["description"], str):
        raise YamlValidationError("'description' must be a string.", line=_find_yaml_key_line(yaml_text, "description"))

    if "mode" in parsed:
        mode = parsed["mode"]
        if not isinstance(mode, str) or not mode.strip():
            raise YamlValidationError("'mode' must be a non-empty string.", line=_find_yaml_key_line(yaml_text, "mode"))

    if "prompt" in parsed and parsed["prompt"] is not None and not isinstance(parsed["prompt"], str):
        raise YamlValidationError("'prompt' must be a string.", line=_find_yaml_key_line(yaml_text, "prompt"))

    if "tools" in parsed and parsed["tools"] is not None and not isinstance(parsed["tools"], (dict, list)):
        raise YamlValidationError("'tools' must be a mapping or list.", line=_find_yaml_key_line(yaml_text, "tools"))

    if "temperature" in parsed and parsed["temperature"] is not None:
        if isinstance(parsed["temperature"], bool) or not isinstance(parsed["temperature"], (int, float)):
            raise YamlValidationError("'temperature' must be a number.", line=_find_yaml_key_line(yaml_text, "temperature"))

    for step_key in ("steps", "maxSteps"):
        if step_key in parsed and parsed[step_key] is not None:
            val = parsed[step_key]
            if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                raise YamlValidationError(
                    f"'{step_key}' must be a positive integer.",
                    line=_find_yaml_key_line(yaml_text, step_key),
                )

    return _order_agent_dict(parsed)


# ---------------------------------------------------------------------------
# JSONC tokenizer, parser, and surgical updater
# ---------------------------------------------------------------------------

@dataclass
class _Token:
    kind: str  # "string", "punct", "other"
    start: int
    end: int
    raw: str
    value: Any = None


def _tokenize_jsonc(text: str) -> list[_Token]:
    """Tokenize JSONC into non-comment/non-whitespace tokens with exact character spans."""
    tokens: list[_Token] = []
    n = len(text)
    i = 0

    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue

        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i += 2
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                end_idx = text.find("*/", i + 2)
                if end_idx == -1:
                    line = text.count("\n", 0, i) + 1
                    col = i - text.rfind("\n", 0, i)
                    raise JsoncParseError("Unterminated block comment", line=line, column=col)
                i = end_idx + 2
                continue

        if ch == '"':
            start = i
            i += 1
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                i += 1
            else:
                line = text.count("\n", 0, start) + 1
                col = start - text.rfind("\n", 0, start)
                raise JsoncParseError("Unterminated string literal", line=line, column=col)
            raw = text[start:i]
            try:
                val = json.loads(raw)
            except json.JSONDecodeError as exc:
                line = text.count("\n", 0, start) + 1
                col = start - text.rfind("\n", 0, start)
                raise JsoncParseError(exc.msg, line=line, column=col) from exc
            tokens.append(_Token("string", start, i, raw, val))
            continue

        if ch in "{}[]:,":
            tokens.append(_Token("punct", i, i + 1, ch, ch))
            i += 1
            continue

        start = i
        while i < n and text[i] not in " \t\r\n{}[]:,/" and text[i] != '"':
            i += 1
        if i == start:
            # Bare '/' that wasn't a comment
            i += 1
        raw = text[start:i]
        tokens.append(_Token("other", start, i, raw, raw))

    return tokens


def strip_jsonc(text: str) -> str:
    """Strip JSONC comments and trailing commas while preserving line/column offsets."""
    n = len(text)
    out = list(text)
    i = 0

    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            continue

        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                out[i] = " "
                out[i + 1] = " "
                i += 2
                while i < n and text[i] != "\n":
                    out[i] = " "
                    i += 1
                continue
            if text[i + 1] == "*":
                out[i] = " "
                out[i + 1] = " "
                i += 2
                while i < n:
                    if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                        out[i] = " "
                        out[i + 1] = " "
                        i += 2
                        break
                    if text[i] != "\n":
                        out[i] = " "
                    i += 1
                else:
                    raise JsoncParseError("Unterminated block comment")
                continue
        i += 1

    cleaned = "".join(out)
    # Remove trailing commas before '}' or ']' outside string literals.
    tokens = _tokenize_jsonc(cleaned)
    out2 = list(cleaned)
    for idx in range(1, len(tokens) - 1):
        prev_tok = tokens[idx - 1]
        tok = tokens[idx]
        nxt = tokens[idx + 1]
        prev_is_value = prev_tok.kind in ("string", "other") or (
            prev_tok.kind == "punct" and prev_tok.raw in ("}", "]")
        )
        if prev_is_value and tok.kind == "punct" and tok.raw == "," and nxt.kind == "punct" and nxt.raw in ("}", "]"):
            out2[tok.start] = " "
    return "".join(out2)


def parse_jsonc(text: str) -> dict[str, Any]:
    """Parse JSON or JSONC text into a top-level dictionary."""
    stripped = strip_jsonc(text)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JsoncParseError(exc.msg, line=exc.lineno, column=exc.colno) from exc
    if not isinstance(parsed, dict):
        raise JsoncParseError("Top-level JSON/JSONC value must be an object", line=1, column=1)
    return parsed


@dataclass
class _ObjectMember:
    key: str
    key_start: int
    key_end: int
    val_start: int
    val_end: int
    comma_start: int | None
    comma_end: int | None


def _find_matching_brace(tokens: list[_Token], open_idx: int) -> int:
    open_ch = tokens[open_idx].raw
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for idx in range(open_idx, len(tokens)):
        if tokens[idx].kind == "punct":
            if tokens[idx].raw == open_ch:
                depth += 1
            elif tokens[idx].raw == close_ch:
                depth -= 1
                if depth == 0:
                    return idx
    raise JsoncParseError(f"Unclosed '{open_ch}'")


def _parse_object_members(tokens: list[_Token], open_idx: int) -> tuple[list[_ObjectMember], int]:
    """Return direct key/value spans for the object starting at open_idx and its closing '}' index."""
    close_idx = _find_matching_brace(tokens, open_idx)
    members: list[_ObjectMember] = []
    idx = open_idx + 1

    while idx < close_idx:
        key_tok = tokens[idx]
        if key_tok.kind != "string":
            raise JsoncParseError(f"Expected object key string, got {key_tok.raw!r}")
        if idx + 1 >= close_idx or tokens[idx + 1].raw != ":":
            raise JsoncParseError(f"Expected ':' after key {key_tok.value!r}")

        val_start_idx = idx + 2
        if val_start_idx >= close_idx:
            raise JsoncParseError(f"Missing value for key {key_tok.value!r}")

        val_tok = tokens[val_start_idx]
        if val_tok.kind == "punct" and val_tok.raw in ("{", "["):
            val_end_idx = _find_matching_brace(tokens, val_start_idx)
        else:
            val_end_idx = val_start_idx

        comma_start: int | None = None
        comma_end: int | None = None
        next_idx = val_end_idx + 1
        if next_idx < close_idx and tokens[next_idx].kind == "punct" and tokens[next_idx].raw == ",":
            comma_start = tokens[next_idx].start
            comma_end = tokens[next_idx].end
            next_idx += 1

        members.append(
            _ObjectMember(
                key=key_tok.value,
                key_start=key_tok.start,
                key_end=key_tok.end,
                val_start=tokens[val_start_idx].start,
                val_end=tokens[val_end_idx].end,
                comma_start=comma_start,
                comma_end=comma_end,
            )
        )
        idx = next_idx

    return members, close_idx


def _line_indent_at(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    ws = []
    for ch in text[line_start:pos]:
        if ch in (" ", "\t"):
            ws.append(ch)
        else:
            break
    return "".join(ws)


def _format_json_value(value: Any, base_indent: str) -> str:
    dumped = json.dumps(value, indent=2, ensure_ascii=False)
    lines = dumped.splitlines()
    if len(lines) <= 1:
        return dumped
    return "\n".join([lines[0]] + [f"{base_indent}{line}" for line in lines[1:]])


def _advance_past_inline_comment(text: str, pos: int) -> int:
    """If the remainder of the current line only has whitespace/inline comment, stop before '\n'."""
    n = len(text)
    i = pos
    while i < n and text[i] in " \t":
        i += 1
    if i + 1 < n and text[i:i + 2] == "//":
        nl = text.find("\n", i)
        return n if nl == -1 else nl
    return pos


def update_jsonc_agent(
    text: str,
    json_key: str,
    agent_name: str,
    agent_data: dict[str, Any] | None,
) -> str:
    """Surgically update, insert, or delete an agent entry in JSONC while preserving comments."""
    # Ensure input is valid JSONC first.
    parse_jsonc(text)
    tokens = _tokenize_jsonc(text)
    if not tokens or tokens[0].raw != "{":
        raise JsoncParseError("Top-level JSON/JSONC value must be an object", line=1, column=1)

    root_members, root_close_idx = _parse_object_members(tokens, 0)
    agents_member = next((m for m in root_members if m.key == json_key), None)

    if agents_member is None:
        if agent_data is None:
            return text
        section_payload = {agent_name: agent_data}
        formatted_section = _format_json_value(section_payload, "  ")
        close_tok = tokens[root_close_idx]
        close_indent = _line_indent_at(text, close_tok.start)
        prop_indent = close_indent + "  "
        entry_str = f'{prop_indent}{json.dumps(json_key)}: {formatted_section}'

        if not root_members:
            result = text[:tokens[0].end] + f"\n{entry_str}\n{close_indent}" + text[close_tok.start:]
        else:
            last = root_members[-1]
            if last.comma_end is None:
                insert_anchor = _advance_past_inline_comment(text, last.val_end)
                comma_insert = ","
                before = text[:last.val_end] + comma_insert + text[last.val_end:insert_anchor]
            else:
                insert_anchor = _advance_past_inline_comment(text, last.comma_end)
                before = text[:insert_anchor]
            result = before + f"\n{entry_str}" + text[insert_anchor:]
        parse_jsonc(result)
        return result

    # Locate the '{' token of the agents object.
    agents_open_idx = next(
        (i for i, tok in enumerate(tokens) if tok.start == agents_member.val_start),
        None,
    )
    if agents_open_idx is None or tokens[agents_open_idx].raw != "{":
        raise ConfigError(f"Expected '{json_key}' to be a JSON object in config file.")

    agent_members, agents_close_idx = _parse_object_members(tokens, agents_open_idx)
    target = next((m for m in agent_members if m.key == agent_name), None)

    if agent_data is None:
        if target is None:
            return text
        if len(agent_members) == 1:
            # Remove the only entry inside {...}
            open_tok = tokens[agents_open_idx]
            close_tok = tokens[agents_close_idx]
            result = text[:open_tok.end] + text[close_tok.start:]
        else:
            idx = agent_members.index(target)
            if target.comma_end is not None:
                # Delete from start of line (or key) through trailing comma and newline.
                line_start = text.rfind("\n", 0, target.key_start)
                del_start = line_start + 1 if line_start != -1 and text[line_start + 1:target.key_start].strip() == "" else target.key_start
                del_end = target.comma_end
                if del_end < len(text) and text[del_end] == "\n":
                    del_end += 1
                result = text[:del_start] + text[del_end:]
            else:
                # Last entry without trailing comma: remove preceding comma through val_end.
                prev = agent_members[idx - 1]
                del_start = prev.comma_start if prev.comma_start is not None else prev.val_end
                del_end = target.val_end
                result = text[:del_start] + text[del_end:]
        parse_jsonc(result)
        return result

    if target is not None:
        indent = _line_indent_at(text, target.key_start)
        formatted = _format_json_value(agent_data, indent)
        result = text[:target.val_start] + formatted + text[target.val_end:]
        parse_jsonc(result)
        return result

    # Insert a new agent into existing agents object.
    open_tok = tokens[agents_open_idx]
    close_tok = tokens[agents_close_idx]
    parent_indent = _line_indent_at(text, agents_member.key_start)
    child_indent = (
        _line_indent_at(text, agent_members[0].key_start)
        if agent_members
        else parent_indent + "  "
    )
    formatted = _format_json_value(agent_data, child_indent)
    entry_str = f"{child_indent}{json.dumps(agent_name)}: {formatted}"

    if not agent_members:
        result = text[:open_tok.end] + f"\n{entry_str}\n{parent_indent}" + text[close_tok.start:]
    else:
        last = agent_members[-1]
        if last.comma_end is None:
            insert_anchor = _advance_past_inline_comment(text, last.val_end)
            before = text[:last.val_end] + "," + text[last.val_end:insert_anchor]
        else:
            insert_anchor = _advance_past_inline_comment(text, last.comma_end)
            before = text[:insert_anchor]
        result = before + f"\n{entry_str}" + text[insert_anchor:]

    parse_jsonc(result)
    return result


# ---------------------------------------------------------------------------
# Markdown frontmatter parser & serializer
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n(.*))?\Z", re.DOTALL)


def parse_agent_markdown(text: str, path: Path | None = None) -> dict[str, Any]:
    """Parse an OpenCode Markdown agent file with YAML frontmatter into an agent dict."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        stripped = text.strip()
        return {"prompt": stripped} if stripped else {}

    fm_text, body = match.group(1), match.group(2) or ""
    try:
        fm_data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        loc = f" in {path}" if path else ""
        raise ConfigError(f"Invalid YAML frontmatter{loc}: {exc}") from exc

    if fm_data is None:
        fm_data = {}
    if not isinstance(fm_data, dict):
        loc = f" in {path}" if path else ""
        raise ConfigError(f"Frontmatter{loc} must be a YAML mapping.")

    result = dict(fm_data)
    body_clean = body.strip()
    if body_clean:
        result["prompt"] = body_clean
    return _order_agent_dict(result)


def serialize_agent_markdown(data: dict[str, Any]) -> str:
    """Serialize an agent dict back into YAML frontmatter + Markdown body."""
    fm_data = {k: v for k, v in data.items() if k != "prompt"}
    ordered_fm: dict[str, Any] = {}
    for key in PREFERRED_KEY_ORDER:
        if key in fm_data:
            ordered_fm[key] = fm_data[key]
    for key, val in fm_data.items():
        if key not in ordered_fm:
            ordered_fm[key] = val

    fm_yaml = yaml.dump(
        ordered_fm,
        Dumper=_LiteralSafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    ).rstrip("\n")

    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return f"---\n{fm_yaml}\n---\n\n{prompt.strip()}\n"
    return f"---\n{fm_yaml}\n---\n"


# ---------------------------------------------------------------------------
# Atomic file operations & scope discovery
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically to path using a temp file in the same directory and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o777

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _pick_json_in_dir(directory: Path) -> Path | None:
    for name in ("opencode.jsonc", "opencode.json"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _detect_json_agents_key(parsed: dict[str, Any]) -> str:
    if "agents" in parsed:
        return "agents"
    if "agent" in parsed:
        return "agent"
    return "agents"


def _load_agents_from_json(json_path: Path) -> tuple[list[AgentEntry], str]:
    raw = json_path.read_text(encoding="utf-8")
    parsed = parse_jsonc(raw)
    json_key = _detect_json_agents_key(parsed)
    entries: list[AgentEntry] = []

    raw_agents = parsed.get(json_key)
    if raw_agents is not None:
        if not isinstance(raw_agents, dict):
            raise ConfigError(f"Expected '{json_key}' in {json_path} to be an object.")
        for name, agent_obj in raw_agents.items():
            if not isinstance(agent_obj, dict):
                raise ConfigError(f"Agent '{name}' in {json_path} must be an object.")
            entries.append(
                AgentEntry(
                    name=str(name),
                    data=_order_agent_dict(dict(agent_obj)),
                    origin="jsonc",
                    source_path=json_path,
                    json_key=json_key,
                )
            )
    return entries, json_key


def _load_agents_from_dir(agents_dir: Path) -> list[AgentEntry]:
    entries: list[AgentEntry] = []
    if not agents_dir.is_dir():
        return entries
    for md_path in sorted(agents_dir.glob("*.md")):
        if not md_path.is_file():
            continue
        data = parse_agent_markdown(md_path.read_text(encoding="utf-8"), md_path)
        entries.append(
            AgentEntry(
                name=md_path.stem,
                data=data,
                origin="markdown",
                source_path=md_path,
            )
        )
    return entries


def resolve_config_scope(
    config_arg: str | Path | None = None,
    cwd: Path | None = None,
    global_dir: Path | None = None,
) -> ConfigScope:
    """Resolve OpenCode config scope (explicit --config, project-local, or global fallback)."""
    work_dir = (cwd or Path.cwd()).resolve()
    home_opencode = (global_dir or (Path.home() / ".config" / "opencode")).resolve()

    json_path: Path | None = None
    agents_dir: Path | None = None
    label: str
    root_dir: Path

    if config_arg is not None:
        target = Path(config_arg).expanduser().resolve()
        if target.is_dir():
            root_dir = target
            label = target.name or str(target)
            json_path = _pick_json_in_dir(target) or _pick_json_in_dir(target / ".opencode")
            if (target / "agents").is_dir():
                agents_dir = target / "agents"
            elif (target / ".opencode" / "agents").is_dir():
                agents_dir = target / ".opencode" / "agents"
            elif target.name == "agents":
                agents_dir = target
                json_path = _pick_json_in_dir(target.parent)
            elif json_path is None:
                json_path = target / "opencode.jsonc"
        elif target.suffix.lower() in (".json", ".jsonc"):
            json_path = target
            root_dir = target.parent
            label = target.name
            sibling_agents = target.parent / "agents"
            if target.parent.name in (".opencode", "opencode") and sibling_agents.is_dir():
                agents_dir = sibling_agents
            elif (target.parent / ".opencode" / "agents").is_dir():
                agents_dir = target.parent / ".opencode" / "agents"
        elif target.suffix.lower() == ".md":
            if not target.exists():
                raise ConfigError(f"Config file does not exist: {target}")
            agents_dir = target.parent
            root_dir = target.parent.parent if target.parent.name == "agents" else target.parent
            label = root_dir.name or str(root_dir)
            json_path = _pick_json_in_dir(root_dir)
        else:
            raise ConfigError(f"Unsupported or missing config path: {target}")
    else:
        # Match OpenCode resolution order: project-local if present, else global ~/.config/opencode.
        local_json = _pick_json_in_dir(work_dir) or _pick_json_in_dir(work_dir / ".opencode")
        local_agents = work_dir / ".opencode" / "agents"
        has_local_agents = local_agents.is_dir() and any(local_agents.glob("*.md"))

        if local_json is not None or has_local_agents:
            root_dir = work_dir
            label = f"Project ({work_dir.name})"
            json_path = local_json
            agents_dir = local_agents if local_agents.is_dir() else None
        else:
            root_dir = home_opencode
            label = "Global (~/.config/opencode)"
            json_path = _pick_json_in_dir(home_opencode) or (home_opencode / "opencode.jsonc")
            global_agents = home_opencode / "agents"
            agents_dir = global_agents if global_agents.is_dir() else None

    return load_scope(ConfigScope(label=label, root_dir=root_dir, json_path=json_path, agents_dir=agents_dir))


def load_scope(scope: ConfigScope) -> ConfigScope:
    """Load or reload all agents belonging to a ConfigScope."""
    entries: list[AgentEntry] = []
    json_key = scope.agents_json_key

    if scope.json_path is not None and scope.json_path.is_file():
        json_entries, json_key = _load_agents_from_json(scope.json_path)
        entries.extend(json_entries)

    if scope.agents_dir is not None and scope.agents_dir.is_dir():
        entries.extend(_load_agents_from_dir(scope.agents_dir))

    entries.sort(key=lambda e: (e.name.lower(), e.origin))
    scope.agents_json_key = json_key
    scope.agents = entries
    return scope


def save_agent_from_yaml(entry: AgentEntry, yaml_text: str) -> AgentEntry:
    """Validate YAML text and atomically write the updated agent back to its source file."""
    validated = yaml_to_agent_dict(yaml_text)

    if entry.origin == "markdown":
        content = serialize_agent_markdown(validated)
        atomic_write_text(entry.source_path, content)
    elif entry.origin == "jsonc":
        existing = entry.source_path.read_text(encoding="utf-8") if entry.source_path.is_file() else "{}\n"
        updated = update_jsonc_agent(existing, entry.json_key, entry.name, validated)
        atomic_write_text(entry.source_path, updated)
    else:
        raise ConfigError(f"Unknown agent storage origin: {entry.origin}")

    entry.data = validated
    return entry


def create_agent(
    scope: ConfigScope,
    name: str,
    model: str,
    origin: str | None = None,
    description: str = "",
    mode: str = "subagent",
) -> AgentEntry:
    """Create and persist a new agent in the given ConfigScope."""
    clean_name = name.strip()
    if not clean_name or not AGENT_NAME_RE.match(clean_name):
        raise ConfigError(
            "Agent name must start with a letter or digit and contain only letters, digits, '.', '_', or '-'."
        )

    clean_model = model.strip()
    if not _is_valid_model_string(clean_model):
        raise ConfigError("Model must be a non-empty 'provider/model' string.")

    chosen_origin = origin
    if chosen_origin is None:
        chosen_origin = "markdown" if scope.agents_dir is not None and scope.agents_dir.is_dir() else "jsonc"

    agent_data: dict[str, Any] = {"model": clean_model}
    if description.strip():
        agent_data["description"] = description.strip()
    if mode.strip():
        agent_data["mode"] = mode.strip()
    agent_data = _order_agent_dict(agent_data)

    if chosen_origin == "markdown":
        target_dir = scope.agents_dir or (scope.root_dir / "agents")
        target_path = target_dir / f"{clean_name}.md"
        if target_path.exists():
            raise ConfigError(f"Agent '{clean_name}' already exists at {target_path}.")
        atomic_write_text(target_path, serialize_agent_markdown(agent_data))
        scope.agents_dir = target_dir
        entry = AgentEntry(name=clean_name, data=agent_data, origin="markdown", source_path=target_path)
    elif chosen_origin == "jsonc":
        target_path = scope.json_path or (scope.root_dir / "opencode.jsonc")
        existing = target_path.read_text(encoding="utf-8") if target_path.is_file() else "{\n}\n"
        parsed = parse_jsonc(existing)
        existing_section = parsed.get(scope.agents_json_key)
        if isinstance(existing_section, dict) and clean_name in existing_section:
            raise ConfigError(f"Agent '{clean_name}' already exists in {target_path}.")
        updated = update_jsonc_agent(existing, scope.agents_json_key, clean_name, agent_data)
        atomic_write_text(target_path, updated)
        scope.json_path = target_path
        entry = AgentEntry(
            name=clean_name,
            data=agent_data,
            origin="jsonc",
            source_path=target_path,
            json_key=scope.agents_json_key,
        )
    else:
        raise ConfigError(f"Unsupported agent origin: {chosen_origin}")

    load_scope(scope)
    return next((e for e in scope.agents if e.entry_id == entry.entry_id), entry)


def delete_agent(scope: ConfigScope, entry: AgentEntry) -> None:
    """Delete an agent from disk (Markdown file or JSONC section) and refresh scope."""
    if entry.origin == "markdown":
        if entry.source_path.exists():
            entry.source_path.unlink()
    elif entry.origin == "jsonc":
        if entry.source_path.is_file():
            existing = entry.source_path.read_text(encoding="utf-8")
            updated = update_jsonc_agent(existing, entry.json_key, entry.name, None)
            atomic_write_text(entry.source_path, updated)
    else:
        raise ConfigError(f"Unknown agent storage origin: {entry.origin}")

    load_scope(scope)
