"""
pacing.py — кривые плотности склеек.

Что чинится
-----------
Старый движок считал длительность кадра одной формулой на весь ролик:

    dur = base * (1 + (decel - 1) * позиция_на_таймлайне)

Формула честная, но у неё ОДНА ФОРМА для всех роликов канала. Меняются
base и decel, а профиль остаётся монотонно возрастающим: у каждой загрузки
кадры равномерно удлиняются от начала к концу. Это и есть подпись шаблона —
её видно на графике длительностей, даже когда ролики отличаются цветом,
переходами и материалом.

Здесь профиль тоже выпадает жребием, но выпадает САМА ФОРМА КРИВОЙ, а не
числа на одной и той же. Плюс поверх лежит замысел доли из beats.py: внутри
нагнетания кадры укорачиваются, внутри развязки стоят долго, независимо от
того, какая кривая досталась ролику целиком.

Три уровня, перемножаются
-------------------------
  арка ролика     ARCS — как темп ходит от начала к концу (5 форм)
  замысел доли    INTENT в beats.py — во сколько раз доля быстрее среднего
  микроритм       кривая внутри доли (4 формы) плюс разброс

Ни один уровень не знает про другие, поэтому одинаковых сочетаний
практически не бывает: пять арок на четыре микроритма на шесть типов долей.
"""

import math

# ─────────────────────────── АРКИ РОЛИКА ───────────────────────────
#
# Функция от позиции 0..1 к множителю длительности кадра. Больше единицы —
# кадры длиннее (медленнее монтаж), меньше — короче.
#
# Названия музыкальные не для красоты: это ровно те формы, которыми монтажёр
# описывает темп вслух, и они разные НА ГРАФИКЕ, а не только по имени.


def _accelerando(p: float) -> float:
    """Начинается спокойно, к финалу частит. Классика расследования."""
    return 1.28 - 0.52 * p


def _decelerando(p: float) -> float:
    """Быстрый вход, ролик успокаивается. Старое поведение движка."""
    return 0.80 + 0.55 * p


def _wave(p: float) -> float:
    """Две волны напряжения и отпускания. Самая «живая» форма."""
    return 1.0 + 0.30 * math.sin(2 * math.pi * (p * 2.0 - 0.25))


def _front_loaded(p: float) -> float:
    """Резкий старт, плато, короткий подъём к финалу."""
    if p < 0.18:
        return 0.62 + 1.1 * p
    if p < 0.82:
        return 0.90 + 0.22 * (p - 0.18) / 0.64
    return 1.12 - 0.28 * (p - 0.82) / 0.18


def _terraced(p: float) -> float:
    """
    Ступени. Темп держится плато и меняется рывком на границах.

    Ступенчатая форма нужна в наборе отдельно от плавных: у неё производная
    рваная, и статистика длительностей у такого ролика непохожа ни на один
    ролик с гладкой аркой. Это самая дешёвая непохожесть из возможных.
    """
    step = int(p * 4.0)
    return [0.78, 1.06, 0.88, 1.24][min(step, 3)]


ARCS = {
    "accelerando": _accelerando,
    "decelerando": _decelerando,
    "wave": _wave,
    "front_loaded": _front_loaded,
    "terraced": _terraced,
}


# ─────────────────────────── МИКРОРИТМ ДОЛИ ───────────────────────────
#
# Функция от позиции ВНУТРИ доли 0..1. Задаёт, как темп ходит на своих
# полутора-двух минутах. Именно этот уровень отвечает за ощущение, что
# монтажёр «разгонялся» и «отпускал» — на масштабе ролика такое не видно.


def _flat(p: float) -> float:
    return 1.0


def _tighten(p: float) -> float:
    """Кадры укорачиваются к концу доли: доля «сжимается»."""
    return 1.16 - 0.34 * p


def _release(p: float) -> float:
    """Наоборот: доля отпускает к своему концу."""
    return 0.84 + 0.36 * p


def _arch(p: float) -> float:
    """Быстро на краях, длинно в середине. Под доли с одной главной мыслью."""
    return 0.82 + 0.42 * math.sin(math.pi * p)


MICRO = {
    "flat": _flat,
    "tighten": _tighten,
    "release": _release,
    "arch": _arch,
}

