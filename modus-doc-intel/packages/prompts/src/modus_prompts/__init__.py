"""
PromptRegistry — load and render Jinja2 templates from the templates/ directory.
"""
from __future__ import annotations

import importlib.resources
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _templates_dir() -> Path:
    """Resolve templates/ directory, works both installed and in-dev."""
    try:
        ref = importlib.resources.files("modus_prompts") / "templates"
        return Path(str(ref))
    except Exception:
        return Path(__file__).parent / "templates"


class PromptRegistry:
    _env: Environment | None = None

    @classmethod
    def _get_env(cls) -> Environment:
        if cls._env is None:
            cls._env = Environment(
                loader=FileSystemLoader(str(_templates_dir())),
                undefined=StrictUndefined,
                trim_blocks=True,
                lstrip_blocks=True,
            )
        return cls._env

    @classmethod
    def render(cls, template_name: str, context: dict) -> str:
        """Render a template by name (without .j2 extension)."""
        env = cls._get_env()
        tpl = env.get_template(f"{template_name}.j2")
        return tpl.render(**context)

    @classmethod
    def render_messages(
        cls, template_name: str, context: dict
    ) -> list[dict[str, str]]:
        """
        Render a template and return as a list of chat messages.
        Templates should contain --- delimiters to split system/user.
        """
        rendered = cls.render(template_name, context)
        parts = rendered.split("---ROLE_BREAK---")
        if len(parts) == 1:
            return [{"role": "user", "content": rendered.strip()}]
        return [
            {"role": "system", "content": parts[0].strip()},
            {"role": "user", "content": parts[1].strip()},
        ]
