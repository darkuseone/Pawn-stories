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

Как выглядит
------------
Одна карточка канала, не жребий из четырёх стилей: крупное число
заглавными, под ним акцентная линейка, которая прочерчивается слева
направо, под линейкой — короткая подпись помельче. Образец оформления —
композиция `lt-accent-underline` (имя выезжает снизу с проявлением,
линейка растёт от левого края, подпись всплывает следом; на выходе всё
уходит в обратном порядке).

Это ЗАМЕНА прежним четырём стилям (stamp / slide_up / typewriter /
underline_wipe). Они были жребием: каждый ролик получал свой, и плашка
переставала быть подписью канала — четыре разных оформления на семи
роликах читаются как четыре разных монтажёра. Разнообразие осталось, но
ушло туда, где ему место: сколько карточек в ролике, а не как они
выглядят.

Техника
-------
drawtext и drawbox с выражениями по времени: выезд — выражение по y,
проявление — по alpha, рост линейки — ширина drawbox от времени. Без
внешних файлов и без второго прохода рендера; браузерный движок образца
(GSAP) сюда не тянется по той же причине, что и в shorts.py.
"""

import re
from pathlib import Path

# ШРИФТ КАНАЛА, А НЕ СИСТЕМНЫЙ. Раньше здесь первым стоял DejaVu Serif:
# плашка с суммой — единственная надпись ролика, набранная НЕ Oswald, и
# на кадре это видно рядом со стеклом названия и субтитром. Канал свёл
# обложки, стекло и шортсы к одному файлу (type.py) — плашки остались
# единственным исключением, и это было упущением, а не решением.
#
# Системные остаются запасными: сборка не должна падать из-за шрифта.
# Если не нашли ни одного — плашки молча выключаются, ролик собирается
# без них (см. moments()).
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
]

# Разовый поиск шрифта канала, см. _channel_font().
_CHANNEL_FONT = {}

# Единственный вид карточки. Кортеж оставлен, чтобы style_override и
# проверка ENUMS в build.py продолжали работать именем, а не числом.
STYLES = ("card",)

# Сколько карточка живёт целиком, вместе с входом и уходом. 4.8 с — как в
# образце: меньше трёх секунд число не успевают прочитать, больше пяти оно
# читается как титр, а не как акцент.
HOLD = 4.8

# ГДЕ СТОИТ КАРТОЧКА — ВЕРХНЯЯ ТРЕТЬ, А НЕ НИЖНЯЯ.
#
# Образец — классическая нижняя треть (left 130, bottom 120). Сюда её
# поставить нельзя: с этой правки низ кадра занят вжжёнными субтитрами
# (pipeline/type.py, SUB_LONG_Y = 0.87 — полоса примерно 0.81–0.93 высоты),
# и карточка легла бы прямо на них. Прежние раскладки lower_left /
# lower_right стояли на 0.760 и по той же причине больше не годятся.
#
# Структура образца при этом сохраняется целиком: левый край, выключка
# влево, число — линейка — подпись. Меняется только высота.
CARD_X = 130
CARD_Y = 0.135            # доля высоты кадра до ВЕРХА числа

# Кегли образца, канвас тот же 1920×1080, поэтому берутся как есть.
NAME_FS = 76
ROLE_FS = 28
RULE_H = 6
GAP_NAME_RULE = 14
GAP_RULE_ROLE = 14

# Акцент линейки. ЕДИНСТВЕННЫЙ ХОЛОДНЫЙ ЦВЕТ НА ТЁПЛОМ КАНАЛЕ — из
# образца (#46e5b7). На тёплом цветокоре канала (янтарь, медь, лампа) он
# бьёт по контрасту сильнее любого тёплого акцента, и в этом весь смысл:
# линейка должна сказать «вот оно», а не слиться с кадром. Если однажды
# решим, что холод здесь чужой, — менять тут, одну строку.
RULE_COLOR = "0x46E5B7"
NAME_COLOR = "0xFFFFFF"
ROLE_COLOR = "0xE7EAF0"

# Тайминги входа и ухода из образца, в секундах от начала карточки.
IN_NAME_AT, IN_NAME_DUR = 0.10, 0.55
IN_RULE_AT, IN_RULE_DUR = 0.30, 0.50
IN_ROLE_AT, IN_ROLE_DUR = 0.46, 0.50
OUT_ROLE_BEFORE = 0.55    # за столько до конца карточки уходит подпись
OUT_RULE_BEFORE = 0.50
OUT_NAME_BEFORE = 0.45
OUT_DUR = 0.32
RISE_NAME = 28            # на сколько пикселей число выезжает снизу
RISE_ROLE = 16
LIFT_NAME = 16            # и на сколько уходит вверх на выходе

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


def _channel_font():
    """
    Путь к шрифту канала из type.py, если модуль вообще достаётся.

    Импорт ленивый и разовый: editorial — отдельный пакет, и жёсткая
    зависимость от соседнего модуля сломала бы его импорт в отрыве от
    конвейера (так его импортирует смоук и так же будут импортировать
    любые проверки). sys.path трогаем один раз, а не на каждую плашку.
    """
    if "path" in _CHANNEL_FONT:
        return _CHANNEL_FONT["path"]
    _CHANNEL_FONT["path"] = None
    try:
        import sys
        root = str(Path(__file__).parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        import type as type_mod
        p = type_mod.font_path()
        if p.exists():
            _CHANNEL_FONT["path"] = str(p)
    except Exception:
        pass
    return _CHANNEL_FONT["path"]


def font_path():
    """Шрифт канала, иначе первый существующий системный."""
    ch = _channel_font()
    if ch:
        return ch
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


# ─────────────────────── ГДЕ СТАВИТЬ ───────────────────────

MONEY_UNITS = {"dollars", "pounds", "euros", "долларов", "рублей"}


def _fact_at(text: str):
    """
    Вытаскивает из предложения ФАКТ: (число, единица, величина).

    Возвращает None, если числа нет. Число — короткая строка на крупную
    строку карточки («$9,000», «43 MILLION»), единица — подпись под
    линейкой («COINS», «DOLLARS»), величина — целое для ранжирования: из
    двух фактов на экран должен попасть тот, что больше, а не тот, что
    раньше встретился.

    Раньше функция отдавала одну склеенную строку («1,427 COINS»), и обе
    эти вещи приходилось разбирать заново на месте отрисовки.
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
        head = text[:mnum.start()].rstrip()
        tail = text[mnum.end():].strip().split()
        unit = ""
        if tail and tail[0].strip(".,!?").lower() in UNIT_TOKENS:
            unit = tail[0].strip(".,!?").upper()
        # Знак валюты стоит ПЕРЕД числом и в класс символов слова не
        # входит: без этой строки «$450 million» приезжало как голое «450».
        sym = head[-1] if head and head[-1] in "$£€" else ""
        try:
            mag = int(float(raw.replace(",", "")))
        except ValueError:
            mag = 0
        # «450 million» — величина в словах сразу за цифрой.
        for k, w in enumerate(tail[:2]):
            scale = _SCALE.get(w.strip(".,!?").lower())
            if scale and scale >= 1000:
                raw = f"{raw} {w.strip('.,!?').upper()}"
                mag *= scale
                unit = ""
                if len(tail) > k + 1 and tail[k + 1].strip(".,!?").lower() in UNIT_TOKENS:
                    unit = tail[k + 1].strip(".,!?").upper()
                break
        return (sym + raw).upper(), unit, mag

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
    # неё «one» из «one of the three» стало бы карточкой «1» — а таких «one»
    # в любом сценарии десятки, и каждое просилось бы на экран.
    if len(best) < 2 and unit is None:
        return None

    value = _to_digits([w.lower() for w in span])
    if value is None:
        return None
    return _display(value), (unit or "").upper(), value


