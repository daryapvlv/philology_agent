"""Ручной запуск лексического поиска; программный интерфейс поддерживает embedder."""
import argparse
import json

from .service import SearchService
from infrastructure.sqlite import get_search_sessions


def main():
    parser = argparse.ArgumentParser(description='Независимые стратегии поиска в Neo4j')
    parser.add_argument('action', choices=['words', 'graph'])
    parser.add_argument('book_id')
    parser.add_argument('query', nargs='?')
    parser.add_argument('--section', action='append', dest='section_ids')
    parser.add_argument('--target', choices=['text', 'scenes'], default='text')
    parser.add_argument('--scene', action='append', dest='scene_ids')
    parser.add_argument('--hops', type=int, default=1)
    parser.add_argument('--candidate-limit', type=int, default=200)
    parser.add_argument('--page-size', type=int, default=10)
    args = parser.parse_args()
    if args.action == 'words' and not args.query:
        parser.error('Укажи поисковый запрос')
    if args.action == 'graph' and not args.scene_ids:
        parser.error('Укажи начальные сцены через --scene SCENE_ID')
    service = SearchService(args.book_id, research_id='default',
                            sessions=get_search_sessions(args.book_id, 'default'))
    if args.action == 'graph':
        result = service.expand_graph(scene_ids=args.scene_ids, section_ids=args.section_ids,
                                      hops=args.hops, candidate_limit=args.candidate_limit,
                                      page_size=args.page_size)
    else:
        result = service.search_words(args.query, section_ids=args.section_ids, target=args.target,
                                      candidate_limit=args.candidate_limit, page_size=args.page_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
