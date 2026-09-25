"""Публичные контракты агента, не зависящие от UI и хранилищ."""
from dataclasses import dataclass, field
from typing import Callable, Protocol


ProgressReporter = Callable[[str], None]


@dataclass(frozen=True)
class AgentRequest:
    messages: list[dict]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    content: str
    trace: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Workspace:
    """Сервисы книги диалога; без выбранной книги оба None."""
    search: object = None
    book: object = None


class Agent(Protocol):
    def run(self, request: AgentRequest, workspace: Workspace,
            progress: ProgressReporter | None = None) -> AgentResult: ...
