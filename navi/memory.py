"""
Simple JSON-backed long-term memory for NAVI tool calls (`remember` / `recall` / `forget`).

Stores a list of objects `{content, timestamp}` under `assets/memory.json`.
Paths are resolved from this file's location so the repo root does not depend on CWD.
"""

import json
from datetime import datetime
from pathlib import Path

MEMORY_PATH = Path(__file__).resolve().parent / "assets" / "memory.json"


def load_memory():
    """Load all memory entries from disk; return empty list if file missing."""
    if not MEMORY_PATH.exists():
        return []
    with MEMORY_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(memories):
    """Persist the full memory list (overwrite)."""
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MEMORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(memories, f, indent=2)


def remember(information):
    """Append one memory entry and save."""
    memories = load_memory()
    memories.append({
        "content": information,
        "timestamp": datetime.now().isoformat()
    })
    save_memory(memories)
    return f"Remembered: {information}"


def recall(query):
    """
    Return all memories as a bullet list for injection into the LLM context.

    `query` is accepted for API symmetry with tools; filtering is not implemented.
    """
    memories = load_memory()
    if not memories:
        return ""
    memory_text = "\n".join([f"- {m['content']}" for m in memories])
    return memory_text


def forget(topic):
    """Remove entries whose `content` contains `topic` (case-insensitive)."""
    memories = load_memory()
    before = len(memories)
    memories = [m for m in memories if topic.lower() not in m["content"].lower()]
    save_memory(memories)
    removed = before - len(memories)
    return f"Removed {removed} memories about {topic}."


def list_memories():
    """Human-readable numbered list of all entries (for debugging or UI)."""
    memories = load_memory()
    if not memories:
        return "No memories stored."
    lines = []
    for i, m in enumerate(memories, 1):
        lines.append(f"{i}. [{m.get('timestamp', 'no date')}] {m['content']}")
    return "\n".join(lines)