# Какой микроритм уместен какому замыслу. Жребий бросается ВНУТРИ списка,
# то есть выбор всё равно случайный, но не бессмысленный: нагнетание не
# может «отпускать», это противоречит самому слову.
MICRO_FOR = {
    "hook":       ["tighten", "flat"],
    "setup":      ["flat", "arch", "release"],
    "escalation": ["tighten", "tighten", "flat"],
    "revelation": ["arch", "flat"],
    "reflection": ["release", "flat", "arch"],
    "cta":        ["release", "flat"],
}


# ─────────────────────────── ВЫДОХИ ───────────────────────────
#
# Намеренно длинный кадр, поставленный не потому, что так вышло по формуле,
# а потому что после сильного куска зрителю нужно молчание. Это самый
# «человеческий» приём во всём наборе и самый заметный: ролик, где ни один
# кадр не выбивается из ритма, читается как автоматический даже при
# идеальном разнообразии всего остального.

BREATH_AFTER = ("revelation", "escalation")   # после чего ставить выдох
BREATH_MULTIPLIER = (1.9, 3.2)                # во сколько раз длиннее среднего


class Pacing:
    """
    Готовая кривая темпа для конкретного ролика.

    Собирается один раз при создании и дальше только отвечает на вопрос
    «сколько секунд просить под кадр, который начинается в такую-то секунду
    такой-то доли». Все решения, которые она приняла, лежат в .decisions —
    оттуда их забирает монтажный лист.
    """

    def __init__(self, rng, beats, total: float, base: float,
                 arc_name: str = None, breath_rate: float = 0.55):
        self.rng = rng
        self.total = max(total, 0.001)
        self.base = base
        self.arc_name = arc_name or rng.choice(list(ARCS))
        self.arc = ARCS[self.arc_name]
        self.breath_rate = breath_rate

        # микроритм своей доле — жребий из разрешённых замыслу
        self.micro_of = {}
        for i, b in enumerate(beats):
            pool = MICRO_FOR.get(b.kind, ["flat"])
            self.micro_of[i] = rng.choice(pool)

        # где стоят выдохи. Не после каждой сильной доли: выдох, стоящий
        # всегда, перестаёт быть приёмом и становится ритмом.
        self.breaths = set()
        for i, b in enumerate(beats[:-1]):
            if b.kind in BREATH_AFTER and rng.random() < breath_rate:
                self.breaths.add(i + 1)      # первый кадр следующей доли

        self.decisions = dict(arc=self.arc_name,
                              micro={i: m for i, m in self.micro_of.items()},
                              breaths=sorted(self.breaths))

    def want(self, beat_index: int, beat, t: float, spread: float = None):
        """
        Сколько секунд просить под кадр, начинающийся в секунду t.

        Возвращает (секунды, пояснение). Пояснение уходит в монтажный лист:
        доказывать осмысленность решений проще всего, когда каждое решение
        само себя объясняет.
        """
        intent = beat.intent
        p_video = min(1.0, max(0.0, t / self.total))
        p_beat = min(1.0, max(0.0, (t - beat.start) / beat.duration))

        micro_name = self.micro_of.get(beat_index, "flat")
        k_arc = self.arc(p_video)
        k_micro = MICRO[micro_name](p_beat)
        k_intent = intent["tempo"]

        dur = self.base * k_arc * k_micro * k_intent
        sp = intent["spread"] if spread is None else spread
        dur *= 1 + self.rng.uniform(-sp, sp)

        why = (f"{beat.kind}×{k_intent:.2f} · арка {self.arc_name}×{k_arc:.2f} "
               f"· {micro_name}×{k_micro:.2f}")

        # выдох: первый кадр доли после сильной держится намеренно долго
        if beat_index in self.breaths and abs(t - beat.start) < 0.05:
            mult = self.rng.uniform(*BREATH_MULTIPLIER)
            dur *= mult
            why += f" · ВЫДОХ ×{mult:.1f}"

        return dur, why

    def clip_share(self, beat) -> float:
        """Какая доля слотов этой доли уместна под стоковое видео."""
        return beat.intent["clip_share"]

    def motion_scale(self, beat) -> float:
        """Множитель амплитуды движения камеры в этой доле."""
        return beat.intent["motion"]

    def report(self):
        rows = [f"  арка темпа: {self.arc_name}"]
        if self.breaths:
            rows.append(f"  выдохов: {len(self.breaths)}")
        counts = {}
        for m in self.micro_of.values():
            counts[m] = counts.get(m, 0) + 1
        rows.append("  микроритм: " + ", ".join(
            f"{k}×{v}" for k, v in sorted(counts.items())))
        return rows
