"""
shorts.py — два вертикальных ролика из уже собранного длинного.

    python pipeline/shorts.py jobs/<id>.json

Запускается ПОСЛЕ build.py, на готовом final.mp4. Ничего не рендерит заново
и ничего не тратит: длинный ролик уже собран, здесь из него вырезаются два
куска, поворачиваются в 9:16 и получают шапку с вопросом и субтитры.

Почему из готового файла, а не из плана
---------------------------------------
Можно было бы собрать шортс из тех же кадров отдельным проходом — с
собственным темпом, своим цветокором, своей раскладкой. Соблазн понятный, и
он неверный: ролик собирается пять-десять раз, и каждый такой проход
означал бы полный рендер ещё двух роликов на каждую пересборку. Вырезка из
готового файла стоит секунды и по картинке не отличается — цветокор,
переходы, движение камеры уже в кадре.

Побочная выгода: шортс гарантированно совпадает с роликом, на который
ведёт. Отдельная сборка со своим жребием этого не гарантирует.

Свой вопрос под каждый шортс
----------------------------
Два куска почти всегда режутся из разных историй ролика (разных
script_blocks) — общий вопрос на оба либо не относится ко второму, либо
выдаёт его развязку заранее. open_loop.questions в спецификации — карта
{"номер_блока": "вопрос"}, ключ строкой (JSON не умеет int-ключи). Блок
куска берётся из beat.block; если для него нет записи — используется
общий open_loop.question, как раньше. Поле необязательное целиком.

Почему ffmpeg, а не Remotion или HyperFrames
--------------------------------------------
Оба движка рисуют кадр браузером и снимают его покадрово. На двух шортсах
это 2×55 с × 30 к/с ≈ 3300 кадров через headless Chromium, плюс Node и npm
в репозитории, где сейчас только Python и ffmpeg. Проект уже один раз
упирался в скорость рендера (zoompan 31 с на секунду видео против 2.6 у
scale+crop — см. шапку build.py), и лимит Actions в 6 часов никуда не
делся.

При этом всё, что здесь нужно, ffmpeg делает нативно: crop+scale для 9:16,
libass для шапки и субтитров одним файлом — перенос строк, центрирование
многострочного текста, чёрные плашки под строками (BorderStyle=3) и даже
анимация вступления (\move, \t) он делает сам, без внешних инструментов.

Remotion имело бы смысл, если бы понадобилась настоящая моушн-графика:
3D-текст, сложные морфы, частицы, связанные с содержанием. Тогда это
отдельный шаг и отдельный разговор про время сборки.

Откуда берутся тайм-коды слов
-----------------------------
Из marks.json — границы предложений, посчитанные из посимвольного
выравнивания ElevenLabs. Они ИЗМЕРЕНЫ, а не угаданы, и субтитр идёт по
ним.

Субтитры кусками фразы, а не по слову
-------------------------------------
Пословная подача была первой и оказалась неверной: слово держится десятые
доли секунды, а его тайм-код внутри предложения не измерен, а посчитан
пропорционально длине слова. На быстрой речи подпись заметно отстаёт от
голоса — это увидели на готовом шортсе. Кусок в одну-две строки живёт
полторы-две секунды, и та же ошибка на нём уже не читается.

Шапка с вопросом — два оформления на жребии
--------------------------------------------
Одна полупрозрачная скруглённая панель под ВЕСЬ вопрос (не коробка на
каждую строку) рисуется самим libass в режиме векторного рисования
(\p1) — отдельного прохода ffmpeg или overlay-слоя не нужно. Вид —
"glass" (тёмное стекло, тонкий светлый кант) или "soft" (без канта,
только мягкое размытое пятно) — выбирается жребием по id ролика своей
лентой случайных чисел (смещение +307, как у подложки +101 и эффектов
+211): правка оформления шапки не должна сдвигать цветокор, переходы
и движения камеры уже собранных роликов. Вид один на оба шортса
эпизода — разводятся РОЛИКИ, а не куски одного ролика. Подробности —
у STYLE_PARAMS и header_layout().

Вступление шортса — крупный вопрос по центру кадра, который через
0.9-1.55 с уменьшается и уезжает в свою постоянную позицию сверху
(INTRO_HOLD, INTRO_SHRINK). Анимацию считает libass прямо внутри ASS-
файла — \\move двигает позицию, \\t уменьшает кегль — в одном событии,
без отдельного видео-прохода: overlay поверх видео или покадровый
рендер (Remotion/HyperFrames) понадобились бы только для
морфов и частиц, а линейное уменьшение с переездом — ровно то, что
libass умеет из коробки, за секунды, а не минуты.
"""

