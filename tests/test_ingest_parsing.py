from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile
from ebooklib import epub

from ingest.parsing import Document, epigraph_merges, parse_book
from ingest.workflow import _merge_epub_epigraph_blocks, _merge_epub_epigraphs
from structure.graph_db.repository import content_block_rows


class IngestParsingTests(unittest.TestCase):
    def parse_markdown(self, text):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "book.md"
            path.write_text(text, encoding="utf-8")
            return parse_book(Document(path))

    def test_epub_uses_opf_title_and_spine_html(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "META-INF/container.xml",
                    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="OPS/content.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles>'
                    '</container>',
                )
                archive.writestr(
                    "OPS/content.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    '<dc:title>Название EPUB</dc:title>'
                    '</metadata>'
                    '<manifest><item id="c1" href="chapter.xhtml" '
                    'media-type="application/xhtml+xml"/></manifest>'
                    '<spine><itemref idref="c1"/></spine>'
                    '</package>',
                )
                archive.writestr(
                    "OPS/chapter.xhtml",
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                    '<h1>Книга</h1><h2>Глава</h2><p>Текст.</p>'
                    '</body></html>',
                )

            parsed = parse_book(Document(path))

        self.assertEqual(parsed.title, "Название EPUB")
        self.assertEqual([element["text"] for element in parsed.elements],
                         ["Книга", "Глава", "Текст."])
        self.assertEqual([(section["title"], section["role"], section["level"])
                          for section in parsed.sections],
                         [("Глава", "chapter", 1)])

    def test_epub_marks_inline_footnote_but_not_its_reference(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            book = epub.EpubBook()
            book.set_identifier("inline-note")
            book.set_title("Книга")
            book.set_language("ru")
            chapter = epub.EpubHtml(title="Глава", file_name="chapter.xhtml")
            chapter.content = (
                '<h1>Глава</h1><p>Текст <a epub:type="noteref" href="#n1">1</a>.</p>'
                '<aside epub:type="footnote" id="n1"><p>Примечание.</p></aside>'
                '<p>Продолжение.</p>'
            )
            book.add_item(chapter)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.toc = [epub.Link("chapter.xhtml", "Глава", "chapter")]
            book.spine = [chapter]
            epub.write_epub(str(path), book)
            parsed = parse_book(Document(path))

        self.assertEqual([element["text"] for element in parsed.elements],
                         ["Глава", "Текст 1 .", "Примечание.", "Продолжение."])
        self.assertEqual([(block["start"], block["end"]) for block in parsed.blocks],
                         [(2, 3)])

    def test_epub_epigraph_container_assigns_role_and_attribution(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            book = epub.EpubBook()
            book.set_identifier("epigraph-lines")
            book.set_title("Книга")
            book.set_language("ru")
            chapter = epub.EpubHtml(title="Глава", file_name="chapter.xhtml")
            chapter.content = (
                '<h1>Глава</h1><blockquote class="epigraph"><div>'
                '<p>Строка один</p><p>Строка два</p>'
                '<blockquote><div>Источник</div></blockquote>'
                '</div></blockquote><p>Основной текст.</p>'
            )
            book.add_item(chapter)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.toc = [epub.Link("chapter.xhtml", "Глава", "chapter")]
            book.spine = [chapter]
            epub.write_epub(str(path), book)
            parsed = parse_book(Document(path))

        self.assertEqual([element["text"] for element in parsed.elements],
                         ["Глава", "Строка один", "Строка два", "Источник", "Основной текст."])
        self.assertEqual(epigraph_merges(parsed.elements),
                         [{"start": 1, "end": 4, "source": "epub"}])
        self.assertEqual([(block["role"], block["start"], block["end"],
                           block["attribution_start"]) for block in parsed.blocks],
                         [("epigraph", 1, 4, 3)])
        rows = content_block_rows("book", parsed.sections, parsed.blocks, parsed.elements)
        self.assertEqual(next(row["text"] for row in rows if row["role"] == "body"),
                         "Основной текст.")
        self.assertEqual(next(row["attribution"] for row in rows
                              if row["role"] == "epigraph"), "Источник")
        self.assertEqual(_merge_epub_epigraphs(
            {"element_merges": [{"start": 1, "end": 3, "source": "human"}]},
            parsed.elements,
        ), [{"start": 1, "end": 3, "source": "human"}])
        self.assertEqual(_merge_epub_epigraphs(
            {"blocks": parsed.blocks},
            parsed.elements,
        ), [{"start": 1, "end": 3, "source": "epub"}])
        blocks = _merge_epub_epigraph_blocks(
            {"sections": parsed.sections,
             "blocks": [{"id": "manual", "section_id": parsed.sections[0]["id"],
                         "role": "epigraph", "start": 1, "end": 2, "source": "human"}]},
            parsed.elements, "book",
        )
        self.assertEqual([(block["source"], block["start"], block["end"],
                           block.get("attribution_start")) for block in blocks],
                         [("human", 1, 2, None), ("epub", 2, 4, 3)])


if __name__ == "__main__":
    unittest.main()
