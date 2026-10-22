"""HTML-отчёт RACI: данные, выбор разделов, валидация и совместимость PDF."""
import unittest
from flask import Flask
from src.app import generate_raci, generate
from src.settings import RACI_ACCENT_COLOR
from pathlib import Path


class RaciReportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        app = Flask(__name__, template_folder=str(root / 'templates'), static_folder=str(root / 'assets'))
        app.config['TESTING'] = True
        app.jinja_env.globals['raci_accent_color'] = RACI_ACCENT_COLOR
        app.add_url_rule('/raci_generate', 'generate_raci', generate_raci, methods=['POST'])
        app.add_url_rule('/ft_generate', 'generate', generate, methods=['POST'])
        app.add_url_rule('/admin', 'admin_editor', lambda: '')
        self.client = app.test_client()
        self.data = dict(output='html', department='HR <отдел>', audience='sector-head',
                         include_about='1', include_recruitment='1')
        self.data.update({'raci-about': 'Первая строка\nВторая строка',
                          'raci-recruitment-before': '<script>alert(1)</script>',
                          'raci-recruitment-target': 'Целевая картина',
                          'raci-prerequisites': 'Исключённый текст'})

    def test_html_content_and_selection(self):
        response = self.client.post('/raci_generate', data=self.data)
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/html')
        self.assertNotIn('Content-Disposition', response.headers)
        for text in ['HR &lt;отдел&gt;', 'для руководителя сектора', 'Первая строка\nВторая строка',
                     '&lt;script&gt;', 'Целевая картина', 'Как было', RACI_ACCENT_COLOR, 'Inter-Regular.ttf']:
            self.assertIn(text, html)
        for text in ['Исключённый текст', '<script>', 'Предпосылки внедрения', 'Мотивация']:
            self.assertNotIn(text, html)
        with self.client.get('/assets/fonts/Inter-Regular.ttf') as font:
            self.assertEqual(font.status_code, 200)

    def test_hidden_audience_and_long_text(self):
        self.data.update(audience='hidden')
        text = 'Начало\n' + 'текст ' * 20000 + '\nКонец'
        self.data['raci-about'] = text
        html = self.client.post('/raci_generate', data=self.data).get_data(as_text=True)
        self.assertIn(text, html)
        self.assertNotIn('для руководителя', html)

    def test_errors_preserve_form(self):
        for change, expected in [({'department': ''}, 'Укажите название'),
                                 ({'audience': 'invalid'}, 'Выберите адресата'),
                                 ({'include_about': '', 'include_recruitment': ''}, 'Выберите хотя бы один')]:
            with self.subTest(change=change):
                response = self.client.post('/raci_generate', data={**self.data, **change})
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 422)
                self.assertIn(expected, html)
                self.assertIn('Первая строка\nВторая строка', html)
                self.assertIn('<option value="raci" selected>', html)
                self.assertNotIn('<script>alert(1)</script>', html)

    def test_pdf_and_meeting_still_work(self):
        self.data.pop('output')
        response = self.client.post('/raci_generate', data=self.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF'))
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertEqual(self.client.post('/ft_generate', data=dict(meeting_name='Запрос', fio='Иван')).status_code, 200)
