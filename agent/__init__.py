"""Агент диалога: поиск и управление выборкой фрагментов, чтение книги."""
from .contracts import Agent, AgentRequest, AgentResult, Workspace
from .graph import Limits, PhilologyAgent

__all__ = ['Agent', 'AgentRequest', 'AgentResult', 'Limits', 'PhilologyAgent', 'Workspace']