def _is_bare_year(value: str, unit: str) -> bool:
    """
    «1943» без единицы — это ГОД, а не сумма.

    Тот же случай, что в rails._value_hook: четырёхзначное число в первой
    фразе про монету — имя предмета, а не его цена. Карточка с датой
    занимает место карточки с суммой, а их на ролик всего одна-три.
    """
    raw = value.replace(",", "").strip()
    if unit or not raw.isdigit() or len(raw) != 4:
        return False
    return 1500 <= int(raw) <= 2029


def _importance(mag: int, unit: str, kind: str) -> float:
    """
    Насколько факт важен. Чем больше — тем вероятнее попадёт на экран.

    Порядок величины, а не сама величина: между 9 000 и 450 000 000
    разница в пять порядков, и линейная шкала отдала бы все карточки
    одному ролику про Лувр. Деньги весомее счёта предметов: канал про то,
    сколько вещь СТОИЛА, а не сколько её везли.
    """
    import math
    score = math.log10(max(mag, 1) + 1)
    if unit.lower() in MONEY_UNITS:
        score += 1.2
    if kind == "revelation":
        score += 0.8
    return round(score, 3)


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


# СКОЛЬКО КАРТОЧЕК НА РОЛИК. Заказано «редко и только на действительно
# важных фактах»: потолок три на ролик любой длины и не ближе полутора
# минут друг к другу. Ролик с карточкой на каждой сумме — это инфографика,
# а не история; приём, который стоит в каждой развязке, перестаёт быть
# приёмом ровно так же, как стоял бы в каждом ролике.
MAX_CARDS = 3
MIN_GAP = 90.0

