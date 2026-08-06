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
drawbox для плашек шапки, libass для субтитров: и перенос строк, и
центрирование многострочного текста он делает сам, одним файлом.

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

Шапка с вопросом — три оформления
---------------------------------
Одна и та же белая плашка в каждой загрузке канала — такая же подпись
конвейера, как один цветокор на все ролики. Вид выбирается жребием по id
ролика своей лентой случайных чисел; внутри одного эпизода он общий на
оба шортса. Подробности — у HEADER_STYLES.
"""

import json
import random
import re
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

# Шапка с вопросом: 20% высоты кадра, как заказано.
BOX_SHARE = 0.20
BOX_MARGIN = 36            # отступ рамки от краёв кадра

# Субтитры кусками фразы, а не по одному слову.
SUB_MAX_CHARS = 20
SUB_MAX_LINES = 2
SUB_SIZE = 70
SUB_MARGIN = 60            # поля стиля SUB, они же предел ширины строки

FONT = "DejaVu Sans"
# Файлы того же шрифта — нужны, чтобы МЕРИТЬ ширину строки перед рендером,
# см. fit_size(). libass берёт шрифт по имени, PIL — только по файлу.
FONT_FILES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

# ─────────────────── ОФОРМЛЕНИЕ ШАПКИ ───────────────────
#
# Три вида вместо одной белой плашки на все ролики канала. Вопрос — то,
# ради чего шортс досматривают, и если он выглядит одинаково в каждой
# загрузке, это такая же подпись конвейера, как один цветокор на всё.
#
# Выбор идёт СВОЕЙ лентой случайных чисел (смещение +307), как у подложки
# (+101) и эффектов (+211): правка оформления шапки не должна сдвигать ни
# цветокор, ни переходы, ни движения камеры уже собранных роликов.
#
# Вид один на оба шортса эпизода: внутри одной загрузки оформление —
# постоянная, разводятся между собой РОЛИКИ, а не куски одного ролика.
HEADER_STYLES = ("paper", "night", "clean")

# Цвета. ASS читает &HAABBGGRR (порядок байт обратный привычному RGB),
# ffmpeg drawbox — 0xRRGGBB@прозрачность. Одни и те же цвета записаны
# дважды в разных порядках именно поэтому, а не по недосмотру.
CREAM_ASS = "&H00D8EAF2"       # #F2EAD8
BROWN_ASS = "&H00161F2A"       # #2A1F16
GOLD_BOX = "0xC9A227"
PAPER_BOX = "0xF2EAD8"
NIGHT_BOX = "0x120E0A"


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


def header_style_for(job) -> str:
    """Оформление шапки — жребием по id ролика, своей лентой (см. HEADER_STYLES)."""
    forced = (job.get("open_loop") or {}).get("header_style")
    if forced in HEADER_STYLES:
        return forced
    return random.Random(seed_from(job["id"]) + 307).choice(HEADER_STYLES)


def header_layout(question: str, style: str) -> dict:
    """
    Геометрия шапки: где плашки, где текст, каким кеглем.

    Считается В ОДНОМ месте, потому что плашки рисует ffmpeg (drawbox), а
    текст — libass, и разъехаться они не должны. Раньше высота плашки
    жила в render_short, а положение текста — в build_ass, и любая правка
    одного требовала помнить про другое.

    Пустой вопрос — пустой макет: ни плашек, ни текста. Пустая белая
    коробка без букв занимает 20% кадра и не сообщает ничего, это уже
    ловили на готовом шортсе.
    """
    lines = wrap(question.strip(), 24) if question else []
    if not lines:
        return dict(lines=[], boxes=[], size=0, cx=W // 2, cy=0,
                    colour=CREAM_ASS, outline=0, shadow=0,
                    outline_colour="&H00000000")

    box_h = int(H * BOX_SHARE)
    size = 66 if len(lines) <= 2 else (56 if len(lines) == 3 else 46)
    # и всё равно меряем: длинное слово в вопросе вылезет за плашку
    size = fit_size(lines, W - 2 * BOX_MARGIN - 60, size, floor=34)
    cy = BOX_MARGIN + box_h // 2
    x, w = BOX_MARGIN, W - 2 * BOX_MARGIN
    # низ текстового блока — от него отбивается золотая линейка
    rule_y = cy + int(len(lines) * size * 1.22) // 2 + 16
    rule_w = min(w - 80, 240)

    if style == "paper":
        # тёплая бумага: тёмные буквы на кремовом, золотая линейка под текстом
        return dict(
            lines=lines, size=size, cx=W // 2, cy=cy,
            colour=BROWN_ASS, outline_colour=BROWN_ASS, outline=0, shadow=0,
            boxes=[(x, BOX_MARGIN, w, box_h, f"{PAPER_BOX}@0.96", "fill"),
                   ((W - rule_w) // 2, rule_y, rule_w, 6, f"{GOLD_BOX}@0.95", "fill")])

    if style == "night":
        # тёмная плашка под цветокор канала, золотой брусок слева
        return dict(
            lines=lines, size=size, cx=W // 2 + 8, cy=cy,
            colour=CREAM_ASS, outline_colour="&H00000000", outline=2, shadow=1,
            boxes=[(x, BOX_MARGIN, w, box_h, f"{NIGHT_BOX}@0.80", "fill"),
                   (x, BOX_MARGIN, 12, box_h, f"{GOLD_BOX}@0.95", "fill")])

    # clean: плашки нет вовсе, буквы прямо на кадре с толстой обводкой
    return dict(
        lines=lines, size=size + 4, cx=W // 2, cy=cy,
        colour=CREAM_ASS, outline_colour="&H00000000", outline=7, shadow=4,
        boxes=[((W - rule_w) // 2, rule_y, rule_w, 7, f"{GOLD_BOX}@0.95", "fill")])


def build_ass(subs, layout: dict, out: Path):
    """
    Файл субтитров: вопрос в шапке на весь кусок + субтитры кусками фразы.

    Оба слоя здесь, а не в drawtext, по одной причине: drawtext не умеет
    ни переносить строки, ни центрировать многострочный текст, и каждый
    кусок в нём — отдельный фильтр в цепочке. libass берёт то же самое
    одним файлом.

    Вопрос ПРОЯВЛЯЕТСЯ за треть секунды (\\fad), а не возникает рывком на
    первом кадре: рывок читается как склейка, наплыв — как приём.
    """
    q_size = layout["size"] or 1
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
Style: Q,{FONT},{q_size},{layout['colour']},{layout['outline_colour']},&H00000000,-1,0,0,0,100,100,0,0,1,{layout['outline']},{layout['shadow']},5,40,40,40,1
Style: SUB,{FONT},{sub_size},&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,5,{SUB_MARGIN},{SUB_MARGIN},60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    if layout["lines"]:
        rows.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(99999)},Q,,0,0,0,,"
            f"{{\\pos({layout['cx']},{layout['cy']})\\fad(320,0)}}"
            + "\\N".join(ass_escape(l) for l in layout["lines"]))
    for s, e, c in subs:
        rows.append(
            f"Dialogue: 1,{ass_time(s)},{ass_time(e)},SUB,,0,0,0,,"
            f"{{\\pos({W//2},{int(H*0.74)})}}"
            + "\\N".join(ass_escape(l) for l in wrap(c, SUB_MAX_CHARS)))
    out.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return out


# ─────────────────────── РЕНДЕР ───────────────────────

def render_short(src: Path, t0: float, t1: float, ass: Path, dst: Path,
                 crf: int = 20, boxes=None):
    """
    Вырезает кусок, разворачивает в 9:16 и накладывает шапку и субтитры.

    Кадрирование центральное: 1920x1080 -> 608x1080 -> 1080x1920. Апскейл
    в 1.78 раза заметен только на мелкой фактуре, а исходники готовятся на
    холсте 3000 пикселей (PREP_W в render.py) — то есть детали в кадре
    достаточно, теряется она уже при сборке длинного ролика, а не здесь.

    -ss ДО -i, а не после: так ffmpeg перематывает по ключевым кадрам и не
    декодирует всё от начала файла. На получасовом ролике разница — секунды
    против минут.

    boxes приходят ГОТОВЫМИ из header_layout() — здесь не решается, что
    рисовать. Пустой список значит «шапки нет вовсе»: без вопроса в кадре
    когда-то повисала пустая белая коробка без единой буквы, а она занимает
    20% кадра и не сообщает ничего.
    """
    # 1080 * 9/16 = 607.5; libx264 требует чётные размеры, берём 608
    crop_w = 608
    box = "".join(
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color={c}:t={t},"
        for x, y, w, h, c, t in (boxes or []))
    vf = (
        f"crop={crop_w}:1080:(iw-{crop_w})/2:0,"
        f"scale={W}:{H}:flags=lanczos,setsar=1,"
        f"{box}"
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
        # Не падаем: шортс без шапки — рабочий шортс. Но молчать нельзя,
        # вопрос в шапке это и есть причина досмотреть.
        log("  ! в спецификации нет open_loop.question — шортсы будут без "
            "шапки с вопросом, а она и держит зрителя")

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
                     crf=int((job.get("style_override") or {}).get("crf", 20)),
                     boxes=layout["boxes"])
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
