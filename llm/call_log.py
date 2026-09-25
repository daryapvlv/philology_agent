"""Отдельный консольный логгер вызовов LLM.

Reflex настраивает логгер HTTP-сервера, но оставляет корневой Python-логгер на
уровне WARNING. Поэтому INFO-события приложения настраиваются здесь отдельно.
"""
import logging
import os


def _configured_logger():
    logger = logging.getLogger("philology.llm")
    level_name = os.getenv("LLM_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    if not any(getattr(handler, "_philology_llm", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        handler._philology_llm = True
        logger.addHandler(handler)
    return logger


logger = _configured_logger()
