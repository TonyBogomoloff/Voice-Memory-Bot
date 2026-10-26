"""Build the original Voice Memory blue audio icon set (requires system librsvg/Cairo and Pillow)."""
from __future__ import annotations

import ctypes
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLUE = '#1688FF'
LIGHT = '#62B7FF'
DARK = '#0865DE'
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'assets/emoji/blue_audio_v1'


def path(d, **attrs):
    return '<path d="' + d + '" ' + ' '.join(f'{k}="{v}"' for k, v in attrs.items()) + '/>'


def rect(x, y, w, h, r=2, **attrs):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" ' + ' '.join(f'{k}="{v}"' for k, v in attrs.items()) + '/>'


def circle(x, y, r, **attrs):
    return f'<circle cx="{x}" cy="{y}" r="{r}" ' + ' '.join(f'{k}="{v}"' for k, v in attrs.items()) + '/>'


def lines(*coords):
    return ''.join(path('M' + ' L'.join(str(x) for x in c)) for c in coords)


def icon(body):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24">'
            f'<g fill="none" stroke="{BLUE}" stroke-width="1.85" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</g></svg>')


def folder_settings_icon():
    # Leave the lower-right corner open for a legible eight-tooth gear.
    outline = path('M10 20 H5 Q3 20 3 18 V5 Q3 3.5 4.5 3.5 H9 L11 6 H18.5 Q20 6 20 8 V10 M3 8 H17')
    points = []
    for tooth in range(8):
        for offset, radius in ((-22.5, 4), (-12, 5.3), (12, 5.3), (22.5, 4)):
            angle = math.radians(tooth * 45 + offset)
            points.append(f'{17 + radius * math.cos(angle):.3f} {17 + radius * math.sin(angle):.3f}')
    gear = 'M' + ' L'.join(points) + ' Z M19 17 A2 2 0 1 0 15 17 A2 2 0 1 0 19 17 Z'
    return outline + path(gear, fill=LIGHT, stroke='none', **{'fill-rule': 'evenodd'})


