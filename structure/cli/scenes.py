"""python -m structure.cli.scenes BOOK_ID CHAPTER_ID"""
import argparse

from llm import configured_client
from retrieval.embeddings import configured_embedder
from structure.application import BookService


def main():
    parser = argparse.ArgumentParser(description='Построить сцены одной главы')
    parser.add_argument('book_id', help='ID книги в Neo4j')
    parser.add_argument('chapter_id', help='id выбранного раздела из sections')
    parser.add_argument('--force', action='store_true', help='Новый анализ без кэша ответов')
    parser.add_argument('--embeddings', action='store_true', help='Посчитать embedding сцены и её summary')
    parser.add_argument("--replace-human", action="store_true", help="Явно заменить ручную разметку")
    args = parser.parse_args()
    client, config = configured_client()
    with client:
        result = BookService(args.book_id).build_scenes(
            args.chapter_id, client, config=config, force=args.force,
            replace_human=args.replace_human,
            embedder=configured_embedder() if args.embeddings else None,
        )
    layer = result['scenes'][args.chapter_id]
    print(f"{layer['status']}: {len(layer['items'])} сцен; "
          f"{layer['reviewed_units']}/{layer['total_units']} фрагментов")
    for issue in layer['issues']:
        print(issue)
    if layer['status'] != 'ready':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