import json
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job
from style import seed_from

from editorial import beats as beats_mod

ROOT = Path(__file__).parent.parent

W, H = 1080, 1920          # вертикальный кадр
FPS = 30

# Длина шортса. YouTube считает шортсом всё до 60 секунд включительно, но
# впритык к границе упираться незачем: кусок режется по границам
# предложений, и последнее предложение может оказаться длинным.
SHORT_MIN = 24.0
SHORT_MAX = 52.0

# Шапка с вопросом: плашки жмутся к тексту (см. header_layout), но
# начинаются с этого отступа от края кадра.
BOX_MARGIN = 36

# Субтитры кусками фразы, а не по одному слову.
SUB_MAX_CHARS = 20
SUB_MAX_LINES = 2
SUB_SIZE = 70
SUB_MARGIN = 60            # поля стиля SUB, они же предел ширины строки

# ГДЕ СТОЯТ СУБТИТРЫ — 0.66 высоты, а не 0.74. Ниже нельзя: в длинный
# ролик ВЖЖЕНЫ плашки с числами (editorial/textcard.py, PLACES), они
# занимают полосу 0.760-0.816 высоты кадра, и доли по высоте при crop+scale
# в 9:16 сохраняются. На 0.74 субтитр наезжал на плашку прямо в кадре —
# поймано на готовом шортсе ff-ep06, где «Under the treasure trove law
# that» легло поверх «1996».
#
# Выше 0.62 тоже не стоит: низ кадра у Shorts закрывает интерфейс YouTube
# (заголовок и кнопки), а слишком высокий субтитр лезет к шапке с вопросом.
SUB_Y = 0.66

FONT = "Liberation Sans"
# Единый фирменный шрифт канала — и в шапке, и в субтитрах, один и тот же
# в каждой загрузке. Выбран не за оригинальность, а за надёжность: качать
# веб-шрифт по URL в конвейере не с чего (нет подтверждённой ссылки и
# нет смысла гадать домен), а Liberation Sans Bold уже был в этом списке
# файлов — нужен для измерения ширины строки, см. fit_size() — и, значит,
# уже проверено, что он есть на раннере GitHub Actions без новых
# зависимостей. Порядок важен: fit_size() меряет ПЕРВЫМ существующим
# файлом, он должен быть тем же шрифтом, что рисует ass.
FONT_FILES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

# ─────────────────── ОФОРМЛЕНИЕ ШАПКИ ───────────────────
#
# Два вида панели на жребии (см. header_style_for) — обе полупрозрачные и
# скруглённые, отличаются только тем, есть ли у панели кант и насколько
# она держит форму. Раньше здесь были плотные чёрные плашки-коробки на
# каждую строку — по отзыву выглядело дёшево; отрисовано и сравнено на
# настоящем кадре канала в docs/shorts-header, выбраны эти два варианта
# из четырёх показанных.
#
# HEADER_YELLOW_ASS общий на оба стиля и на вступление (см. ниже) — цвет
# шапки узнаётся с первого кадра независимо от того, какой из двух видов
# панели выпал.
HEADER_YELLOW_ASS = "&H0000D4FF"   # #FFD400, ASS-порядок &HAABBGGRR

# Параметры панели — БЕЗ ведущего &H и без & на конце: это голые байты
# ASS-тэгов \1c/\1a/\3c/\3a, собираются в build_ass() форматированием.
STYLE_PARAMS = {
    # A — стекло: тёмная полупрозрачная панель с тонким светлым кантом
    # и мягкой тенью. Текст не нуждается в собственной обводке — контраст
    # даёт панель.
    "glass": dict(
        pad_x=46, pad_y=34, radius=30,
        panel_fill="141110", panel_alpha="4A",
        border_colour="E8F4FF", border_alpha="B4", bord=2, blur="0.6",
        shadow_alpha="96", shadow_blur=14, shadow_dy=6,
        text_outline=0,
    ),
    # C — без рамки: канта нет вовсе, только размытое тёмное пятно под
    # буквами. Тексту здесь нужна собственная тонкая обводка — без канта
    # панели больше не за что зацепиться взглядом.
    "soft": dict(
        pad_x=60, pad_y=44, radius=60,
        panel_fill="0A0806", panel_alpha="55",
        border_colour=None, border_alpha=None, bord=0, blur="26",
        shadow_alpha=None, shadow_blur=0, shadow_dy=0,
        text_outline=3,
    ),
}
HEADER_STYLES = tuple(STYLE_PARAMS)

