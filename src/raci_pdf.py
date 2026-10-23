"""PDF RACI с переносом вводных текстов и цветных блоков между страницами."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from src.settings import RACI_ACCENT_COLOR


FONT_DIR = Path(__file__).resolve().parent / 'assets' / 'fonts'
pdfmetrics.registerFont(TTFont('Inter', str(FONT_DIR / 'Inter-Regular.ttf')))
pdfmetrics.registerFont(TTFont('InterBold', str(FONT_DIR / 'Inter-Bold.ttf')))

SECTIONS = [
    ('about', 'О матрице RACI'), ('prerequisites', 'Предпосылки внедрения'),
    ('recruitment', 'Подбор'), ('motivation', 'Мотивация'),
    ('competencies', 'Оценка компетенций'), ('culture', 'Корпоративная культура'),
    ('hr-documents', 'Кадровый документооборот'),
]
AUDIENCES = {
    'department-head': 'для руководителя отдела',
    'sector-head': 'для руководителя сектора',
    'directorate-head': 'для руководителя управления',
    'hidden': '',
}
ACCENT = HexColor(RACI_ACCENT_COLOR)
GREY = HexColor('#F7F7F7')
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 45.5
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN


class PdfValidationError(ValueError):
    pass


def paragraph(text, size=11, bold=False):
    style = ParagraphStyle(
        'raci', fontName='InterBold' if bold else 'Inter',
        fontSize=size, leading=size * 1.13, alignment=TA_LEFT,
        splitLongWords=True, allowOrphans=1, allowWidows=1,
    )
    return Paragraph(escape(text).replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br/>'), style)


def draw_paragraph(pdf, item, x, top, width):
    _, height = item.wrap(width, PAGE_HEIGHT)
    item.drawOn(pdf, x, top - height)
    return height


class PdfLayout:
    def __init__(self, pdf, department, audience):
        self.pdf = pdf
        self.department = department
        self.audience = audience
        self.top = 0
        self.started = False

    def new_page(self, title=None):
        if self.started:
            self.pdf.showPage()
        self.started = True
        self.top = PAGE_HEIGHT - 58
        self.top -= draw_paragraph(
            self.pdf, paragraph(f'Как это повлияет на HR-процессы\nв {self.department}', 20.5, True),
            MARGIN, self.top, CONTENT_WIDTH,
        )
        self.top -= 12
        if self.audience:
            width = pdfmetrics.stringWidth(self.audience, 'Inter', 10.5) + 20
            self.pdf.setFillColor(ACCENT)
            self.pdf.roundRect(MARGIN, self.top - 22, width, 22, 11, fill=1, stroke=0)
            self.pdf.setFillColor(HexColor('#000000'))
            self.pdf.setFont('Inter', 10.5)
            self.pdf.drawString(MARGIN + 10, self.top - 15, self.audience)
            self.top -= 22
        self.top -= 34
        if title:
            self.section_heading(title)

    def section_heading(self, title):
        self.top -= draw_paragraph(self.pdf, paragraph(title, 13, True), MARGIN, self.top, CONTENT_WIDTH)
        self.top -= 16

    def block(self, title, text, label='', color=None):
        pending = paragraph(text)
        continued = False
        while pending is not None:
            caption = label + (' (продолжение)' if continued else '') if label else ''
            overhead = 64 if label else 0
            padding = 20 if color is not None else 0
            minimum = 124 if color is not None else 2 * 11 * 1.13
            available = self.top - MARGIN
            if available < minimum:
                self.new_page(title + ' — продолжение')
                continue
            body_space = available - overhead - 2  # Запас для нижних выносных элементов шрифта.
            _, height = pending.wrap(CONTENT_WIDTH - 40, PAGE_HEIGHT)
            if height <= body_space:
                chunk, rest = pending, None
            else:
                parts = pending.split(CONTENT_WIDTH - 40, body_space)
                if not parts:
                    self.new_page(title + ' — продолжение')
                    continue
                chunk, rest = parts[0], parts[1] if len(parts) > 1 else None
            _, height = chunk.wrap(CONTENT_WIDTH - 40, PAGE_HEIGHT)
            block_height = max(minimum, height + overhead)
            if color is not None:
                self.pdf.setFillColor(color)
                self.pdf.roundRect(MARGIN, self.top - block_height, CONTENT_WIDTH,
                                   block_height, 20, fill=1, stroke=0)
            self.pdf.setFillColor(HexColor('#000000'))
            text_top = self.top - padding
            if caption:
                draw_paragraph(self.pdf, paragraph(caption, 12, True), MARGIN + 20,
                               text_top, CONTENT_WIDTH - 40)
                text_top -= 24
            draw_paragraph(self.pdf, chunk, MARGIN + 20, text_top, CONTENT_WIDTH - 40)
            self.top -= block_height + (13 if color is not None else 28)
            pending = rest
            if pending is not None:
                continued = True
                self.new_page(title + ' — продолжение')


def validate_raci(values, included_sections=None):
    department = values.get('department', '').strip()
    if not department:
        raise PdfValidationError('Укажите название подразделения.')
    if len(department) > 200:
        raise PdfValidationError('Название подразделения должно быть не длиннее 200 символов.')
    audience_key = values.get('audience', '')
    if audience_key not in AUDIENCES:
        raise PdfValidationError('Выберите адресата из списка.')
    included = {key for key, _ in SECTIONS} if included_sections is None else set(included_sections)
    sections = [(key, title) for key, title in SECTIONS if key in included]
    if not sections:
        raise PdfValidationError('Выберите хотя бы один раздел.')

    return department, AUDIENCES[audience_key], sections


def build_raci_pdf(values, included_sections=None):
    department, audience, sections = validate_raci(values, included_sections)
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4, initialFontName='Inter')
    pdf.setTitle(f'Матрица RACI — {department}')
    pdf.setAuthor('Генератор шаблонов')
    layout = PdfLayout(pdf, department, audience)
    for key, title in sections:
        if key not in {'about', 'prerequisites'}:
            layout.new_page(title)
            layout.block(title, values.get(f'raci-{key}-before', ''), 'Как было', GREY)
            layout.block(title, values.get(f'raci-{key}-target', ''), 'Как будет в целевой картине', ACCENT)
            continue
        if not layout.started or layout.top - MARGIN < 70:
            layout.new_page()
        layout.section_heading(title)
        layout.block(title, values.get(f'raci-{key}', ''))
    pdf.save()
    output.seek(0)
    return output
