"""Проверки PDF; извлечение текста требует утилиту pdftotext из Poppler."""

import re
import shutil
import subprocess
import unittest

from src.raci_pdf import AUDIENCES, SECTIONS, PdfValidationError, build_raci_pdf


class RaciPdfTests(unittest.TestCase):
    def values(self, audience='department-head'):
        result = {'department': 'Отдел развития', 'audience': audience}
        for key, _ in SECTIONS:
            if key in {'about', 'prerequisites'}:
                result[f'raci-{key}'] = f'Текст {key}\nВторая строка <пример> & символы'
            else:
                result[f'raci-{key}-before'] = 'Исходное состояние'
                result[f'raci-{key}-target'] = 'Целевое состояние'
        return result

    def test_six_pages_and_embedded_fonts(self):
        pdf = build_raci_pdf(self.values()).getvalue()
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertEqual(len(re.findall(rb'/Type\s*/Page\b', pdf)), 6)
        self.assertIn(b'/FontFile2', pdf)
        self.assertIn(b'Inter-Regular', pdf)
        self.assertIn(b'Inter-Bold', pdf)

    @unittest.skipUnless(shutil.which('pdftotext'), 'Нужен pdftotext')
    def test_text_order_audiences_and_special_characters(self):
        for audience, label in AUDIENCES.items():
            with self.subTest(audience=audience):
                pdf = build_raci_pdf(self.values(audience)).getvalue()
                text = subprocess.run(['pdftotext', '-layout', '-', '-'], input=pdf,
                                      capture_output=True, check=True).stdout.decode()
                pages = text.split('\f')[:-1]
                self.assertEqual(len(pages), 6)
                expected_titles = [[title for _, title in SECTIONS[:2]], *[[title] for _, title in SECTIONS[2:]]]
                for page, titles in zip(pages, expected_titles):
                    for title in titles:
                        self.assertIn(title, page)
                    self.assertIn('Отдел развития', page)
                    if label:
                        self.assertIn(label, page)
                    else:
                        self.assertNotIn('для руководителя', page)
                self.assertIn('Вторая строка <пример> & символы', pages[0])
                self.assertIn('Текст about', pages[0])
                self.assertIn('Текст prerequisites', pages[0])
                self.assertLess(pages[0].index('О матрице RACI'), pages[0].index('Предпосылки внедрения'))
                self.assertIn('Как было', pages[1])
                self.assertIn('Как будет в целевой картине', pages[1])
                self.assertIn('Исходное состояние', pages[1])
                self.assertIn('Целевое состояние', pages[1])

    def test_empty_blocks_still_produce_six_pages(self):
        pdf = build_raci_pdf({'department': 'Отдел', 'audience': 'hidden'}).getvalue()
        self.assertEqual(len(re.findall(rb'/Type\s*/Page\b', pdf)), 6)

    def test_long_intro_adds_pages(self):
        values = self.values()
        long_text = 'Строка текста\n' * 45
        for key in ('about', 'prerequisites'):
            build_raci_pdf({**values, f'raci-{key}': long_text})
        pdf = build_raci_pdf({**values, 'raci-about': long_text, 'raci-prerequisites': long_text}).getvalue()
        self.assertGreater(len(re.findall(rb'/Type\s*/Page\b', pdf)), 6)

    @unittest.skipUnless(shutil.which('pdftotext'), 'Нужен pdftotext')
    def test_paginated_text_is_complete_ordered_and_inside_page(self):
        import xml.etree.ElementTree as ET

        fields = ['raci-about', 'raci-prerequisites', 'raci-recruitment-before', 'raci-recruitment-target']
        for field in fields:
            with self.subTest(field=field):
                # Более 20 000 символов в каждом поле, с уникальными маркерами строк.
                markers = [f'RACILINE{index:04d}' for index in range(700)]
                long_text = '\n'.join(marker + ' Текст для проверки переноса.' for marker in markers)
                self.assertGreater(len(long_text), 20000)
                pdf = build_raci_pdf({**self.values(), field: long_text}).getvalue()
                text = subprocess.run(['pdftotext', '-', '-'], input=pdf, capture_output=True, check=True).stdout.decode()
                self.assertEqual(re.findall(r'RACILINE\d{4}', text), markers)
                self.assertIn('продолжение', text)
                self.assertGreater(len(re.findall(rb'/Type\s*/Page\b', pdf)), 6)
                bbox = subprocess.run(['pdftotext', '-bbox', '-', '-'], input=pdf, capture_output=True, check=True).stdout
                root = ET.fromstring(bbox)
                for word in root.findall('.//{*}word'):
                    self.assertGreaterEqual(float(word.attrib['yMin']), 45)
                    self.assertLessEqual(float(word.attrib['yMax']), 842 - 45)

    def test_invalid_input(self):
        for values in [
            {'department': '', 'audience': 'hidden'},
            {'department': 'Отдел', 'audience': 'unexpected'},
            {'department': 'я' * 201, 'audience': 'hidden'},
        ]:
            with self.subTest(values=list(values)):
                with self.assertRaises(PdfValidationError):
                    build_raci_pdf(values)

    def test_no_selected_sections(self):
        with self.assertRaisesRegex(PdfValidationError, 'Выберите хотя бы один раздел'):
            build_raci_pdf(self.values(), included_sections=set())

    @unittest.skipUnless(shutil.which('pdftotext'), 'Нужен pdftotext')
    def test_section_selection_omits_titles_text_and_pages(self):
        values = self.values()
        for key, _ in SECTIONS:
            values[f'raci-{key}'] = f'BODY_{key}'
            values[f'raci-{key}-before'] = f'BEFORE_{key}'
            values[f'raci-{key}-target'] = f'TARGET_{key}'
        all_keys = {key for key, _ in SECTIONS}
        selections = [all_keys - {key} for key, _ in SECTIONS]
        selections += [{'about'}, {'prerequisites'}, {'recruitment'}, {'about', 'prerequisites'},
                       all_keys - {'about', 'prerequisites'}]
        for selected in selections:
            with self.subTest(selected=sorted(selected)):
                pdf = build_raci_pdf(values, included_sections=selected).getvalue()
                expected_pages = bool(selected & {'about', 'prerequisites'}) + len(selected - {'about', 'prerequisites'})
                self.assertEqual(len(re.findall(rb'/Type\s*/Page\b', pdf)), expected_pages)
                text = subprocess.run(['pdftotext', '-', '-'], input=pdf, capture_output=True, check=True).stdout.decode()
                for key, title in SECTIONS:
                    markers = ([f'BODY_{key}'] if key in {'about', 'prerequisites'}
                               else [f'BEFORE_{key}', f'TARGET_{key}'])
                    if key in selected:
                        self.assertIn(title, text)
                        for marker in markers:
                            self.assertIn(marker, text)
                    else:
                        self.assertNotIn(title, text)
                        for marker in markers:
                            self.assertNotIn(marker, text)

    def test_excluded_long_sections_do_not_generate_continuations(self):
        values = self.values()
        values['raci-about'] = 'Не включать\n' * 5000
        values['raci-recruitment-target'] = 'Не включать\n' * 5000
        pdf = build_raci_pdf(values, included_sections={'motivation'}).getvalue()
        self.assertEqual(len(re.findall(rb'/Type\s*/Page\b', pdf)), 1)


if __name__ == '__main__':
    unittest.main()
