"""A coding agent, in about 400 lines you can read in one sitting.

    python3 main.py "add a retry to the fetch call"     # real model
    python3 main.py --demo                               # mock model, no key
    python3 -m unittest discover -s tests -v             # tests

Layout:
    config.py    which model we talk to, from a JSON file
    provider.py  how we talk to a model
    tools.py     what the model can do
    context.py   what the model can see, and forgetting
    loop.py      think -> act -> observe, until done
"""

from .loop import Agent, Run, Step
from .config import Config, ConfigError, Profile, load_config, load_config_or_builtin
from .context import Context
from .provider import MockProvider, OpenAIProvider, Provider, AssistantTurn, ToolCall
from .tools import Workspace, build_tools, Tool, ToolError

__all__ = [
    "Agent",
    "Run",
    "Step",
    "Config",
    "ConfigError",
    "Profile",
    "load_config",
    "load_config_or_builtin",
    "Context",
    "MockProvider",
    "OpenAIProvider",
    "Provider",
    "AssistantTurn",
    "ToolCall",
    "Workspace",
    "build_tools",
    "Tool",
    "ToolError",
]
