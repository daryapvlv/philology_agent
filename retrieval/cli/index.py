"""python -m retrieval.cli.index BOOK_ID — явная индексация без эмбеддингов."""
import argparse
import json

from retrieval.indexing import IndexService
from retrieval.embeddings import configured_embedder


def main():
    parser = argparse.ArgumentParser(description='Подготовка поискового индекса книги в Neo4j')
    parser.add_argument('book_id')
    parser.add_argument('--embeddings', action='store_true')
    parser.add_argument('--min-chars', type=int, default=400)
    parser.add_argument('--max-chars', type=int, default=1200)
    args = parser.parse_args()
    embedder = configured_embedder() if args.embeddings else None
    if args.embeddings and embedder is None:
        parser.error('Настрой EMBEDDING_MODEL и ключ провайдера')
    result = IndexService(args.book_id, embedder=embedder).index_book(
        min_chars=args.min_chars, max_chars=args.max_chars)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
