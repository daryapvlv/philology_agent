import unittest

import csv
import io

from frontend.search import Findings


class FakeSearchService:
    def __init__(self):
        self.pages = {
            'search-1': [
                dict(book_id='book', search_id='search-1', revision=1, query='возвращение',
                     candidate_count=2, next_offset=None, remaining_unreviewed=0,
                     truncated_channels=[], warnings=[], exhaustive=False, items=[
                    dict(id='10:60', start_char=10, end_char=60,
                         text='Шёл дождь. Он вернулся домой. Все спали.',
                         review_status='relevant', reason='Прямое совпадение', selected=True,
                         selected_ranges=[dict(start_char=21, end_char=39)],
                         sections=[dict(id='c1', title='Глава 1', role='chapter')]),
                    dict(id='80:120', start_char=80, end_char=120, text='Возможно, это о возвращении.',
                         review_status='uncertain', reason='Нет явного маркера', selected=False,
                         sections=[dict(id='c1', title='Глава 1', role='chapter')]),
                ]),
            ],
        }

    def list_searches(self, limit=20):
        return [dict(search_id='search-1', query='возвращение', candidate_count=2,
                     next_offset=None, updated_at='2026-01-01T12:00:00', current=True,
                     viewed=2, unreviewed=0, rejected=0, selected=1, has_plan=True),
                dict(search_id='words-1', query='возвращ', candidate_count=9,
                     next_offset=None, updated_at='2026-01-01T11:59:00', current=True,
                     viewed=0, unreviewed=9, rejected=0, selected=0, has_plan=False)]

    def continue_search(self, search_id, *, offset=0, page_size=20, review_status=None):
        self.last_review_status = review_status
        pages = self.pages[search_id]
        page = pages[min(offset, len(pages) - 1)]
        if review_status is None:
            return page
        return dict(page, items=[row for row in page['items']
                                 if row['review_status'] == review_status])

    def review_counts(self, search_id):
        return dict(relevant=1, uncertain=1, rejected=0, unreviewed=0)

    def review_candidate(self, search_id, candidate_id, status, reason):
        self.reviewed = (search_id, candidate_id, status, reason)
        return dict(search_id=search_id, candidate_id=candidate_id, status=status, reason=reason)


class FindingsTests(unittest.TestCase):
    def test_csv_export_has_a_header_and_one_row_per_fragment(self):
        findings = Findings('book', 'chat', service=FakeSearchService())
        content = findings.export_csv('search-1', include_all=True)
        self.assertTrue(content.startswith('\ufeff'))
        rows = list(csv.reader(io.StringIO(content.lstrip('\ufeff'))))
        self.assertEqual(rows[0][:4], ['№', 'Статус', 'Раздел', 'Цитата'])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1:4], ['Подходит', 'Глава 1', 'Он вернулся домой.'])


if __name__ == '__main__':
    unittest.main()