folder = path('M3 7 V5 Q3 3.5 4.5 3.5 H9 L11 6 H19.5 Q21 6 21 8 V18 Q21 20 19 20 H5 Q3 20 3 18 Z')
tape = rect(2.5, 5, 19, 14, 2.5) + rect(5.5, 8, 13, 5, 2.5, stroke=LIGHT) + circle(8, 10.5, .7, fill=BLUE, stroke='none') + circle(16, 10.5, .7, fill=BLUE, stroke='none') + path('M7 19 L8.5 15.5 H15.5 L17 19')
mic = rect(8, 2.5, 8, 12, 4) + path('M5.5 10.5 V12 A6.5 6.5 0 0 0 18.5 12 V10.5 M12 18.5 V22 M8 22 H16') + path('M11 6 H13 M11 9 H13', stroke=LIGHT)
page = path('M6 3 H14 L19 8 V21 H5 V4 Q5 3 6 3 Z M14 3 V8 H19')
icons = [
    ('microphone', '🎙️', 'Микрофон', mic),
    ('recorder', '🎚️', 'Диктофон', rect(6,2,12,20,3)+rect(8.5,4.5,7,7,1.5,stroke=LIGHT)+path('M9 7 H15 M9 9 H15')+circle(12,16,2,fill=BLUE)+circle(9,20,.35,fill=LIGHT,stroke='none')+circle(15,20,.35,fill=LIGHT,stroke='none')),
    ('cassette', '📼', 'Кассета', tape),
    ('folder', '📁', 'Папка', folder + path('M8 12 V16 M12 10 V16 M16 13 V16',stroke=LIGHT)),
    ('folder_active', '✅', 'Выбрана', folder + path('M8 13 L11 16 L17 10',stroke=LIGHT)),
    ('folders', '📂', 'Все папки', path('M5 3 H11 L13 5 H21 M3 8 V6 H9 L11 9 H19 V20 H3 Z')+path('M7 13 H15 M7 16 H12',stroke=LIGHT)),
    ('add', '➕', 'Создать', circle(12,12,9)+path('M12 7 V17 M7 12 H17',stroke=LIGHT)),
    ('search', '🔎', 'Поиск', circle(10,10,6.5)+path('M15 15 L21 21')+path('M6.5 10 H8 L9 8 L11 12 L12 10 H13.5',stroke=LIGHT)),
    ('ai', '🤖', 'ИИ', rect(3,6,18,14,4)+path('M12 3 V6 M8 3 H16')+rect(6,10,3,3,1,fill=LIGHT,stroke='none')+rect(15,10,3,3,1,fill=LIGHT,stroke='none')+path('M8 17 H16',stroke=LIGHT)),
    ('export', '📦', 'Экспорт', path('M4 8 H8 M16 8 H20 V21 H4 V8 M4 15 H8 L10 18 H14 L16 15 H20')+path('M12 2 V12 M8 8 L12 12 L16 8',stroke=LIGHT)),
    ('settings', '⚙️', 'Настройки', path('M5 3 V7 M5 11 V21 M12 3 V13 M12 17 V21 M19 3 V7 M19 11 V21')+rect(2.5,7,5,4,1,fill=BLUE)+rect(9.5,13,5,4,1,fill=BLUE)+rect(16.5,7,5,4,1,fill=BLUE)),
    ('help', 'ℹ️', 'Помощь', circle(12,12,9)+circle(12,7.2,1,fill=LIGHT,stroke='none')+path('M12 11 V17',stroke=LIGHT)),
    ('delete', '🗑️', 'Удалить', path('M4 6 H20 M9 6 V3 H15 V6 M6 6 L7 21 H17 L18 6')+path('M10 10 V17 M14 10 V17',stroke=LIGHT)),
    ('edit', '✏️', 'Правка', path('M4 20 L5 14 L16 3 Q17 2 18 3 L21 6 Q22 7 21 8 L10 19 Z M14 5 L19 10 M5 14 L10 19')+path('M3 22 H14',stroke=LIGHT)),
    ('text', '📄', 'Текст', page+path('M8 12 H16 M8 15 H16 M8 18 H13',stroke=LIGHT)),
    ('notes', '📝', 'Записи', rect(4,3,16,18,2)+path('M8 2 V5 M16 2 V5 M8 9 H16 M8 13 H16 M8 17 H12',stroke=LIGHT)),
    ('photo', '📷', 'Фото', path('M3 7 H7 L9 4 H15 L17 7 H21 V20 H3 Z')+circle(12,13,4,stroke=LIGHT)+circle(18,9,.6,fill=LIGHT,stroke='none')),
    ('video', '📹', 'Кружок', rect(2,6,13,14,2)+path('M15 10 L22 7 V19 L15 16 Z')+circle(6,3,2,stroke=LIGHT)+circle(12,3,2,stroke=LIGHT)),
    ('attach', '📎', 'Файл', path('M9 12 L15 6 A3 3 0 0 1 19 10 L10 19 A5 5 0 0 1 3 12 L12 3 A3 3 0 0 1 16 3 M7 14 L14 7',stroke=BLUE)),
    ('back', '⬅️', 'Назад', path('M20 5 L10 12 L20 19 Z',fill=BLUE,stroke='none')+path('M11 5 L1 12 L11 19 Z',fill=LIGHT,stroke='none')),
    ('next', '➡️', 'Вперёд', path('M4 5 L14 12 L4 19 Z',fill=BLUE,stroke='none')+path('M13 5 L23 12 L13 19 Z',fill=LIGHT,stroke='none')),
    ('home', '🏠', 'Главная', path('M2 10 L12 2 L22 10 M5 8 V21 H19 V8')+path('M9 21 V14 H15 V21',stroke=LIGHT)),
    ('menu', '🎛️', 'Меню', rect(2.5,4,19,16,2.5)+circle(8,10,2.5,stroke=LIGHT)+path('M14 8 H18 M14 11 H18 M6 16 H10 M14 16 H18',stroke=LIGHT)),
    ('cancel', '❌', 'Отмена', circle(12,12,9)+path('M8 8 L16 16 M16 8 L8 16',stroke=LIGHT)),
    ('check', '☑️', 'Готово', circle(12,12,9)+path('M7 12 L10.5 15.5 L17 8.5',stroke=LIGHT)),
    ('pin', '📌', 'Закрепить', path('M8 2 H16 L15 10 L19 14 H5 L9 10 Z M12 14 V22')+path('M8 2 H16',stroke=LIGHT)),
    ('history', '🕘', 'История', path('M4 7 A9 9 0 1 1 3 15 M4 2 V7 H9')+path('M12 7 V12 L16 15',stroke=LIGHT)),
    ('retry', '🔄', 'Повторить', path('M20 9 A8.5 8.5 0 0 0 5 5 L2 8 M2 3 V8 H7 M4 15 A8.5 8.5 0 0 0 19 19 L22 16 M22 21 V16 H17')),
    ('chat', '💬', 'Разговор', path('M4 3 H20 Q22 3 22 5 V15 Q22 17 20 17 H9 L3 22 V17 H4 Q2 17 2 15 V5 Q2 3 4 3 Z')+path('M7 9 V12 M12 7 V13 M17 9 V12',stroke=LIGHT)),
    ('access', '🔒', 'Доступ', rect(4,10,16,11,2)+path('M7 10 V7 A5 5 0 0 1 17 7 V10')+circle(12,15,1,fill=LIGHT,stroke='none')+path('M12 16 V18',stroke=LIGHT)),
    ('model', '🧠', 'Модель', rect(6,6,12,12,2)+path('M9 2 V6 M15 2 V6 M9 18 V22 M15 18 V22 M2 9 H6 M2 15 H6 M18 9 H22 M18 15 H22')+path('M9 13 V11 M12 10 V14 M15 12 V13',stroke=LIGHT)),
    ('waveform', '🎵', 'Аудио', path('M3 10 V14 M7.5 6 V18 M12 2.5 V21.5 M16.5 6 V18 M21 10 V14')),
    ('move', '↪️', 'Перенос', path('M3 19 V10 Q3 6 7 6 H20 M15 1 L20 6 L15 11')+rect(10,13,11,8,1.5,stroke=LIGHT)),
    ('spark', '✨', 'Обработка', path('M12 2 L14.5 9.5 L22 12 L14.5 14.5 L12 22 L9.5 14.5 L2 12 L9.5 9.5 Z')+circle(20,4,1.5,fill=LIGHT,stroke='none')),
    ('toggle', '🔘', 'Кнопки', rect(2,6,20,12,6)+circle(15,12,3.5,fill=LIGHT,stroke='none')),
    ('undo', '↩️', 'Отменить правку', path('M9 2 L3 8 L9 14 M3 8 H15 A6 6 0 0 1 15 20 H10',stroke=BLUE)),
    ('play', '▶️', 'Слушать', path('M6 3 L21 12 L6 21 Z',fill=BLUE,stroke='none')),
    ('stop', '⏹️', 'Остановить', rect(4,4,16,16,3,fill=BLUE,stroke='none')),
    ('warning', '⚠️', 'Внимание', path('M12 2 L23 21 H1 Z')+path('M12 8 V13',stroke=LIGHT)+circle(12,17,1,fill=LIGHT,stroke='none')),
    ('record', '🔴', 'Запись', circle(12,12,9)+circle(12,12,5,fill=LIGHT,stroke='none')),
    ('menu_list', '🎛️', 'Меню · список', circle(4,5,1.5,fill=LIGHT,stroke='none')+circle(4,12,1.5,fill=LIGHT,stroke='none')+circle(4,19,1.5,fill=LIGHT,stroke='none')+path('M9 5 H21 M9 12 H18 M9 19 H21',stroke=BLUE)),
    ('toggle_off', '🔘', 'Кнопки выкл', rect(2,6,20,12,6)+circle(9,12,3.5,fill=LIGHT,stroke='none')),
    ('folder_settings', '📁', 'Опции папки', folder_settings_icon()),
]