# Сколько карточек выпадет ИМЕННО ЭТОМУ ролику — единственное, что здесь
# осталось от жребия. Ось text_density (0.15..0.65) растягивается в 1..3:
# у соседних роликов разное число карточек, но вид у них один. Разнообразие
# должно быть в том, СКОЛЬКО, а не в том, КАК: четыре разных оформления на
# семи роликах читаются как четыре разных монтажёра.
def _want_count(density: float) -> int:
    lo, hi = 0.15, 0.65
    k = (max(lo, min(hi, density)) - lo) / (hi - lo)
    return max(1, min(MAX_CARDS, 1 + round(k * (MAX_CARDS - 1))))


def _chapter_of(beat, chapters) -> str:
    """Имя главы, к которой принадлежит доля. Нет карты глав — пусто."""
    if not chapters:
        return ""
    i = getattr(beat, "block", None)
    if not isinstance(i, int) or not (0 <= i < len(chapters)):
        return ""
    return " ".join(str(chapters[i]).split()).upper()


def moments(beats, marks, vector, rng=None, chapters=None):
    """
    Где по таймлайну поставить карточки.

    Возвращает список словарей {t, value, unit, hold, score}. Пустой список
    — нормальный результат: в ролике может не найтись ни одного числа.

    ОТБОР ПО ВЕСУ, А НЕ ПО ЖРЕБИЮ. Раньше кандидаты шли по порядку и
    каждый проходил монетку по плотности: на экран попадало то, что
    встретилось раньше, а не то, что важнее. Теперь все кандидаты
    ранжируются по порядку величины (см. _importance), берутся самые
    весомые, и уже они расставляются по времени с зазором MIN_GAP.

    Только развязки. Нагнетание (escalation) из кандидатов убрано: там
    числа идут перечислением, и карточка на каждом втором превращает
    нагнетание в таблицу. Развязка — то, ради чего досмотрели, и число в
    ней и есть тот самый «действительно важный факт».
    """
    style = vector.get("text_style", "card")
    if style == "none" or not font_path():
        return []
    density = float(vector.get("text_density", 0.4))

    cands = []
    for b in beats:
        if b.kind != "revelation":
            continue
        for m in marks[b.first_mark:b.last_mark + 1]:
            fact = _fact_at(m.get("text", ""))
            if not fact:
                continue
            value, unit, mag = fact
            if not (2 <= len(value) <= 16) or _is_bare_year(value, unit):
                continue
            # Подпись под линейкой: единица измерения, а если её нет —
            # имя главы, то есть история, к которой относится число.
            # Пустая подпись оставляет карточку без третьего элемента, а
            # он у неё в образце есть всегда.
            label = unit or _chapter_of(b, chapters)
            cands.append(dict(t=round(float(m["start"]), 3), value=value,
                              unit=label, kind=b.kind,
                              score=_importance(mag, unit, b.kind)))
    if not cands:
        return []

    want = _want_count(density)
    picked = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["t"])):
        if len(picked) >= want:
            break
        if any(abs(c["t"] - p["t"]) < MIN_GAP for p in picked):
            continue
        picked.append(c)
    picked.sort(key=lambda c: c["t"])
    for c in picked:
        c["hold"] = HOLD
    return picked