# Вступление: крупный вопрос по центру кадра — держится, потом уезжает и
# уменьшается в постоянную шапку сверху. 0.9 с держим — время, за которое
# читается короткая фраза; 0.65 с едет и сжимается. Длиннее держать —
# тратить темп открытия шортса впустую, короче — вопрос крупным планом не
# успеет прочитаться.
INTRO_HOLD = 0.9
INTRO_SHRINK = 0.65


def log(*a):
    print(*a, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ─────────────────────── ВЫБОР КУСКОВ ───────────────────────

def rank_beats(beats):
    """
    Доли в порядке пригодности для шортса.

    Развязка с числами — лучший кандидат: в ней звучит сумма, ради которой
    ролик и смотрят («ушёл за восемьдесят девять тысяч»). Это тот самый
    признак, по которому beats.py её и опознал, так что считать заново
    нечего — берём features["num"].

    Нагнетание идёт вторым сортом: там есть напряжение, но нет выплаты.
    Всё остальное для шортса не годится — завязка без развязки это реклама
    ролика, а не самостоятельный кусок.
    """
    weight = {"revelation": 1.0, "escalation": 0.55}
    out = []
    for b in beats:
        w = weight.get(b.kind, 0.0)
        if not w:
            continue
        f = b.features or {}
        # числа решают, пауза и медленная речь добавляют: диктор
        # притормаживает ровно там, где говорит главное
        score = w * (1.0 + 2.2 * f.get("num", 0.0)
                     + 0.6 * f.get("pause", 0.0)
                     + 0.5 * max(0.0, 1.0 - f.get("rate", 1.0)))
        out.append((score, b))
    out.sort(key=lambda x: -x[0])
    return out


def window_for(beat, marks, total, min_s=SHORT_MIN, max_s=SHORT_MAX):
    """
    Границы куска вокруг доли, ПО ГРАНИЦАМ ПРЕДЛОЖЕНИЙ.

    Резать по секундомеру нельзя: шортс, начинающийся с середины слова,
    выглядит обрезком, а не роликом. Поэтому начало и конец всегда
    совпадают с границами предложений из marks.json.

    Кусок расширяется НАЗАД от развязки, а не вперёд: развязка должна
    прозвучать в шортсе целиком и ближе к концу, а перед ней нужен разгон,
    иначе сумма падает на зрителя без контекста и ничего не значит.
    """
    if not marks:
        return None
    idx = [i for i, m in enumerate(marks)
           if m["start"] >= beat.start - 0.01 and m["end"] <= beat.end + 0.01]
    if not idx:
        idx = [min(range(len(marks)),
                   key=lambda i: abs(marks[i]["start"] - beat.start))]
    lo, hi = idx[0], idx[-1]

    def span():
        return marks[hi]["end"] - marks[lo]["start"]

    # сначала добираем разгон назад
    while span() < min_s and lo > 0:
        lo -= 1
    # если всё равно коротко — тянем вперёд
    while span() < min_s and hi < len(marks) - 1:
        hi += 1
    # если переросло — режем спереди, сохраняя развязку в конце
    while span() > max_s and lo < hi:
        lo += 1
    while span() > max_s and hi > lo:
        hi -= 1

    t0 = max(0.0, marks[lo]["start"] - 0.15)
    t1 = min(total, marks[hi]["end"] + 0.35)
    if t1 - t0 < 8.0:
        return None
    return t0, t1, lo, hi


def pick_windows(beats, marks, total, want=2):
    """
    Два куска, которые не пересекаются и не стоят вплотную.

    Два шортса из соседних абзацев — это один и тот же шортс дважды: то же
    место ролика, тот же материал в кадре, тот же смысл. Поэтому второй
    берётся только если он отстоит от первого.

    Первый проход берёт кандидатов ТОЛЬКО из ещё не занятых script_blocks:
    лучшая развязка по числам не обязана распределяться по одной на блок
    (число может оказаться там, где чисел просто больше), и без этого
    условия оба куска на ff-ep06 достались одному и тому же блоку — с
    одним и тем же вопросом в шапке у обоих, что и обесценивает вопрос.
    Второй проход снимает ограничение по блоку и просто добирает
    недостающее — на коротком ролике с одной сильной развязкой второго
    блока может не быть вовсе, и это не повод остаться без второго шортса.
    """
    def take(candidates, out, require_new_block):
        for _, b in candidates:
            if len(out) >= want:
                break
            if require_new_block and any(b.block == o[4].block for o in out):
                continue
            w = window_for(b, marks, total)
            if not w:
                continue
            t0, t1 = w[0], w[1]
            if any(not (t1 <= o0 or t0 >= o1) for o0, o1, *_ in out):
                continue        # пересекается с уже взятым
            if any(min(abs(t0 - o1), abs(o0 - t1)) < 12.0 for o0, o1, *_ in out):
                continue        # стоит вплотную
            out.append((t0, t1, w[2], w[3], b))
        return out

    ranked = rank_beats(beats)
    out = take(ranked, [], require_new_block=True)
    out = take(ranked, out, require_new_block=False)
    return out


# ─────────────────────── СУБТИТРЫ ───────────────────────

def split_chunks(text: str, per_line: int = SUB_MAX_CHARS,
                 max_lines: int = SUB_MAX_LINES):
    """Режет предложение на куски не длиннее max_lines строк, не рвя слов."""
    out, cur = [], []
    for w in text.split():
        probe = cur + [w]
        if len(wrap(" ".join(probe), per_line)) > max_lines and cur:
            out.append(" ".join(cur))
            cur = [w]
        else:
            cur = probe
    if cur:
        out.append(" ".join(cur))
    return out


def lines_with_times(marks, lo, hi, t0):
    """
    Субтитры КУСКАМИ ФРАЗЫ, а не по одному слову.

    Пословная выдача выглядит бодрее, но читается плохо, и это заметили на
    готовом шортсе: слово держится десятые доли секунды, а его тайм-код не
    измерен, а посчитан пропорционально длине внутри предложения. На
    быстрой речи накопленная ошибка видна — подпись отстаёт от голоса.

    Кусок в одну-две строки живёт полторы-две секунды, и та же ошибка в
    десятые доли на нём уже не читается. Границы предложений при этом
    берутся из marks как есть — они ИЗМЕРЕНЫ выравниванием ElevenLabs, а
    не угаданы; делится только само предложение, если оно длиннее двух
    строк, и делится по тому же принципу пропорционально символам.

    Нижней границы длительности здесь нет намеренно: растянуть короткий
    кусок значило бы залезть на следующий и разъехаться со звуком, а
    короткое предложение («Not gold.») и так висит около секунды.
    """
    out = []
    for m in marks[lo:hi + 1]:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        chunks = split_chunks(text)
        if not chunks:
            continue
        dur = max(m["end"] - m["start"], 0.05)
        total_chars = sum(len(c) for c in chunks) or 1
        cur = m["start"] - t0
        for c in chunks:
            wd = dur * (len(c) / total_chars)
            out.append((max(0.0, cur), max(0.0, cur) + wd, c))
            cur += wd
    # подрезаем нахлёсты, чтобы два куска не висели разом
    for i in range(len(out) - 1):
        s, e, c = out[i]
        out[i] = (s, min(e, out[i + 1][0]), c)
    return [(s, e, c) for s, e, c in out if e > s]


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def fit_size(lines, max_w: int, size: int, floor: int = 30) -> int:
    """
    Уменьшает кегль, пока самая длинная строка не влезет в max_w.

    Число символов в строке — плохая мера ширины: «illinois» и «MMMMMMMM»
    одной длины занимают втрое разную ширину, а одно длинное слово (в этом
    жанре сплошь «archaeologists» и «authentication») переносом не режется
    вовсе и вылезает за кадр целиком. Первый прогон именно так и выехал
    буквами за края кадра.

    Меряется настоящим шрифтом. Если PIL или файл шрифта недоступны —
    возвращается исходный кегль: подпись чуть шире кадра лучше, чем
    упавшая сборка шортса.
    """
    if not lines:
        return size
    try:
        from PIL import ImageFont
    except ImportError:
        return size
    path = next((p for p in FONT_FILES if Path(p).exists()), None)
    if not path:
        return size
    while size > floor:
        f = ImageFont.truetype(path, size)
        if max(f.getlength(l) for l in lines) <= max_w:
            break
        size -= 2
    return size


def wrap(text: str, per_line: int):
    """Простой перенос по словам. drawtext и ASS сами не переносят."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > per_line:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def fallback_question(job) -> str:
    """
    Вопрос для шапки, когда в спецификации нет open_loop вовсе.

    ШАПКА ОБЯЗАНА БЫТЬ В КАЖДОМ ШОРТСЕ. Раньше отсутствие open_loop
    молча давало шортс без единой надписи сверху — а заодно и без
    вступления, потому что крупный вопрос и постоянная шапка это одно
    и то же событие ASS. Ровно так вышел ff-ep08: спецификацию прислали
    без open_loop, прогон честно написал предупреждение в лог, и оба
    шортса уехали к человеку голыми. Предупреждение в логе — не защита:
    его никто не читает на успешном прогоне.

    Заголовок ролика — не идеальная замена (он утверждение, а не
    вопрос), но он ХУК канала, ради которого ролик открывают, и в шапке
    работает. Дальше по убыванию: текст с обложки, имя главы. Пустой
    ответ здесь означал бы, что в спецификации нет ни заголовка, ни
    глав — такую в конвейер не пускает smoke.py.
    """
    y = job.get("youtube") or {}
    title = (y.get("title") or "").strip()
    if title:
        return title
    overlay = ((job.get("_превью_промпт") or {}).get("overlay_text") or "").strip()
    if overlay:
        return overlay
    chapters = y.get("chapters") or []
    return str(chapters[0]).strip() if chapters else ""


def header_style_for(job) -> str:
    """Оформление панели — жребием по id ролика, своей лентой (см. HEADER_STYLES)."""
    forced = (job.get("open_loop") or {}).get("header_style")
    if forced in HEADER_STYLES:
        return forced
    return random.Random(seed_from(job["id"]) + 307).choice(HEADER_STYLES)


def measure_width(lines, size: int) -> float:
    """Ширина самой широкой строки настоящим шрифтом — для размера панели."""
    try:
        from PIL import ImageFont
    except ImportError:
        return max(len(l) for l in lines) * size * 0.56
    path = next((p for p in FONT_FILES if Path(p).exists()), None)
    if not path:
        return max(len(l) for l in lines) * size * 0.56
    f = ImageFont.truetype(path, size)
    return max(f.getlength(l) for l in lines)


def rrect(w: float, h: float, r: float) -> str:
    """
    Скруглённый прямоугольник в режиме рисования ASS (\\p1), угол (0,0).

    ffmpeg drawbox скруглять углы не умеет вовсе — отсюда раньше и были
    плотные прямоугольные коробки. libass же рисует произвольные фигуры
    через move/line/bezier, и скруглённая панель — четыре прямые стороны
    плюс четыре четвертные безье в углах, без единого внешнего файла.
    """
    w, h, r = round(w), round(h), round(r)
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} "
            f"l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} "
            f"l 0 {r} b 0 0 0 0 {r} 0")


def header_layout(question: str, style: str) -> dict:
    """
    Геометрия шапки: одна полупрозрачная скруглённая панель под весь
    вопрос + параметры вступления — крупный вопрос по центру кадра,
    уезжающий и уменьшающийся в свою постоянную позицию за
    INTRO_HOLD+INTRO_SHRINK секунд.

    Панель — НЕ автобокс (как раньше BorderStyle=3), а рисуется вручную
    через rrect(), поэтому размер меряется здесь самостоятельно, той же
    PIL-логикой, что и fit_size().

    Пустой вопрос — пустой макет целиком, ни панели, ни вступления: без
    него десять секунд провисела бы пустая анимация без единой буквы —
    это уже ловили на готовом шортсе с прежней белой коробкой.
    """
    lines = wrap(question.strip(), 24) if question else []
    if not lines:
        return dict(lines=[], size=0, big_size=0, cx=W // 2, big_cy=0,
                    block_cy=0, panel=None, pct=100, style=style)

    sp = STYLE_PARAMS[style]
    size = 66 if len(lines) <= 2 else (56 if len(lines) == 3 else 46)
    size = fit_size(lines, W - 2 * BOX_MARGIN - 2 * sp["pad_x"], size, floor=34)

    tw = measure_width(lines, size)
    line_h = size * 1.30
    pw = tw + 2 * sp["pad_x"]
    ph = line_h * len(lines) + 2 * sp["pad_y"]
    x = (W - pw) / 2
    y = BOX_MARGIN
    block_cy = y + ph / 2

    # Крупный вопрос вступления — минимум на 14pt больше итогового кегля,
    # иначе на длинном вопросе fit_size ужмёт его почти до того же
    # размера, и уменьшение на экране не будет заметно вовсе.
    big_size = fit_size(lines, W - 2 * BOX_MARGIN, int(size * 1.9), floor=size + 14)
    pct = round(size / big_size * 100)

    return dict(lines=lines, size=size, big_size=big_size, cx=W // 2,
                big_cy=H * 0.46, block_cy=block_cy, pct=pct, style=style,
                panel=dict(x=x, y=y, w=pw, h=ph, r=sp["radius"]))


def build_ass(subs, layout: dict, out: Path):
    """
    Файл субтитров: вступление с крупным вопросом, полупрозрачная
    скруглённая панель под тем же вопросом на весь шортс, и субтитры
    кусками фразы.

    Всё — включая панель и анимацию вступления — рисует один и тот же
    ass-фильтр ffmpeg, без отдельного прохода или overlay-слоя поверх
    видео. Панель — векторная фигура (\\p1, см. rrect()) с тенью и
    кантом через \\1a/\\3c/\\3a/\\blur, библиотека рисует и заливку, и
    полупрозрачность, и скругление сама. Для вступления — стандартные
    для ASS \\move (позиция едет от t1 до t2) и \\t (кегль через
    \\fscx/\\fscy едет туда же) внутри ОДНОГО события: видео-движок
    вроде Remotion нужен был бы для морфов и частиц, а настоящее
    уменьшение текста с переездом — то, что ASS/libass умеет из коробки.

    Вступление ПЕРЕКРЫВАЕТ по времени субтитры снизу — это нормально:
    экраны разные, крупный вопрос сверху и по центру первые секунды,
    реплика внизу — с самого начала куска, как обычно.
    """
    q_size = layout["size"] or 1
    sp = STYLE_PARAMS.get(layout.get("style"), STYLE_PARAMS["glass"])
    # Кегль субтитров — по самой широкой строке ВСЕГО куска, а не по каждой
    # отдельно: прыгающий от реплики к реплике размер читается как брак.
    all_lines = [l for _, _, c in subs for l in wrap(c, SUB_MAX_CHARS)]
    sub_size = fit_size(all_lines, W - 2 * SUB_MARGIN, SUB_SIZE, floor=34)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: QBIG,{FONT},{layout['big_size']},{HEADER_YELLOW_ASS},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,7,4,5,40,40,40,1
Style: Q,{FONT},{q_size},{HEADER_YELLOW_ASS},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{sp['text_outline']},0,5,40,40,40,1
Style: PANEL,{FONT},60,&H00FFFFFF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: SUB,{FONT},{sub_size},&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,5,{SUB_MARGIN},{SUB_MARGIN},60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    if layout["lines"]:
        hold_ms = round(INTRO_HOLD * 1000)
        intro_ms = round((INTRO_HOLD + INTRO_SHRINK) * 1000)
        text = "\\N".join(ass_escape(l) for l in layout["lines"])
        rows.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(intro_ms / 1000)},QBIG,,0,0,0,,"
            f"{{\\an5\\move({layout['cx']},{layout['big_cy']:.0f},"
            f"{layout['cx']},{layout['block_cy']:.0f},{hold_ms},{intro_ms})"
            f"\\t({hold_ms},{intro_ms},\\fscx{layout['pct']}\\fscy{layout['pct']})"
            f"\\fad(150,150)}}" + text)

        p = layout["panel"]
        path = rrect(p["w"], p["h"], p["r"])
        start = ass_time(intro_ms / 1000)
        if sp["shadow_alpha"]:
            rows.append(
                f"Dialogue: 0,{start},{ass_time(99999)},PANEL,,0,0,0,,"
                f"{{\\pos({p['x']:.0f},{p['y'] + sp['shadow_dy']:.0f})"
                f"\\1c&H000000&\\1a&H{sp['shadow_alpha']}&\\bord0"
                f"\\blur{sp['shadow_blur']}\\p1}}{path}{{\\p0}}")
        border = ""
        if sp["border_colour"]:
            border = (f"\\bord{sp['bord']}\\3c&H{sp['border_colour']}&"
                      f"\\3a&H{sp['border_alpha']}&")
        rows.append(
            f"Dialogue: 1,{start},{ass_time(99999)},PANEL,,0,0,0,,"
            f"{{\\pos({p['x']:.0f},{p['y']:.0f})\\1c&H{sp['panel_fill']}&"
            f"\\1a&H{sp['panel_alpha']}&{border}\\blur{sp['blur']}\\p1}}"
            f"{path}{{\\p0}}")
        rows.append(
            f"Dialogue: 2,{start},{ass_time(99999)},Q,,0,0,0,,"
            f"{{\\an5\\pos({W // 2},{layout['block_cy']:.0f})}}" + text)
    for s, e, c in subs:
        rows.append(
            f"Dialogue: 1,{ass_time(s)},{ass_time(e)},SUB,,0,0,0,,"
            f"{{\\pos({W//2},{int(H*SUB_Y)})}}"
            + "\\N".join(ass_escape(l) for l in wrap(c, SUB_MAX_CHARS)))
    out.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return out


# ─────────────────────── РЕНДЕР ───────────────────────

def render_short(src: Path, t0: float, t1: float, ass: Path, dst: Path,
                 crf: int = 20):
    """
    Вырезает кусок, разворачивает в 9:16 и накладывает шапку и субтитры.

    Кадрирование центральное: 1920x1080 -> 608x1080 -> 1080x1920. Апскейл
    в 1.78 раза заметен только на мелкой фактуре, а исходники готовятся на
    холсте 3000 пикселей (PREP_W в render.py) — то есть детали в кадре
    достаточно, теряется она уже при сборке длинного ролика, а не здесь.

    -ss ДО -i, а не после: так ffmpeg перематывает по ключевым кадрам и не
    декодирует всё от начала файла. На получасовом ролике разница — секунды
    против минут.

    Плашки шапки больше не рисует ffmpeg (drawbox) — их кладёт сам libass
    через ass-фильтр (BorderStyle=3, см. build_ass), поэтому здесь только
    кадрирование, масштаб и один слой субтитров.
    """
    # 1080 * 9/16 = 607.5; libx264 требует чётные размеры, берём 608
    crop_w = 608
    vf = (
        f"crop={crop_w}:1080:(iw-{crop_w})/2:0,"
        f"scale={W}:{H}:flags=lanczos,setsar=1,"
        f"ass={ass.as_posix()}"
    )
    run(["ffmpeg", "-v", "error", "-y",
         "-ss", f"{t0:.3f}", "-t", f"{t1-t0:.3f}", "-i", str(src),
         "-vf", vf, "-r", str(FPS),
         "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         str(dst)])
    return dst


def duration_of(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ─────────────────────── ТАЙМ-КОДЫ ГОТОВОГО РОЛИКА ───────────────────────

def load_final_marks(job, assets: Path, out: Path):
    """
    Тайм-коды на шкале ГОТОВОГО final.mp4, а не кэшированной начитки.

    Если build.py вставлял паузы перед главами, final.mp4 длиннее начитки
    на их сумму, и резать куски по assets/marks.json значило бы промахи-
    ваться на накопленную паузу — к концу получаса это уже секунд десять.

    Три источника по убыванию надёжности:

    1. out/marks_final.json — это ровно то, что писал build.py рядом с
       роликом. Берём как есть.
    2. Нет файла (стадия post/shorts на свежем раннере — в релизе лежит
       только final.mp4): ПЕРЕСЧИТЫВАЕМ сдвиг теми же функциями build.py и
       из тех же входов (сырые marks, total_audio из state.json, карта
       глав из спецификации). Разбор долей детерминирован, поэтому границы
       получаются те же, что были при сборке.
    3. Нет и state.json — отдаём сырые marks и говорим об этом вслух:
       ролик, собранный до появления пауз, так и режется, а новый лучше
       порезать чуть мимо, чем не порезать вовсе.
    """
    final_marks = out / "marks_final.json"
    if final_marks.exists():
        return json.loads(final_marks.read_text(encoding="utf-8"))

    raw = json.loads((assets / "marks.json").read_text(encoding="utf-8"))
    state = assets / "state.json"
    if not state.exists():
        log("  ! нет ни marks_final.json, ни state.json — режу по сырым "
            "тайм-кодам; если в ролике есть паузы глав, куски уедут")
        return raw

    import build as build_mod
    total_audio = json.loads(state.read_text(encoding="utf-8"))["total_audio"]
    beats = beats_mod.analyze(raw, job["script_blocks"], total_audio)
    bounds = build_mod.chapter_boundaries(job, beats, total_audio)
    if not bounds:
        return raw
    log(f"  marks_final.json нет — пересчитал сдвиг сам: {len(bounds)} пауз "
        f"по {build_mod.CHAPTER_PAUSE:.1f} с")
    return build_mod.shift_marks(raw, bounds, build_mod.CHAPTER_PAUSE)


# ─────────────────────── ГЛАВНОЕ ───────────────────────

def main(job_path, want=2):
    job = load_job(job_path)
    work = ROOT / "work" / job["id"]
    assets, out = work / "assets", work / "out"
    final = out / "final.mp4"
    if not final.exists():
        raise SystemExit(f"нет {final} — сначала собери ролик (build.py)")

    marks = load_final_marks(job, assets, out)
    total = duration_of(final)
    if not marks or total <= 0:
        raise SystemExit("нет тайм-кодов или пустой ролик")

    loop = job.get("open_loop") or {}
    default_question = (loop.get("question") or "").strip()
    per_block = {str(k): v.strip() for k, v in (loop.get("questions") or {}).items()
                if v and v.strip()}
    if not default_question and not per_block:
        # НЕ оставляем шортс без шапки. Раньше здесь была только строчка в
        # лог — и ff-ep08 уехал с двумя голыми шортсами: ни вопроса сверху,
        # ни вступления (это одно событие ASS). Предупреждение на успешном
        # прогоне никто не читает, поэтому берём запасной вопрос.
        default_question = fallback_question(job)
        log(f"  ! в спецификации нет open_loop — шапка взята из заголовка "
            f"ролика: «{default_question}». Свой вопрос под каждый шортс "
            f"задаётся в open_loop.questions и почти всегда лучше")

    log("── разбор сценария на доли")
    bts = beats_mod.analyze(marks, job["script_blocks"], total)
    windows = pick_windows(bts, marks, total, want=want)
    if not windows:
        raise SystemExit(
            "не нашлось ни одной доли, годной для шортса.\n"
            "Шортс режется из развязки или нагнетания — в этом ролике "
            "beats.py таких не нашёл. Смотри строку «долей» в логе сборки.")

    if len(windows) < want:
        # Не ошибка: на коротком ролике двух непересекающихся кусков по 24+
        # секунды просто нет. Но и молчать нельзя — заказывали два.
        log(f"  ! нашлось кусков: {len(windows)} из {want}. Ролик длиной "
            f"{total/60:.1f} мин, а куски не должны пересекаться и стоять "
            f"вплотную — на коротком ролике второго места не остаётся")

    style = header_style_for(job)
    log(f"── оформление шапки: {style}")

    made, questions_used = [], []
    for n, (t0, t1, lo, hi, beat) in enumerate(windows, 1):
        # Свой вопрос под свой блок сценария, а не один на оба шортса: два
        # куска почти всегда режутся из РАЗНЫХ историй ролика (разные
        # script_blocks), и общий вопрос либо не относится ко второму
        # шортсу, либо выдаёт его развязку раньше, чем видео до неё дошло.
        # per_block ключуется строкой номера блока (JSON не умеет int-ключи).
        question = per_block.get(str(beat.block), default_question)
        layout = header_layout(question, style)
        subs = lines_with_times(marks, lo, hi, t0)
        ass = build_ass(subs, layout, out / f"short_{n}.ass")
        dst = out / f"short_{n}.mp4"
        log(f"── шортс {n}: {t0:.1f}–{t1:.1f} с ({t1-t0:.1f} с), "
            f"доля «{beat.kind}», блок {beat.block}, субтитров {len(subs)}, "
            f"вопрос: {question or '(нет)'}")
        render_short(final, t0, t1, ass, dst,
                     crf=int((job.get("style_override") or {}).get("crf", 20)))
        got = duration_of(dst)
        if got > 60.5:
            log(f"  ! {got:.1f} с — длиннее 60, YouTube не примет как Shorts")
        log(f"  {dst.name}: {got:.1f} с, {dst.stat().st_size/1048576:.1f} МБ")
        made.append(dst)
        questions_used.append(question)

    (out / "shorts.json").write_text(json.dumps([
        {"file": p.name, "start": round(w[0], 2), "end": round(w[1], 2),
         "beat": w[4].kind, "block": w[4].block, "question": q}
        for p, w, q in zip(made, windows, questions_used)],
        ensure_ascii=False, indent=1),
        encoding="utf-8")
    log(f"── готово: {len(made)} шортса")
    return made


if __name__ == "__main__":
    main(sys.argv[1], want=int(sys.argv[2]) if len(sys.argv) > 2 else 2)
