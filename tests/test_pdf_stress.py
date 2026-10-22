"""Большой PDF: по 100 000 символов в каждом из 12 полей, без изменения базы."""

import re
import shutil
import subprocess
import unittest

from src.raci_pdf import SECTIONS, build_raci_pdf


class PdfStressTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('pdftotext'), 'Для проверки содержимого нужен pdftotext (Poppler)')
    def test_all_blocks_with_100_000_characters(self):
        fields = ['raci-about', 'raci-prerequisites']
        fields += [f'raci-{key}-{suffix}' for key, _ in SECTIONS[2:] for suffix in ('before', 'target')]
        self.assertEqual(len(fields), 12)
        values = {'department': 'Отдел развития', 'audience': 'department-head'}
        for field_index, field in enumerate(fields):
            rows = []
            for row_index in range(1000):
                marker = f'STRESS{field_index:02d}ROW{row_index:04d}'
                row = (marker + ' ' + 'Проверяем перенос длинного текста без потерь. ' * 3)[:99]
                rows.append(row + '\n')
            values[field] = ''.join(rows)
            self.assertEqual(len(values[field]), 100_000, field)

        pdf = build_raci_pdf(values).getvalue()
        self.assertTrue(pdf.startswith(b'%PDF-'))
        extracted = subprocess.run(
            ['pdftotext', '-layout', '-', '-'], input=pdf,
            capture_output=True, check=True, timeout=120,
        ).stdout.decode('utf-8')
        pages = extracted.split('\f')[:-1]
        self.assertGreater(len(pages), 6)
        for field_index, field in enumerate(fields):
            with self.subTest(field=field):
                first = f'STRESS{field_index:02d}ROW0000'
                last = f'STRESS{field_index:02d}ROW0999'
                first_page = next(index for index, page in enumerate(pages) if first in page)
                last_page = next(index for index, page in enumerate(pages) if last in page)
                self.assertGreater(last_page, first_page)

        # Убираем только строки служебных заголовков, сохраняя весь текст блоков.
        headings = {
            'Как это повлияет на HR-процессы', 'в Отдел развития', 'для руководителя отдела',
            'Как было', 'Как было (продолжение)',
            'Как будет в целевой картине', 'Как будет в целевой картине (продолжение)',
        }
        headings.update(title for _, title in SECTIONS)
        headings.update(title + ' — продолжение' for _, title in SECTIONS)
        body = '\n'.join(line for line in extracted.splitlines() if line.strip() not in headings)
        normalize = lambda value: re.sub(r'\s+', '', value)
        expected = ''.join(values[field] for field in fields)
        # Краткое сообщение об ошибке вместо печати 1,2 млн символов.
        self.assertTrue(normalize(body) == normalize(expected),
                        'Извлечённый текст отличается от исходного: потеря, повтор или перестановка.')
        print(f'\nPDF: {len(fields)} полей × 100 000 символов, {len(pages)} страниц; текст сохранён полностью.')


if __name__ == '__main__':
    unittest.main()