# ─────────────────────── ЧЕМ РИСОВАТЬ ───────────────────────

def _esc(s: str) -> str:
    """Экранирование текста для drawtext. Порядок важен: слэш первым."""
    s = s.replace("\\", r"\\\\")
    for ch in (":", "'", "%", ",", "[", "]", ";"):
        s = s.replace(ch, "\\" + ch)
    return s


def _text_width(text: str, size: int, font) -> float:
    """Ширина надписи настоящим шрифтом. Нет PIL или файла — оценка."""
    try:
        from PIL import ImageFont
        return ImageFont.truetype(str(font), size).getlength(text)
    except Exception:
        return len(text) * size * 0.52


def _ramp(t0: float, dur: float) -> str:
    """0 до t0, линейно до 1 за dur, дальше 1. Для alpha и смещений."""
    return f"min(1\\,max(0\\,(t-{t0:.3f})/{dur:.3f}))"


def filter_chain(items, font=None, size_scale=1.0):
    """
    Цепочка drawtext/drawbox для списка карточек.

    items — [{t_local, value, unit, hold}], где t_local это секунда ВНУТРИ
    той дорожки, на которую цепочка ляжет. Пересчёт из абсолютного времени
    делает вызывающий: только он знает смещения групп склейки.

    Возвращает строку фильтров (может быть пустой).
    """
    font = font or font_path()
    if not font or not items:
        return ""
    return ",".join(f for f in (_card(it, font, size_scale) for it in items) if f)


# На сколько отрезков режется рост линейки и её втягивание. 16 шагов на
# полсекунды — это 32 мс на шаг, мельче кадра при 30 к/с: глаз видит
# непрерывный рост, а не ступеньки.
RULE_IN_STEPS = 16
RULE_OUT_STEPS = 10


def _rule_steps(x: int, y_expr: str, full: int, h: int,
                t0: float, t1: float) -> list:
    """
    Линейка отрезками: рост, держание, втягивание.

    Кривая роста — замедление к концу (в образце ease power4.out): линейка
    выстреливает и мягко доходит до конца, а не ползёт равномерно.
    """
    out = []
    a0 = t0 + IN_RULE_AT
    a1 = a0 + IN_RULE_DUR
    b0 = t1 - OUT_RULE_BEFORE
    b1 = b0 + OUT_DUR

    def box(w, s, e):
        if w < 1 or e <= s:
            return
        out.append(f"drawbox=x={x}:y='{y_expr}':w={int(w)}:h={h}:"
                   f"color={RULE_COLOR}:t=fill:"
                   f"enable='between(t\\,{s:.3f}\\,{e:.3f})'")

    for i in range(1, RULE_IN_STEPS + 1):
        p_ = i / RULE_IN_STEPS
        w = full * (1 - (1 - p_) ** 4)          # power4.out
        s_ = a0 + (a1 - a0) * (i - 1) / RULE_IN_STEPS
        e_ = a0 + (a1 - a0) * i / RULE_IN_STEPS
        box(w, s_, e_)
    # держание: одна полная линейка от конца роста до начала ухода
    box(full, a1, b0)
    for j in range(1, RULE_OUT_STEPS + 1):
        p_ = j / RULE_OUT_STEPS
        w = full * (1 - p_ ** 2)                # power2.in
        s_ = b0 + (b1 - b0) * (j - 1) / RULE_OUT_STEPS
        e_ = b0 + (b1 - b0) * j / RULE_OUT_STEPS
        box(w, s_, e_)
    return out


