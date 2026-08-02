"""
textcard.py — КИНЕТИЧЕСКАЯ ТИПОГРАФИКА на числах и датах.

Зачем
-----
Ролик, собранный только из чужих картинок и чужого видео, не содержит НИ
ОДНОГО собственного изображения — даже когда сценарий свой, монтаж свой и
разбор структуры свой. Плашка с суммой, набранная и анимированная самим
конвейером, это единственный элемент кадра, которого нет ни в одном стоке.
Стоит она ноль (шрифт системный, рендер тем же ffmpeg) и добавляет ролику
то, чего в исходном материале не было вовсе.

Второй смысл — редакторский. Числа в этом жанре несут сюжет: сорок три
миллиона, тысяча четыреста двадцать семь монет, пять дней. Зритель их не
запоминает на слух. Плашка ставит цифру на экран ровно тогда, когда её
произносят, и это решение монтажёра, а не оформление.

Когда ставится
--------------
Только на долях-развязках и только там, где в тексте реально звучит число.
Плотность задаётся осью text_density вектора стиля, стиль анимации — осью
text_style. У части роликов ось выпадает в "none", и плашек нет вовсе:
приём, стоящий в каждой загрузке, перестаёт быть приёмом.

Техника
-------
drawtext с выражениями по времени. Все четыре стиля собраны на альфе и
координатах, без внешних файлов и без второго прохода рендера.
"""

import re
from pathlib import Path

# Системные шрифты. Проверяются по порядку: на раннере GitHub Actions
# стоит DejaVu, на локальной машине может быть что угодно. Если не нашли
# ни одного — плашки молча выключаются, ролик собирается без них.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
]

# Стили анимации. Каждый — своя механика появления, а не своя длительность
# одного и того же выезда.
STYLES = ("stamp", "slide_up", "typewriter", "underline_wipe")

# Сколько плашка держится на экране. Меньше двух секунд читается как сбой,
# больше пяти — как титр.
HOLD = (2.6, 4.2)

# Раскладка. Две позиции, чтобы плашки не выстраивались в столбик у тех
# роликов, где их несколько.
PLACES = {
    "lower_left":  dict(x="W*0.070", y="H*0.760"),
    "lower_right": dict(x="W*0.560", y="H*0.762"),
    "upper_left":  dict(x="W*0.072", y="H*0.135"),
}

# Числа словами — тем же словарём, что и в beats.py, но здесь нужен ПОРЯДОК
# слов, чтобы вытащить фразу целиком: «forty-three million pounds», а не
# три отдельных слова.
NUM_TOKENS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
    "billion", "and",
}
UNIT_TOKENS = {
    "dollars", "pounds", "euros", "coins", "years", "days", "months",
    "weeks", "hours", "minutes", "seconds", "percent", "kilograms", "kilos",
    "grams", "objects", "pieces", "items", "people", "miles", "inches",
    "feet", "metres", "meters", "centimetres", "carats", "bags", "cans",
    "долларов", "рублей", "монет", "лет", "дней", "часов", "процентов",
    "килограммов", "граммов", "метров", "предметов", "человек",
}

# Насколько далеко за числом искать единицу измерения. Два слова, потому
# что между ними стандартно встаёт определение: «одна тысяча четыреста
# двадцать семь ЗОЛОТЫХ монет». На одном слове единица терялась, и на
# экран уходило голое «1,427».
UNIT_LOOKAHEAD = 2


def font_path():
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


# ─────────────────────── ГДЕ СТАВИТЬ ───────────────────────

