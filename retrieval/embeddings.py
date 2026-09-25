"""Адаптер OpenAI-совместимого embedding API для поискового представления."""
import os

from openai import OpenAI


class APIEmbedder:
    def __init__(self, model, api_key, base_url=None):
        self.api_model = model
        self.api_key = api_key
        self.base_url = base_url
        self.model = f'{base_url or "https://api.openai.com/v1"}/{model}'

    def embed_documents(self, texts):
        # Сбой соединения/DNS/5xx повторяем с backoff SDK, как transient_retries у
        # LLM-запросов: иначе один сетевой сбой откатывает сцены целой главы.
        with OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=90, max_retries=2) as client:
            response = client.embeddings.create(model=self.api_model, input=texts)
        return [r.embedding for r in sorted(response.data, key=lambda r: r.index)]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


def configured_embedder():
    model = os.getenv('EMBEDDING_MODEL')
    if not model:
        return None
    key = os.getenv('EMBEDDING_API_KEY') or os.getenv('OPENAI_API_KEY')
    if not key:
        raise ValueError('Для EMBEDDING_MODEL задай EMBEDDING_API_KEY или OPENAI_API_KEY')
    return APIEmbedder(model, key, os.getenv('EMBEDDING_BASE_URL') or os.getenv('OPENAI_BASE_URL'))
