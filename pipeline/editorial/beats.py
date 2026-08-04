"""
beats.py — разбор сценария на НАРРАТИВНЫЕ ДОЛИ.

Зачем это вообще
----------------
Монтаж по секундомеру («кадр пять секунд, к финалу семь») даёт ровный ритм,
и ровность — главный признак поточной сборки. Живой монтажёр режет не по
таймеру: под перечислением фактов он частит, на цифре останавливается, после
развязки даёт кадру выдохнуть. Ритм у него идёт ОТ ТЕКСТА.

Здесь то же самое, только правило записано. Сценарий раскладывается на доли,
у каждой доли свой замысел, и уже замысел задаёт плотность склеек, тип
материала и амплитуду движения камеры.

Откуда берутся признаки
-----------------------
Всё считается по тайм-кодам озвучки и по самому тексту, БЕЗ ЕДИНОГО ЗАПРОСА
К МОДЕЛИ. Это принципиально: разбор идёт на каждой пересборке монтажа, а
пересборок у ролика пять-десять. Платный разбор структуры означал бы, что
за правку одной склейки платишь как за новый ролик.

Признаки, которые реально работают:

  скорость речи   символов в секунду по тайм-кодам ElevenLabs. Диктор сам
                  замедляется на важном — это лучший сигнал во всём наборе,
                  и он достаётся бесплатно вместе со звуком
  длина фразы     короткие рубленые предложения = нагнетание
  пауза после     диктор молчит дольше там, где мысль закончилась
  плотность цифр  суммы, даты, счёт — признак развязки и фактурного куска
  второе лицо     «вы», «tell me», «think about» — обращение к зрителю
  вопросы         в финале это призыв, в середине — риторика перед поворотом

Ни один признак сам по себе не решает: классификация идёт по сумме баллов,
и доля, не набравшая ни на что, честно остаётся завязкой.
"""

import re
import unicodedata
from dataclasses import dataclass, field

# ─────────────────────────── ТИПЫ ДОЛЕЙ ───────────────────────────
#
# Каждый тип — это РЕДАКТОРСКИЙ ЗАМЫСЕЛ, а не жанр текста. Название говорит,
# что монтаж должен сделать со зрителем на этом куске.

HOOK = "hook"                # удержать первые секунды: частая перебивка
SETUP = "setup"              # ввести в курс: ровный средний темп
ESCALATION = "escalation"    # нагнетать: кадры укорачиваются по ходу доли
REVELATION = "revelation"    # развязка: остановка, длинный кадр, тишина
REFLECTION = "reflection"    # выдох: медленно, минимум движения
CTA = "cta"                  # обращение к зрителю: возврат к образам

ALL_KINDS = (HOOK, SETUP, ESCALATION, REVELATION, REFLECTION, CTA)

# Замысел каждой доли в числах. Это НЕ окончательные длительности: поверх
# ляжет кривая из pacing.py и разброс от вектора стиля. Здесь задаётся
# характер — во сколько раз доля быстрее или медленнее среднего по ролику.
#
# clip_share — сколько слотов доли отдаётся стоковому видео. У развязки он
# низкий сознательно: под конкретный предмет нужен архив или генерация, а
# не абстрактный сток «руки в темноте».
#
# motion — множитель амплитуды движения камеры. На выдохе камера почти
# стоит, на нагнетании ходит широко.
INTENT = {
    HOOK:       dict(tempo=0.42, clip_share=0.62, motion=1.15, spread=0.30),
    SETUP:      dict(tempo=1.00, clip_share=0.30, motion=1.00, spread=0.22),
    ESCALATION: dict(tempo=0.72, clip_share=0.38, motion=1.20, spread=0.26),
    REVELATION: dict(tempo=1.65, clip_share=0.12, motion=0.72, spread=0.16),
    REFLECTION: dict(tempo=1.35, clip_share=0.18, motion=0.62, spread=0.18),
    CTA:        dict(tempo=1.12, clip_share=0.22, motion=0.85, spread=0.20),
}

# Слова второго лица. Разделены по языкам: сценарии на канале английские,
# но конвейер обязан пережить русский текст без падения в ноль признаков.
SECOND_PERSON = {
    "you", "your", "yours", "yourself", "tell", "think", "imagine", "ask",
    "look", "remember", "consider", "subscribe", "comment", "below",
    "вы", "ваш", "ваши", "тебе", "твой", "представьте", "подумайте",
    "смотрите", "вспомните", "напишите", "подпишитесь",
}

