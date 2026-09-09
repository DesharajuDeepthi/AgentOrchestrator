"""Provider-neutral message-content extraction.

Models with reasoning enabled return message content as a list of typed
blocks (thinking / signature / text) instead of a plain string. Every
consumer that turns model output into prose must go through here, so block
reprs never leak into answers, traces, streams, or the UI.
"""

from typing import Any


def text_from_content(content: Any) -> str:
    """Concatenate the prose out of any message-content shape."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                # thinking / signature / redacted blocks are never prose
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def thinking_from_content(content: Any) -> str:
    """Concatenate reasoning text so it can be shown as its own layout —
    never mixed into the prose (see text_from_content)."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            text = block.get("thinking")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)
