from __future__ import annotations

import json
import re
from typing import Any


def parse_json_payload(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            payload, _ = decoder.raw_decode(text[match.start() :])
            return payload
        except json.JSONDecodeError:
            continue
    raise ValueError("Model response contains no valid JSON payload")


def recover_partial_object_array(content: str, field: str) -> list[dict[str, Any]]:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*\[', content)
    if not match:
        return []
    decoder = json.JSONDecoder()
    cursor = match.end()
    rows: list[dict[str, Any]] = []
    while cursor < len(content):
        while cursor < len(content) and (content[cursor].isspace() or content[cursor] == ","):
            cursor += 1
        if cursor >= len(content) or content[cursor] == "]":
            break
        try:
            payload, consumed = decoder.raw_decode(content[cursor:])
        except json.JSONDecodeError:
            break
        if not isinstance(payload, dict):
            break
        rows.append(payload)
        cursor += consumed
    return rows