def _phrase_at(text: str):
    """
    Вытаскивает числовую фразу из предложения.

    Возвращает короткую строку для экрана либо None. Фраза обрезается по
    четырём словам: «one thousand four hundred and twenty-seven gold coins»
    на экране нечитаемо, а «1,427 COINS» читается за полсекунды — поэтому
    словесные числительные ещё и сворачиваются в цифры.
    """
    text = text or ""
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9\-]+", text)
    low = [w.lower() for w in words]

    # Готовая цифра в тексте. Ищется ПО ИСХОДНОЙ СТРОКЕ, а не по разбитым
    # словам: разделитель тысяч в класс символов слова не входит, и «43,000»
    # приезжало сюда двумя кусками, из которых брался первый. На экране
    # выходило «43» вместо «43,000» — ошибка в тысячу раз, и молчаливая.
    mnum = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if mnum:
        raw = mnum.group(0).rstrip(".,")
        tail = text[mnum.end():].strip().split()
        unit = tail[0].strip(".,!?").upper() if tail and \
            tail[0].strip(".,!?").lower() in UNIT_TOKENS else ""
        return f"{raw} {unit}".strip().upper()

    # числительные словами: ищем самую длинную непрерывную цепочку
    best, cur = [], []
    for i, w in enumerate(low):
        if w in NUM_TOKENS or re.fullmatch(r"(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-\w+", w):
            cur.append(i)
        else:
            if len(cur) > len(best):
                best = cur
            cur = []
    if len(cur) > len(best):
        best = cur
    if not best:
        return None

    span = words[best[0]:best[-1] + 1]

    # Единица измерения ищется не вплотную за числом, а в пределах двух
    # слов: между ними стандартно встаёт определение («тысяча четыреста
    # двадцать семь ЗОЛОТЫХ монет»).
    unit = None
    for k in range(best[-1] + 1, min(best[-1] + 1 + UNIT_LOOKAHEAD, len(low))):
        if low[k] in UNIT_TOKENS:
            unit = words[k]
            break

    # Однословное числительное берётся ТОЛЬКО с единицей измерения. Без
    # неё «one» из «one of the three» стало бы плашкой «1» — а таких «one»
    # в любом сценарии десятки, и каждое просилось бы на экран.
    if len(best) < 2 and unit is None:
        return None
    if unit:
        span = span + [unit]

    value = _to_digits([w.lower() for w in span])
    if value is not None:
        unit = span[-1].upper() if span[-1].lower() in UNIT_TOKENS else ""
        return f"{_display(value)} {unit}".strip()
    if len(span) > 5:
        return None
    return " ".join(span).upper()


def _display(v: int) -> str:
    """
    Число на экран. Крупные разряды словом, остальное цифрами.

    «43,000,000» в углу кадра читается как случайный набор нулей, «43
    MILLION» — мгновенно. А вот «1,427» словом («ONE THOUSAND FOUR HUNDRED
    AND TWENTY SEVEN») не читается вовсе, поэтому граница проходит по
    миллиону, а не по тысяче.
    """
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:g} BILLION"
    if v >= 1_000_000:
        return f"{v / 1_000_000:g} MILLION"
    return f"{v:,}"


_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SCALE = {"hundred": 100, "thousand": 1000, "million": 1_000_000,
          "billion": 1_000_000_000}


def _to_digits(tokens):
    """
    Словесное числительное -> целое число. Не разобралось — None.

    Обычный разбор английских числительных: разряд УМНОЖАЕТ накопленное и
    сбрасывает накопитель, а не прибавляется к нему.

    Первая версия прибавляла, и «one thousand four hundred and twenty
    seven» превращалось в 428 вместо 1427 — единица от «one thousand»
    складывалась с 427 вместо того, чтобы стать тысячей. Поймано
    прогоном на реальных фразах сценария: ни один тест на отдельных
    словах такую ошибку не показывает, она вылезает только на составных.
    """
    total, current = 0, 0
    seen = False
    for t in tokens:
        for part in t.split("-"):
            if part in _ONES:
                current += _ONES[part]
                seen = True
            elif part in _TENS:
                current += _TENS[part]
                seen = True
            elif part == "hundred":
                current = max(current, 1) * 100
                seen = True
            elif part in _SCALE:                 # thousand / million / billion
                total += max(current, 1) * _SCALE[part]
                current = 0
                seen = True
            elif part in ("and",) or part in UNIT_TOKENS:
                continue
            else:
                return None
    if not seen:
        return None
    total += current
    return total or None


def moments(beats, marks, vector, rng):
    """
    Где по таймлайну поставить плашки.

    Возвращает список словарей {t, text, style, place, hold}. Пустой список
    — нормальный результат: у ролика может не выпасть стиль либо не найтись
    ни одного числа.
    """
    style = vector.get("text_style", "none")
    if style == "none" or not font_path():
        return []
    density = float(vector.get("text_density", 0.3))

    # кандидаты — только доли-развязки и нагнетания: плашка на завязке
    # оформляет то, что не требует оформления
    want_kinds = {"revelation", "escalation"}
    cands = []
    for b in beats:
        if b.kind not in want_kinds:
            continue
        for m in marks[b.first_mark:b.last_mark + 1]:
            phrase = _phrase_at(m.get("text", ""))
            if phrase and 2 <= len(phrase) <= 22:
                cands.append((m["start"], phrase, b.kind))

    if not cands:
        return []

    # прореживаем: не чаще одной плашки на MIN_GAP и не больше, чем просит
    # плотность. Две плашки подряд превращают ролик в инфографику.
    MIN_GAP = 45.0
    picked, last_t = [], -1e9
    for t, phrase, kind in cands:
        if t - last_t < MIN_GAP:
            continue
        if rng.random() > density:
            continue
        picked.append(dict(
            t=round(t, 3), text=phrase,
            style=style if style in STYLES else rng.choice(STYLES),
            place=rng.choice(list(PLACES)),
            hold=round(rng.uniform(*HOLD), 2)))
        last_t = t
    return picked