def _card(it, font, size_scale=1.0):
    """
    Одна карточка: число, акцентная линейка, подпись.

    Вход и уход по образцу и в том же порядке: число выезжает снизу с
    проявлением, линейка растёт от левого края, подпись всплывает следом;
    на выходе первой гаснет подпись, за ней втягивается линейка, последним
    поднимается и гаснет число.

    Ширина линейки — ЗАМЕР числа настоящим шрифтом, а не доля кадра: в
    образце линейка ровно по ширине имени (width: 100% внутри колонки), и
    на «$9,000» и на «450 MILLION» она обязана быть разной.
    """
    t0 = float(it["t_local"])
    hold = float(it.get("hold", HOLD))
    t1 = t0 + hold
    value = str(it.get("value") or "")
    unit = str(it.get("unit") or "")
    if not value:
        return ""

    name_fs = max(28, int(NAME_FS * size_scale))
    role_fs = max(14, int(ROLE_FS * size_scale))
    rule_h = max(2, int(RULE_H * size_scale))
    x = int(CARD_X * size_scale)
    y_name = f"H*{CARD_Y:.3f}"
    # drawbox не знает заглавных W и H — только iw/ih. Заглавные знает
    # ТОЛЬКО drawtext, и на drawbox они падают «Undefined constant» не при
    # разборе строки, а на первом кадре, то есть на середине рендера
    # группы. Поймано прогоном, глазами такое не видно вовсе.
    y_name_box = y_name.replace("H", "ih")
    y_rule = f"{y_name_box}+{name_fs + GAP_NAME_RULE}"
    y_role = f"{y_name}+{name_fs + GAP_NAME_RULE + rule_h + GAP_RULE_ROLE}"

    en = f"between(t\\,{t0:.3f}\\,{t1:.3f})"
    parts = []

    # ── число: выезд снизу + проявление, на выходе подъём и гашение
    in_n = _ramp(t0 + IN_NAME_AT, IN_NAME_DUR)
    out_n_at = t1 - OUT_NAME_BEFORE
    out_n = _ramp(out_n_at, OUT_DUR)
    a_name = f"{in_n}*(1-{out_n})"
    y_expr = (f"{y_name}+{RISE_NAME}*(1-{in_n})-{LIFT_NAME}*{out_n}")
    parts.append(
        f"drawtext=fontfile={font}:text='{_esc(value)}':"
        f"fontcolor={NAME_COLOR}:fontsize={name_fs}:"
        f"borderw=0:shadowx=0:shadowy=2:shadowcolor=black@0.45:"
        f"x={x}:y='{y_expr}':alpha='{a_name}':enable='{en}'")

    # ── линейка: растёт слева направо, на выходе втягивается обратно
    #
    # СЛОЯМИ, А НЕ ВЫРАЖЕНИЕМ ПО ВРЕМЕНИ. drawbox считает свои x/y/w/h
    # ОДИН РАЗ, при сборке фильтра, а не на каждом кадре: выражение с t
    # там не работает вовсе. Проверено прямым рендером — линейка с
    # w='400*ramp(t)' выходила одинаковой ширины на 0.4, 0.6 и 1.5 секунде.
    # (Так же молча не анимировался и прежний стиль underline_wipe: он
    # рисовал полную линию с первого кадра.)
    #
    # У drawtext всё наоборот: x, y и alpha он считает покадрово — там же
    # проверено, — поэтому выезд и проявление текста остаются выражениями.
    #
    # Линейка поэтому набирается короткими отрезками с постоянной шириной,
    # каждый в своём окне enable: enable ФИЛЬТР проверяет на каждом кадре.
    # Тот же приём, что у прежнего typewriter, и он был рабочим.
    # Ширина линейки — по САМОЙ ШИРОКОЙ строке карточки, а не только по
    # числу. В образце линейка это flex-ребёнок с width:100%, то есть она
    # тянется на ширину колонки, а колонку задаёт самый широкий элемент —
    # им бывает и подпись («THE COINS THAT FOLLOWED» шире, чем «7 MILLION»).
    full = int(max(_text_width(value, name_fs, font),
                   _text_width(unit, role_fs, font) if unit else 0))
    parts += _rule_steps(x, y_rule, full, rule_h, t0, t1)

    # ── подпись: всплывает следом, уходит первой
    if unit:
        in_o = _ramp(t0 + IN_ROLE_AT, IN_ROLE_DUR)
        out_o = _ramp(t1 - OUT_ROLE_BEFORE, OUT_DUR)
        a_role = f"{in_o}*(1-{out_o})"
        y_o = f"{y_role}+{RISE_ROLE}*(1-{in_o})"
        parts.append(
            f"drawtext=fontfile={font}:text='{_esc(unit)}':"
            f"fontcolor={ROLE_COLOR}:fontsize={role_fs}:"
            f"borderw=0:shadowx=0:shadowy=2:shadowcolor=black@0.45:"
            f"x={x}:y='{y_o}':alpha='{a_role}':enable='{en}'")

    return ",".join(parts)
