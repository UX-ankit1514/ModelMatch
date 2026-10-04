"""Read the few Codex settings ModelMatch needs, without a TOML library.

Codex keeps its settings in $CODEX_HOME/config.toml (default ~/.codex/config.toml).
Only simple top-level `key = value` lines are read (model, openai_base_url,
model_provider, model_catalog_json); tables and everything else are left alone.
Codex's own model list comes from the file named by model_catalog_json, or else
the models_cache.json Codex downloads.

Standard library only (Python 3.7+): the Codex hook and the settings tool import it.
"""

import json
import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"""^\s*([A-Za-z0-9_.-]+|"(?:[^"\\]|\\.)*"|'[^']*')\s*=\s*(.*)$""")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "\\": "\\", '"': '"'}


def codex_home(env=None):
    env = os.environ if env is None else env
    value = env.get("MODELMATCH_CODEX_HOME") or env.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(value).expanduser()


def _value_state(text, depth):
    """Follow brackets through one line of a value; return (depth, open multi-line string delimiter)."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            break
        if text.startswith('"""', i) or text.startswith("'''", i):
            delimiter = text[i:i + 3]
            end = text.find(delimiter, i + 3)
            if end == -1:
                return depth, delimiter
            i = end + 3
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "'":
            end = text.find("'", i + 1)
            i = end + 1 if end != -1 else n
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth = max(0, depth - 1)
        i += 1
    return depth, None


def scan_top_level(text):
    """Return (keys, end) for the top-level lines of a TOML document.

    keys maps each top-level key to (line index, raw value text); end is the index of
    the first table header. Multi-line arrays and strings are followed, so a "[" inside
    a value is never mistaken for a table.
    """
    lines = text.splitlines()
    keys = {}
    depth, multi = 0, None
    for index, line in enumerate(lines):
        if multi:
            end = line.find(multi)
            if end != -1:
                depth, multi = _value_state(line[end + 3:], depth)
            continue
        if depth:
            depth, multi = _value_state(line, depth)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            return keys, index
        match = _KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key[0] in "\"'":
            key = key[1:-1]
        keys.setdefault(key, (index, match.group(2)))
        depth, multi = _value_state(match.group(2), 0)
    return keys, len(lines)


def parse_scalar(raw):
    """A TOML string, boolean or number; None for anything else (arrays, tables, ...)."""
    raw = raw.strip()
    if raw.startswith('"""') or raw.startswith("'''"):
        return None
    if raw.startswith('"'):
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                if nxt in _ESCAPES:
                    out.append(_ESCAPES[nxt])
                    i += 2
                    continue
                width = {"u": 4, "U": 8}.get(nxt)
                if width:
                    try:
                        out.append(chr(int(raw[i + 2:i + 2 + width], 16)))
                    except ValueError:
                        return None
                    i += 2 + width
                    continue
                return None
            if c == '"':
                return "".join(out)
            out.append(c)
            i += 1
        return None
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:end] if end != -1 else None
    value = raw.split("#", 1)[0].strip()
    if value in ("true", "false"):
        return value == "true"
    for kind in (int, float):
        try:
            return kind(value.replace("_", ""))
        except ValueError:
            pass
    return None


def read_config(home):
    """Top-level scalar settings of $CODEX_HOME/config.toml ({} if missing or unreadable)."""
    try:
        text = (Path(home) / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    keys, _ = scan_top_level(text)
    return {key: parse_scalar(raw) for key, (_, raw) in keys.items()}


def model_list_file(home, config):
    custom = config.get("model_catalog_json")
    if isinstance(custom, str) and custom.strip():
        path = Path(custom).expanduser()
        path = path if path.is_absolute() else Path(home) / path
        if path.exists():
            return path
    return Path(home) / "models_cache.json"


def load_model_entries(home, config=None):
    """Codex's selectable models, best first, and the file they came from.

    Each entry: id, name, description, reasoning_levels (tuple, or None if unknown),
    supports_verbosity (bool or None), service_tiers (tuple, or None if unknown).
    """
    config = read_config(home) if config is None else config
    path = model_list_file(home, config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return [], path
    raw = data.get("models") if isinstance(data, dict) else data
    entries = []
    for position, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict) or item.get("visibility") == "hide":
            continue
        slug = item.get("slug") or item.get("id") or item.get("model")
        if not isinstance(slug, str) or not slug.strip():
            continue
        levels = item.get("supported_reasoning_levels")
        tiers = item.get("service_tiers")
        verbosity = item.get("support_verbosity")
        priority = item.get("priority")
        entries.append({
            "id": slug.strip(),
            "name": str(item.get("display_name") or ""),
            "description": str(item.get("description") or ""),
            "reasoning_levels": None if not isinstance(levels, list) else tuple(
                str(level.get("effort") if isinstance(level, dict) else level) for level in levels if level),
            "supports_verbosity": verbosity if isinstance(verbosity, bool) else None,
            "service_tiers": None if not isinstance(tiers, list) else tuple(
                str(tier.get("id") if isinstance(tier, dict) else tier) for tier in tiers if tier),
            "order": (priority if isinstance(priority, (int, float)) and not isinstance(priority, bool) else 10 ** 6,
                      position),
        })
    entries.sort(key=lambda entry: entry["order"])
    for entry in entries:
        del entry["order"]
    return entries, path
