"""Общие настройки оформления формы и PDF."""

import os
import re


DEFAULT_RACI_ACCENT_COLOR = '#DBD2FF'


def load_accent_color():
    value = os.environ.get('RACI_ACCENT_COLOR', '').strip() or DEFAULT_RACI_ACCENT_COLOR
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise ValueError('RACI_ACCENT_COLOR должен иметь формат #RRGGBB, например #DBD2FF.')
    return value.upper()


RACI_ACCENT_COLOR = load_accent_color()