# ─────────────────────── ЧЕМ РИСОВАТЬ ───────────────────────

def _esc(s: str) -> str:
    """Экранирование текста для drawtext. Порядок важен: слэш первым."""
    s = s.replace("\\", r"\\\\")
    for ch in (":", "'", "%", ",", "[", "]", ";"):
        s = s.replace(ch, "\\" + ch)
    return s


def filter_chain(items, font=None, size_scale=1.0):
    """
    Цепочка drawtext для списка плашек.

    items — [{t_local, text, style, place, hold}], где t_local это секунда
    ВНУТРИ той дорожки, на которую цепочка ляжет. Пересчёт из абсолютного
    времени делает вызывающий: только он знает смещения групп склейки.

    Возвращает строку фильтров (может быть пустой).
    """
    font = font or font_path()
    if not font or not items:
        return ""

    parts = []
    for it in items:
        t0 = float(it["t_local"])
        hold = float(it.get("hold", 3.0))
        t1 = t0 + hold
        place = PLACES.get(it.get("place"), PLACES["lower_left"])
        size = int(58 * size_scale)
        parts.append(_one(it, t0, t1, place, font, size))
    return ",".join(p for p in parts if p)


def _one(it, t0, t1, place, font, size):
    style = it.get("style", "stamp")
    txt = _esc(it["text"])
    x, y = place["x"], place["y"]
    # общая обёртка: плашка живёт только в своём окне
    en = f"between(t\\,{t0:.3f}\\,{t1:.3f})"

    # вход и выход по альфе — общие для всех стилей, различается движение
    fade_in, fade_out = 0.35, 0.45
    alpha = (f"if(lt(t\\,{t0 + fade_in:.3f})\\,"
             f"(t-{t0:.3f})/{fade_in}\\,"
             f"if(gt(t\\,{t1 - fade_out:.3f})\\,"
             f"({t1:.3f}-t)/{fade_out}\\,1))")

    base = (f"fontfile={font}:text='{txt}':fontcolor=white:"
            f"fontsize={size}:borderw=4:bordercolor=black@0.85:"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.5:enable='{en}'")

    if style == "stamp":
        # удар: буквы приходят чуть крупнее и садятся на место. Размер в
        # drawtext не анимируется, поэтому «удар» делается альфой с резким
        # фронтом и подскоком по вертикали на несколько пикселей.
        bump = (f"{y}-14*max(0\\,1-(t-{t0:.3f})/0.22)")
        return (f"drawtext={base}:x={x}:y='{bump}':"
                f"alpha='min(1\\,{alpha}*1.6)'")

    if style == "slide_up":
        rise = f"{y}+52*max(0\\,1-(t-{t0:.3f})/0.55)"
        return f"drawtext={base}:x={x}:y='{rise}':alpha='{alpha}'"

    if style == "underline_wipe":
        # Текст стоит, под ним уезжает линия.
        #
        # Координаты для drawbox пишутся ЧЕРЕЗ iw/ih, а не через W/H.
        # Заглавные W и H знает только drawtext; drawbox на них падает с
        # «Undefined constant», причём падает не при разборе строки, а
        # при первом кадре — то есть на середине рендера группы. Поймано
        # прогоном всех четырёх стилей через ffmpeg, глазами такое не
        # видно вовсе.
        bx = x.replace("W", "iw").replace("H", "ih")
        by = y.replace("W", "iw").replace("H", "ih")
        line_w = f"min(1\\,(t-{t0:.3f})/0.6)"
        under = (f"drawbox=x='{bx}':y='{by}+{int(size * 1.15)}':"
                 f"w='{int(size * 0.62)}*{len(it['text'])}*{line_w}':"
                 f"h=5:color=white@0.85:t=fill:enable='{en}'")
        return f"drawtext={base}:x={x}:y='{y}':alpha='{alpha}',{under}"

    if style == "typewriter":
        # посимвольное появление. drawtext не умеет резать строку по
        # времени, поэтому строка печатается слоями: каждый слой — на один
        # символ длиннее и живёт свой отрезок. Для строк до 22 символов это
        # два десятка вызовов, что рендер не замечает.
        step = 0.055
        layers = []
        s = it["text"]
        for k in range(1, len(s) + 1):
            a = t0 + step * (k - 1)
            b = t0 + step * k if k < len(s) else t1
            layers.append(
                f"drawtext=fontfile={font}:text='{_esc(s[:k])}':"
                f"fontcolor=white:fontsize={size}:borderw=4:"
                f"bordercolor=black@0.85:x={x}:y='{y}':"
                f"alpha='{alpha}':"
                f"enable='between(t\\,{a:.3f}\\,{b:.3f})'")
        return ",".join(layers)

    return f"drawtext={base}:x={x}:y='{y}':alpha='{alpha}'"
