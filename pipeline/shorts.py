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
libass для шапки и субтитров одним файлом. Стекло и жёлтый только в
длинном ролике; шортс непрозрачный — Oswald, белый, чёрная обводка.
Вступление вопроса: слой с \\blur гаснет, резкий слой punch 118%→100%,
вспышка обводки, уезжает наверх. Кусок не стартует с обрывка предложения.


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
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job
import type as type_mod

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

FONT = type_mod.font_name()

# Вступление вопроса: blur-слой гаснет, резкий слой punch 118%→100%
# и короткая вспышка обводки, затем уезжает наверх. Не путать с прежним
# «крупный вопрос на полкадра, потом ужимается в шапку» — масштаб ровно
# 118→100, как в ТЗ, не 190%.
INTRO_HOLD = 0.85
INTRO_MOVE = 0.55
PUNCH_MS = 380
BLUR_END = 0.42
OUTLINE = 8


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


# Начало предложения, которое на слух — продолжение предыдущего.
# Не трогаем for/in/on/when: ими начинаются нормальные завязки.
CONTINUATION = frozenset({
    "and", "but", "that", "which", "who", "whom", "whose", "or", "nor",
    "because", "while", "so", "then", "yet", "however", "plus", "minus",
    "increments", "breaking", "until", "than", "though", "although",
    "unless", "whether", "also", "just", "even", "still", "including",
    "not", "nor",
})


