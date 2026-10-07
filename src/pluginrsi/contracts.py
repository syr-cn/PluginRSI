from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Task:
    id: str
    instruction: str
    metadata: dict = field(default_factory=dict)


@dataclass
class SolveResult:
    output: str
    artifacts: list[str] = field(default_factory=list)
    termination_reason: str = "completed"


@dataclass
class SkillContent:
    instructions: str
    resources: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    artifacts: list[str] = field(default_factory=list)


@dataclass
class MemoryItem:
    id: str
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class PluginServices:
    model: Any
    environment: Any
    emit: Any
    resource_dir: Path
    state_dir: Path


# The four plugin kinds: Role, Skill, Tool, Memory.
class Plugin(ABC):
    def __init__(self, config: dict, services: PluginServices):
        self.config = config
        self.services = services


class Role(Plugin):
    @abstractmethod
    def render(self, context: dict) -> str: ...


class Skill(Plugin):
    @abstractmethod
    def load(self, context: dict) -> SkillContent: ...


class Tool(Plugin):
    @abstractmethod
    def schema(self) -> dict: ...

    @abstractmethod
    async def execute(self, arguments: dict) -> ToolResult: ...


class Memory(Plugin):
    @abstractmethod
    async def update(self, experience: dict) -> None: ...

    @abstractmethod
    async def retrieve(self, query: dict) -> list[MemoryItem]: ...
