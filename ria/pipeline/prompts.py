"""Versioned prompt loading. Prompts live in ria/prompts/<name>.v<N>.md."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache
def load_prompt(name: str) -> tuple[str, str]:
    """Return (template, version) for the highest version of a prompt."""
    files = sorted(PROMPT_DIR.glob(f"{name}.v*.md"),
                   key=lambda p: int(re.search(r"\.v(\d+)\.md$", p.name).group(1)))
    if not files:
        raise FileNotFoundError(f"Prompt '{name}' not found in {PROMPT_DIR}")
    path = files[-1]
    return path.read_text(encoding="utf-8"), path.name.removesuffix(".md")


def render(name: str, **values: str) -> tuple[str, str]:
    """Fill {placeholders}. Uses explicit replacement so JSON braces in values are safe."""
    template, version = load_prompt(name)
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    leftover = re.findall(r"\{[a-z_]+\}", out.split("<", 1)[0])
    if leftover:
        raise ValueError(f"Unfilled placeholders in prompt {name}: {leftover}")
    return out, version