# Слова-указатели развязки: ими начинается предложение, где выясняется
# главное. Признак слабый по одиночке, поэтому вес у него маленький.
TURN_MARKERS = {
    "but", "then", "instead", "however", "except", "until", "suddenly",
    "actually", "turns", "never", "nobody", "nothing", "was", "wasn",
    "но", "однако", "зато", "вдруг", "оказалось", "никто", "ничего",
}


def _norm(s: str) -> str:
    """Строка без пунктуации и регистра — для сопоставления текстов."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def _words(s: str):
    return re.findall(r"[a-zA-Zа-яА-ЯёЁ]+", (s or "").lower())


# ─────────────────────────── ДОЛЯ ───────────────────────────

@dataclass
class Beat:
    """Одна нарративная доля: кусок таймлайна с общим редакторским замыслом."""

    kind: str
    start: float
    end: float
    block: int                     # номер блока сценария, из которого доля
    first_mark: int                # индекс первого тайм-кода доли
    last_mark: int                 # индекс последнего (включительно)
    features: dict = field(default_factory=dict)
    reason: str = ""               # почему классифицирована именно так

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.001)

    @property
    def intent(self) -> dict:
        return INTENT[self.kind]

    def summary(self) -> dict:
        return dict(kind=self.kind, start=round(self.start, 2),
                    end=round(self.end, 2), seconds=round(self.duration, 1),
                    block=self.block, reason=self.reason,
                    features={k: round(v, 3) for k, v in self.features.items()})


# ─────────────────────────── ГРАНИЦЫ БЛОКОВ ───────────────────────────

def block_bounds(marks, script_blocks):
    """
    Где в тайм-кодах начинается каждый блок сценария.

    Блоки — это авторские главы, и границы между ними самые надёжные из всех
    возможных: их поставил человек. Тайм-коды приходят от озвучки сплошным
    потоком и номера блока не несут, поэтому блок ищется по тексту.

    Сопоставление идёт по СКЛЕЕННОЙ нормализованной строке, а не по отдельным
    предложениям. Причина: ElevenLabs режет фразы не там, где точки в
    сценарии, и поиск «первого предложения блока» среди тайм-кодов
    промахивается на каждом втором блоке. Склеенная строка от этого не
    зависит вовсе.

    Возвращает список индексов первых тайм-кодов каждого блока. При неудаче
    сопоставления блок получает пропорциональную границу — ролик соберётся,
    просто доли лягут грубее.
    """
    if not marks:
        return []
    if not script_blocks:
        return [0]

    joined = ""
    offsets = []                    # offsets[i] — где начинается marks[i]
    for m in marks:
        offsets.append(len(joined))
        joined += _norm(m.get("text", ""))

    bounds, cursor = [], 0
    for bi, block in enumerate(script_blocks):
        if bi == 0:
            bounds.append(0)
            cursor = 0
            continue
        # ключ — первые 60 значимых символов блока. Меньше 24 брать нельзя:
        # короткий ключ находится в середине предыдущего блока.
        key = _norm(block)[:60]
        pos = joined.find(key, cursor + 1) if len(key) >= 24 else -1
        if pos < 0:
            # не нашли — делим оставшееся поровну между оставшимися блоками
            left = len(script_blocks) - bi
            span = (len(marks) - bounds[-1]) // max(left + 1, 1)
            bounds.append(min(len(marks) - 1, bounds[-1] + max(span, 1)))
            continue
        # переводим позицию в символах в индекс тайм-кода
        idx = 0
        for i, off in enumerate(offsets):
            if off <= pos:
                idx = i
            else:
                break
        bounds.append(max(idx, bounds[-1] + 1))
        cursor = pos
    return bounds


# ─────────────────────────── ПРИЗНАКИ ───────────────────────────

def features_of(marks, lo, hi, mean_rate: float) -> dict:
    """
    Числовые признаки куска тайм-кодов [lo, hi]. Все нормированы к 0..1
    либо к среднему по ролику, чтобы пороги классификации не зависели от
    длины ролика и от скорости конкретного диктора.
    """
    span = marks[lo:hi + 1]
    if not span:
        return dict(rate=1.0, short=0.0, num=0.0, you=0.0, q=0.0,
                    turn=0.0, pause=0.0)

    texts = [m.get("text", "") for m in span]
    chars = sum(len(t) for t in texts) or 1
    secs = sum(max(m["end"] - m["start"], 0.01) for m in span)

    # Скорость речи относительно средней по ролику. Ниже единицы — диктор
    # замедлился, а он замедляется на важном.
    rate = (chars / secs) / (mean_rate or 1.0)

    # Доля коротких фраз. Порог в 60 символов — примерно две с половиной
    # секунды речи: всё, что короче, звучит как рубленая фраза.
    short = sum(1 for t in texts if len(t) < 60) / len(texts)

    words = _words(" ".join(texts))
    nwords = len(words) or 1

    # Плотность чисел. Считаются и цифры, и записанные словами суммы —
    # в начитке для TTS числа всегда прописаны словами («forty-three
    # million»), цифр в таком тексте нет вовсе, и счёт только по \d дал бы
    # ноль на самом фактурном куске ролика.
    digits = len(re.findall(r"\d", " ".join(texts)))
    numwords = sum(1 for w in words if w in NUMBER_WORDS)
    num = min(1.0, (numwords + digits * 0.5) / nwords * 12)

    you = min(1.0, sum(1 for w in words if w in SECOND_PERSON) / nwords * 20)
    turn = min(1.0, sum(1 for w in words if w in TURN_MARKERS) / nwords * 10)
    q = min(1.0, sum(t.count("?") for t in texts) / len(texts) * 3)

    # Средняя пауза после фразы: там, где мысль закончилась, диктор молчит.
    gaps = []
    for i in range(lo, min(hi, len(marks) - 2) + 1):
        gaps.append(max(0.0, marks[i + 1]["start"] - marks[i]["end"]))
    pause = min(1.0, (sum(gaps) / len(gaps) / 0.55) if gaps else 0.0)

    return dict(rate=rate, short=short, num=num, you=you, q=q,
                turn=turn, pause=pause)


NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
    "billion", "dollars", "pounds", "euros", "percent", "half", "quarter",
    "первый", "второй", "третий", "десять", "сто", "тысяча", "миллион",
    "миллиард", "рублей", "долларов", "процент", "процентов",
}


# ─────────────────────────── КЛАССИФИКАЦИЯ ───────────────────────────

def ranks_of(values):
    """
    Позиция каждого значения в РАСПРЕДЕЛЕНИИ ПО ЭТОМУ ЖЕ РОЛИКУ, 0..1.

    Это ключевая поправка, и она стоила одного полного прогона.

    Сначала пороги классификации были абсолютными: «плотность цифр выше
    0.34 — развязка». На сценарии про находки это дало 86% развязок из
    всего ролика. Причина очевидна задним числом: сценарий целиком про
    деньги, суммы звучат в каждом втором абзаце, и абсолютный порог по
    цифрам там срабатывает всюду. Классификатор, выдающий один тип на весь
    ролик, возвращает нас ровно к той монотонности, ради ухода от которой
    он написан.

    Правильный вопрос не «много ли здесь цифр», а «БОЛЬШЕ ЛИ ИХ ЗДЕСЬ, ЧЕМ
    В ОСТАЛЬНОМ ЭТОМ ЖЕ СЦЕНАРИИ». Ранг на него и отвечает. Побочная
    выгода: пороги перестают зависеть от жанра, от языка и от диктора —
    сравнение всегда внутри одного ролика.
    """
    n = len(values)
    if n < 2:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    for place, i in enumerate(order):
        out[i] = place / (n - 1)
    return out


# Абсолютные полы. Ранг сам по себе развязку из ничего не делает: если
# цифр в сценарии нет вовсе, верхняя пятая часть по цифрам — это всё ещё
# ноль цифр, и называть такую долю развязкой нечестно.
FLOOR = dict(num=0.10, short=0.26, pause=0.26, you=0.08)

# Опорный балл завязки. Доля, которая ни по одному признаку не выделяется,
# должна остаться ровным куском — а не «слабейшей развязкой ролика».
# Число задаёт, насколько уверенно признак обязан выделяться, чтобы
# перебить это «ничем не выделяется».
SETUP_SCORE = 0.62

# Потолок на один тип. Ни один замысел не занимает больше 42% долей:
# ролик, у которого две трети — развязка, это ролик с одним темпом, то есть
# ровно то, от чего уходим. Первая версия классификатора отдавала развязке
# 86%, потому что сценарий целиком про деньги; потолок делает такой исход
# невозможным независимо от того, что написано в тексте.
KIND_CAP = 0.42

# Призыв живёт только в самом хвосте. Признак второго лица встречается в
# финальной главе не раз, и без ограничения по позиции призывом становилась
# вся глава — четыре минуты «обращения к зрителю» посреди рассказа.
CTA_TAIL = 0.88


def kind_scores(f: dict, rank: dict) -> dict:
    """
    Насколько доля похожа на каждый из замыслов. Баллы сравнимы между собой.

    Считается по рангам, то есть ОТНОСИТЕЛЬНО ОСТАЛЬНОГО ЭТОГО ЖЕ РОЛИКА.
    Абсолютные пороги здесь не работают в принципе: сценарий про находки
    целиком состоит из сумм, сценарий про войну — из дат, и любой
    абсолютный порог по цифрам срабатывает на них всюду.
    """
    s = {}

    # развязка: цифры плотнее обычного, диктор при этом не частит
    s[REVELATION] = (rank["num"] * 1.00 + (1 - rank["rate"]) * 0.40) / 1.40 \
        if f["num"] >= FLOOR["num"] else 0.0

    # нагнетание: рубленые фразы, слова-повороты, ускорение
    s[ESCALATION] = (rank["short"] * 1.00 + rank["turn"] * 0.45
                     + rank["rate"] * 0.35) / 1.80 \
        if f["short"] >= FLOOR["short"] else 0.0

    # выдох: длинные паузы, медленная речь, цифр меньше обычного
    s[REFLECTION] = (rank["pause"] * 1.00 + (1 - rank["rate"]) * 0.45
                     + (1 - rank["num"]) * 0.30) / 1.75 \
        if f["pause"] >= FLOOR["pause"] else 0.0

    s[SETUP] = SETUP_SCORE
    return s


def assign_kinds(raw, ranks, marks, total, last_block):
    """
    Раздаёт замыслы всем долям сразу, с потолком на каждый тип.

    Раздача идёт не по порядку долей, а по УВЕРЕННОСТИ: сначала те, что
    сильнее всего похожи на свой тип. Так самая цифровая доля ролика точно
    станет развязкой, даже если потолок на развязки к её очереди уже был бы
    выбран более слабыми претендентами.

    Крючок и призыв раздаются до всего: их определяет позиция, а не текст.
    """
    n = len(raw)
    out = [None] * n
    why = [""] * n
    keys = ("rate", "short", "num", "you", "q", "turn", "pause")

    # ── позиционные типы ──
    for i, (bi, lo, hi, f) in enumerate(raw):
        pos = marks[lo]["start"] / max(total, 0.001)
        if pos < 0.055:
            out[i], why[i] = HOOK, "начало ролика: зритель решает остаться здесь"
        elif bi == last_block and pos > CTA_TAIL:
            r = {k: ranks[k][i] for k in keys}
            if f["you"] >= FLOOR["you"] or f["q"] > 0.05:
                out[i], why[i] = CTA, (
                    f"хвост последней главы, второе лицо {f['you']:.2f}")
            else:
                out[i], why[i] = REFLECTION, "финал ролика: обязан замедлиться"

    # ── остальные: по уверенности, с потолком ──
    cap = max(2, int(n * KIND_CAP))
    counts = {}
    for k in out:
        if k:
            counts[k] = counts.get(k, 0) + 1

    pending = []
    for i in range(n):
        if out[i] is not None:
            continue
        f = raw[i][3]
        r = {k: ranks[k][i] for k in keys}
        sc = kind_scores(f, r)
        best = max(sc, key=sc.get)
        pending.append((sc[best] - SETUP_SCORE, i, sc, r, f))

    # сначала самые уверенные — у них приоритет на места под потолком
    pending.sort(reverse=True, key=lambda x: x[0])
    for _margin, i, sc, r, f in pending:
        for kind in sorted(sc, key=sc.get, reverse=True):
            if kind != SETUP and counts.get(kind, 0) >= cap:
                continue
            out[i] = kind
            counts[kind] = counts.get(kind, 0) + 1
            why[i] = _explain(kind, f, r)
            break
        if out[i] is None:
            out[i], why[i] = SETUP, "ровный кусок"
    return out, why


def _explain(kind, f, r):
    """Человеческое объяснение решения — уходит в монтажный лист."""
    if kind == REVELATION:
        return (f"цифры в верхних {(1 - r['num']) * 100:.0f}% ролика "
                f"({f['num']:.2f}), речь не частит")
    if kind == ESCALATION:
        return (f"короткие фразы, верхние {(1 - r['short']) * 100:.0f}% "
                f"ролика ({f['short']:.2f})")
    if kind == REFLECTION:
        return (f"паузы в верхних {(1 - r['pause']) * 100:.0f}% ролика "
                f"({f['pause']:.2f}), речь медленная")
    return "ровный кусок: ни один признак не выделяется"


# ─────────────────────────── РАЗБОР ───────────────────────────

# Целевая длина доли. Меньше 35 секунд дробить незачем — внутри доли и так
# работает кривая плотности; больше 150 значит, что глава на десять минут
# получит один режим на всю длину, а это ровно та ровность, от которой уходим.
BEAT_MIN_SECONDS = 35.0
BEAT_MAX_SECONDS = 150.0


def analyze(marks, script_blocks, total: float = None):
    """
    Сценарий и тайм-коды -> список долей.

    Двухуровневый разбор. Верхний уровень — блоки сценария, их границы
    поставил человек и трогать их нельзя: смена главы это всегда смена
    доли. Нижний — дробление длинного блока по самым длинным паузам внутри
    него, потому что глава на восемь минут содержит несколько поворотов,
    и монтировать её одним режимом — то же самое, что монтировать по таймеру.
    """
    if not marks:
        return []
    total = total or marks[-1]["end"]

    # средняя скорость речи по всему ролику — база для нормировки
    all_chars = sum(len(m.get("text", "")) for m in marks) or 1
    all_secs = sum(max(m["end"] - m["start"], 0.01) for m in marks) or 1.0
    mean_rate = all_chars / all_secs

    bounds = block_bounds(marks, script_blocks)
    bounds = sorted(set(b for b in bounds if 0 <= b < len(marks)))
    if not bounds or bounds[0] != 0:
        bounds = [0] + bounds

    # ПЕРВЫЙ ПРОХОД: границы и признаки. Классифицировать здесь ещё нельзя —
    # пороги ранговые, а ранг существует только относительно всех остальных
    # долей ролика, то есть считается, когда собраны все.
    raw = []
    for bi, lo in enumerate(bounds):
        hi = (bounds[bi + 1] - 1) if bi + 1 < len(bounds) else len(marks) - 1
        if hi < lo:
            continue
        for sub_lo, sub_hi in _split_block(marks, lo, hi):
            raw.append((bi, sub_lo, sub_hi,
                        features_of(marks, sub_lo, sub_hi, mean_rate)))
    if not raw:
        return []

    # ВТОРОЙ ПРОХОД: ранги признаков по ролику и классификация по ним.
    keys = ("rate", "short", "num", "you", "q", "turn", "pause")
    ranks = {k: ranks_of([f[k] for _b, _l, _h, f in raw]) for k in keys}

    last_block = max(b for b, _l, _h, _f in raw)
    kinds, whys = assign_kinds(raw, ranks, marks, total, last_block)

    beats = []
    for n, (bi, sub_lo, sub_hi, f) in enumerate(raw):
        beats.append(Beat(kind=kinds[n], start=marks[sub_lo]["start"],
                          end=marks[sub_hi]["end"], block=bi,
                          first_mark=sub_lo, last_mark=sub_hi,
                          features=f, reason=whys[n]))

    _enforce_variety(beats)
    # последняя доля дотягивается до конца звука: иначе хвост начитки
    # остаётся без плана и последний кадр обрывается раньше голоса
    if beats:
        beats[0].start = min(beats[0].start, marks[0]["start"])
        beats[-1].end = max(beats[-1].end, total)
    return beats


def _split_block(marks, lo, hi):
    """
    Режет длинный блок на доли РОВНО и по паузам одновременно.

    Первая версия просто брала самые длинные паузы. Это оказалось неверно,
    и неверно принципиально, а не по недосмотру: у критерия «самая длинная
    пауза» нет ничего, что тянуло бы резы к равномерности. Замер на
    ff-ep03 показал деление блока в 287 секунд на 162 + 82 + 42 — то есть
    первая доля втрое длиннее последней и держит один режим почти три
    минуты. На синтетике, где все паузы одинаковой длины, всё было ещё
    хуже: сортировка при равных паузах падала на порядок индексов и
    сдвигала все резы к концу блока.

    Правильный критерий — тот, которым пользуется живой монтажёр: делим
    примерно поровну, а границу двигаем к ближайшей ощутимой паузе.
    Поэтому каждый рез ищется В ОКНЕ вокруг своей идеальной позиции, и
    внутри окна побеждает самая длинная пауза.
    """
    span = marks[hi]["end"] - marks[lo]["start"]
    if span <= BEAT_MAX_SECONDS:
        return [(lo, hi)]

    n = max(2, int(round(span / max(BEAT_MAX_SECONDS * 0.72, 1.0))))
    t0 = marks[lo]["start"]
    piece = span / n

    def gap_after(i):
        if i + 1 > hi:
            return 0.0
        return max(0.0, marks[i + 1]["start"] - marks[i]["end"])

    # Окно поиска: ±35% от длины куска. Шире — резы снова разъезжаются,
    # уже — рез садится на первую попавшуюся границу предложения, даже
    # если в двух фразах от неё диктор молчит вчетверо дольше.
    window = piece * 0.35

    cuts = []
    for k in range(1, n):
        ideal = t0 + piece * k
        best, best_score = None, None
        for i in range(lo, hi):
            t = marks[i]["end"]
            if abs(t - ideal) > window:
                continue
            if t - t0 < BEAT_MIN_SECONDS:
                continue
            if marks[hi]["end"] - t < BEAT_MIN_SECONDS:
                continue
            if any(abs(t - marks[c]["end"]) < BEAT_MIN_SECONDS for c in cuts):
                continue
            # длинная пауза притягивает, отход от идеала отталкивает
            score = gap_after(i) - abs(t - ideal) / max(window, 0.1) * 0.25
            if best_score is None or score > best_score:
                best, best_score = i, score
        if best is not None:
            cuts.append(best)

    out, prev = [], lo
    for c in sorted(cuts):
        out.append((prev, c))
        prev = c + 1
    out.append((prev, hi))
    return [(a, b) for a, b in out if b >= a]


def _enforce_variety(beats):
    """
    Не даёт классификатору выродиться в одну завязку на весь ролик.

    Ситуация реальная: сценарий, написанный ровным повествовательным тоном,
    не даёт ни одного признака выше порога, и все доли выходят SETUP. План
    из одних завязок — это ровно та монотонность, ради ухода от которой
    разбор и делался, поэтому здесь работает страховка: если разнообразия
    нет, самые контрастные по признакам доли переводятся принудительно.

    Это НЕ подделка разбора. Доли переводятся не случайные, а те, что
    объективно лежат на краях распределения — самая цифровая становится
    развязкой, самая паузная выдохом. Просто порог для них опускается до
    «лучшая из имеющихся» вместо абсолютного.
    """
    if len(beats) < 4:
        return
    kinds = {b.kind for b in beats}
    if len(kinds) >= 3:
        return

    middle = [b for b in beats if b.kind == SETUP]
    if len(middle) < 3:
        return

    by_num = max(middle, key=lambda b: b.features["num"])
    if by_num.kind == SETUP:
        by_num.kind = REVELATION
        by_num.reason = f"самая цифровая доля ролика ({by_num.features['num']:.2f})"

    rest = [b for b in middle if b is not by_num]
    if rest:
        by_pause = max(rest, key=lambda b: b.features["pause"])
        by_pause.kind = REFLECTION
        by_pause.reason = (f"самая паузная доля ролика "
                           f"({by_pause.features['pause']:.2f})")
        rest = [b for b in rest if b is not by_pause]
    if rest:
        by_short = max(rest, key=lambda b: b.features["short"])
        by_short.kind = ESCALATION
        by_short.reason = (f"самые короткие фразы ролика "
                           f"({by_short.features['short']:.2f})")


def report(beats):
    """Строки для лога: сколько чего вышло и как оно легло по времени."""
    if not beats:
        return ["долей нет"]
    total = sum(b.duration for b in beats) or 1.0
    by_kind = {}
    for b in beats:
        by_kind[b.kind] = by_kind.get(b.kind, 0.0) + b.duration
    rows = [f"  {len(beats)} долей на {total/60:.1f} мин"]
    for k in ALL_KINDS:
        if k in by_kind:
            rows.append(f"    {k:<11} {by_kind[k]/total*100:5.1f}%  "
                        f"{by_kind[k]/60:5.1f} мин")
    return rows
