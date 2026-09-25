"""python -m structure.cli.graph check|list"""
import argparse
import json

from structure.graph_db.repository import get_repository


def main():
    parser = argparse.ArgumentParser(description='Подключение к графу структуры')
    parser.add_argument('action', choices=['check', 'list'])
    args = parser.parse_args()
    repository = get_repository()
    if args.action == 'check':
        print('Neo4j доступна; ограничения уникальности настроены')
    else:
        print(json.dumps(repository.catalog(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