def _first_word(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    return t.split()[0].strip(".,;:!?\"'()[]“”‘’").lower()


def is_fragment_start(text: str) -> bool:
    """Предложение или его первый чанк — обрывок, а не завязка."""
    t = (text or "").strip()
    if not t:
        return True
    if t[0].islower():
        return True
    if _first_word(t) in CONTINUATION:
        return True
    chunks = split_chunks(t)
    if not chunks:
        return False
    first = chunks[0].strip()
    if first and first[0].islower():
        return True
    if _first_word(first) in CONTINUATION:
        return True
    return False


def is_hook_start(text: str) -> bool:
    """Завязка той же истории: вопрос, «here's the part», год, имя."""
    t = (text or "").strip()
    if not t or is_fragment_start(t):
        return False
    low = t.lower()
    if "?" in t[:160]:
        return True
    if re.search(r"here's the (part|where|what|how)", low):
        return True
    if re.search(r"here is the (part|where|what|how)", low):
        return True
    if re.match(
            r"^(in |on |by )?(january|february|march|april|may|june|"
            r"july|august|september|october|november|december)\b", low):
        return True
    if re.match(r"^(nineteen|twenty)\b", low):
        return True
    if re.match(r"^(19|20)\d{2}\b", t):
        return True
    if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", t):
        return True
    return False


def window_for(beat, marks, total, min_s=SHORT_MIN, max_s=SHORT_MAX,
               block_t0=0.0):
    """
    Границы куска вокруг доли, ПО ГРАНИЦАМ ПРЕДЛОЖЕНИЙ.

    Растёт НАЗАД от развязки. Не стартует с обрывка («increments,
    breaking through two hundred»): если первый чанк — продолжение,
    lo сдвигается ещё на одно предложение. Не выходит за начало той же
    истории (block_t0). Развязка остаётся ближе к концу.
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

    def can_back():
        return lo > 0 and marks[lo - 1]["start"] >= block_t0 - 0.05

    def txt(i):
        return marks[i].get("text") or ""

    while span() < min_s and can_back():
        lo -= 1
    while span() < min_s and hi < len(marks) - 1:
        hi += 1

    while can_back() and is_fragment_start(txt(lo)):
        lo -= 1
        if span() > max_s + 10:
            break

    while can_back() and span() < max_s and not is_hook_start(txt(lo)):
        if is_hook_start(txt(lo - 1)):
            lo -= 1
        else:
            break

    while span() > max_s and lo < hi:
        nxt = lo + 1
        while nxt < hi and is_fragment_start(txt(nxt)):
            nxt += 1
        if marks[hi]["end"] - marks[nxt]["start"] < min_s * 0.85:
            break
        lo = nxt
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
            w = window_for(
                b, marks, total,
                block_t0=min((x.start for x in beats if x.block == b.block),
                             default=0.0))
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
    return type_mod.ass_time(t)


def ass_escape(s: str) -> str:
    return type_mod.ass_escape(s)


def fit_size(lines, max_w: int, size: int, floor: int = 30) -> int:
    """Кегль по замеру настоящего шрифта канала, не по числу символов."""
    return type_mod.fit_size(lines, max_w, size, floor=floor)


def wrap(text: str, per_line: int):
    """Простой перенос по словам. drawtext и ASS сами не переносят."""
    return type_mod.wrap_text(text, per_line)


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
    kicker = (y.get("cover_kicker") or "").strip()
    if kicker:
        return kicker
    overlay = ((job.get("_превью_промпт") or {}).get("overlay_text") or "").strip()
    if overlay:
        return overlay
    chapters = y.get("chapters") or []
    return str(chapters[0]).strip() if chapters else ""


def header_layout(question: str, style: str = "") -> dict:
    """
    Геометрия шапки: вопрос сверху, без панели.

    Пустой вопрос — пустой макет: без него десять секунд провисела бы
    пустая анимация без единой буквы.
    style оставлен в сигнатуре, чтобы старые вызовы не падали, и игнорируется.
    """
    lines = wrap(question.strip(), 24) if question else []
    if not lines:
        return dict(lines=[], size=0, cx=W // 2, intro_cy=0, block_cy=0)
    size = 64 if len(lines) <= 2 else (54 if len(lines) == 3 else 44)
    size = fit_size(lines, W - 2 * BOX_MARGIN, size, floor=34)
    line_h = size * 1.22
    ph = line_h * len(lines)
    block_cy = BOX_MARGIN + ph / 2
    return dict(lines=lines, size=size, cx=W // 2,
                intro_cy=int(H * 0.42), block_cy=block_cy)


def build_ass(subs, layout: dict, out: Path):
    """
    Шапка и субтитры: Oswald, белый, чёрная обводка, без стекла и без жёлтого.

    Вступление вопроса в libass: слой с сильным \\blur гаснет, резкий слой
    \\fad + масштаб 118%→100%, вспышка обводки, затем \\move наверх.
    Не Remotion, не deshake, не второй Ken Burns.
    """
    q_size = layout["size"] or 1
    all_lines = [l for _, _, c in subs for l in wrap(c, SUB_MAX_CHARS)]
    sub_size = fit_size(all_lines, W - 2 * SUB_MARGIN, SUB_SIZE, floor=34)
    white, black = "&H00FFFFFF", "&H00000000"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: QGLOW,{FONT},{q_size},{white},{black},&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,40,40,40,1
Style: Q,{FONT},{q_size},{white},{black},&H00000000,-1,0,0,0,100,100,0,0,1,{OUTLINE},0,5,40,40,40,1
Style: SUB,{FONT},{sub_size},{white},{black},&H00000000,-1,0,0,0,100,100,0,0,1,7,0,5,{SUB_MARGIN},{SUB_MARGIN},60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    if layout["lines"]:
        text = "\\N".join(ass_escape(l) for l in layout["lines"])
        cx = layout["cx"]
        intro_cy = int(layout["intro_cy"])
        top_cy = int(layout["block_cy"])
        hold_ms = round(INTRO_HOLD * 1000)
        move_end = round((INTRO_HOLD + INTRO_MOVE) * 1000)
        rows.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(BLUR_END)},QGLOW,,0,0,0,,"
            f"{{\\an5\\pos({cx},{intro_cy})\\blur22\\fad(50,280)\\alpha&H48&}}"
            + text)
        rows.append(
            f"Dialogue: 2,{ass_time(0)},{ass_time(99999)},Q,,0,0,0,,"
            f"{{\\an5\\move({cx},{intro_cy},{cx},{top_cy},{hold_ms},{move_end})"
            f"\\fscx118\\fscy118\\t(0,{PUNCH_MS},\\fscx100\\fscy100)"
            f"\\bord{OUTLINE}\\t(120,200,\\bord16)\\t(200,360,\\bord{OUTLINE})"
            f"\\fad(90,0)}}" + text)
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
    # 1080 * 9/16 = 607.5; libx264 требует чётные размеры, берём 608.
    # hqdn3d ПОСЛЕ crop, ДО scale: в final.mp4 уже запечены движение камеры
    # и temporal grain/искры. Вертикальный кроп оставляет треть ширины и
    # усиливает горизонтальный дрейф втрое, а зерно после апскейла читается
    # как вторая тряска. deshake/vidstab сюда нельзя — они борются с
    # запечённым Ken Burns и качают сами.
    crop_w = 608
    ass_f = f"ass={ass.as_posix()}:fontsdir={type_mod.fontsdir()}"
    vf = (
        f"crop={crop_w}:1080:(iw-{crop_w})/2:0,"
        f"hqdn3d=1.2:1.2:3:3,"
        f"scale={W}:{H}:flags=lanczos,setsar=1,"
        f"{ass_f}"
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

    log("── оформление шапки: Oswald, белый, чёрная обводка")

    made, questions_used = [], []
    for n, (t0, t1, lo, hi, beat) in enumerate(windows, 1):
        # Свой вопрос под свой блок сценария, а не один на оба шортса: два
        # куска почти всегда режутся из РАЗНЫХ историй ролика (разные
        # script_blocks), и общий вопрос либо не относится ко второму
        # шортсу, либо выдаёт его развязку раньше, чем видео до неё дошло.
        # per_block ключуется строкой номера блока (JSON не умеет int-ключи).
        question = per_block.get(str(beat.block), default_question)
        layout = header_layout(question)
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