def render_svg(svg, destination):
    """Use the desktop's librsvg/Cairo without adding a bot runtime dependency."""
    def lib(name, functions):
        result = ctypes.CDLL(name)
        for fn, args, ret in functions:
            getattr(result, fn).argtypes = args
            getattr(result, fn).restype = ret
        return result
    pointer, integer, double = ctypes.c_void_p, ctypes.c_int, ctypes.c_double
    cairo_lib = lib('libcairo.so.2', [
        ('cairo_image_surface_create', [integer, integer, integer], pointer),
        ('cairo_create', [pointer], pointer), ('cairo_scale', [pointer,double,double], None),
        ('cairo_surface_write_to_png', [pointer,ctypes.c_char_p], integer),
        ('cairo_destroy', [pointer], None), ('cairo_surface_destroy', [pointer], None)])
    rsvg = lib('librsvg-2.so.2', [
        ('rsvg_handle_new_from_data', [ctypes.c_char_p,ctypes.c_size_t,pointer], pointer),
        ('rsvg_handle_render_cairo', [pointer,pointer], integer)])
    gobject = lib('libgobject-2.0.so.0', [('g_object_unref', [pointer], None)])
    data=svg.encode()
    handle=rsvg.rsvg_handle_new_from_data(data,len(data),None)
    if not handle:
        raise ValueError('Invalid SVG')
    surface=cairo_lib.cairo_image_surface_create(0,400,400)
    context=cairo_lib.cairo_create(surface)
    try:
        cairo_lib.cairo_scale(context,4,4)
        if not rsvg.rsvg_handle_render_cairo(handle,context):
            raise ValueError('SVG render failed')
        if cairo_lib.cairo_surface_write_to_png(surface,str(destination).encode()):
            raise OSError('PNG write failed')
    finally:
        cairo_lib.cairo_destroy(context)
        cairo_lib.cairo_surface_destroy(surface)
        gobject.g_object_unref(handle)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {'title': 'Audio Notes · Blue Tape', 'palette': [BLUE,LIGHT,DARK], 'size': [100,100], 'icons': []}
    for name, emoji, label, body in icons:
        svg = icon(body)
        (OUT/f'{name}.svg').write_text(svg)
        large = OUT/f'.{name}.large.png'
        render_svg(svg, large)
        with Image.open(large) as src:
            png=src.resize((100,100),Image.Resampling.LANCZOS)
            png.save(OUT/f'{name}.png', optimize=True)
        large.unlink()
        manifest['icons'].append({'name':name,'emoji':emoji,'label':label})
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    font_path='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    title_font=ImageFont.truetype(font_path,34)
    label_font=ImageFont.truetype(font_path,15)
    small_font=ImageFont.truetype(font_path,16)
    sheet=Image.new('RGB',(1120, 120 + ((len(icons) + 7) // 8) * 140),'#10151E')
    draw=ImageDraw.Draw(sheet)
    draw.text((36,24),'AUDIO NOTES / BLUE TAPE',font=title_font,fill='#E6EDF7')
    draw.text((38,72),f'{len(icons)} минималистичных ретро-аудио эмодзи · ярко-синий · прозрачный фон',font=small_font,fill='#92A4BC')
    for i,(name,emoji,label,_) in enumerate(icons):
        x=35+(i%8)*134
        y=120+(i//8)*140
        draw.rounded_rectangle((x,y,x+118,y+125),radius=14,fill='#182230')
        with Image.open(OUT/f'{name}.png') as src:
            sheet.paste(src,(x+9,y+5),src)
        draw.text((x+59,y+108),label,font=label_font,fill='#C4D5EB',anchor='mm')
    sheet.save(OUT/'preview.png',optimize=True)
    print(f'{len(icons)} SVG + PNG assets in {OUT}')


if __name__=='__main__':
    main()
