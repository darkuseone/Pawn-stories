"""
variation.py — ВЕКТОР СТИЛЯ ролика.

Идея
----
Стиль ролика описывается набором независимых осей: арка темпа, основной
переход, тип открытия, вероятность эффекта, амплитуда камеры, доля стока и
так далее. Каждая ось — это ручка, которую можно повернуть, не трогая
остальные.

Зачем такое усложнение вместо россыпи полей, как было раньше: пока стиль
это два десятка отдельных атрибутов, нельзя ответить на вопрос «насколько
этот ролик отличается от предыдущего». А ответить надо — иначе защита от
повторов сводится к «не бери тот же цветокор», и ролики, различающиеся
одним цветом, спокойно проходят проверку.

Вектор считается, сравнивается и хранится целиком. memory.py умеет мерить
расстояние между двумя векторами и требовать, чтобы оно было не меньше
заданного. Это и есть требование «минимум 3-4 значимых отличия подряд»:
не пожелание в комментарии, а условие, которое цикл пересдачи обязан
выполнить, прежде чем вернуть стиль.

Воспроизводимость
-----------------
Жребий детерминирован: seed из id ролика. Тот же id даёт тот же вектор,
поэтому рендер можно перезапустить и получить тот же ролик — это нужно и
для отладки, и для пересборки монтажа из кэша. Разведение с историей канала
меняет вектор предсказуемо: см. memory.draw_diverged.
"""

import hashlib
import random

# ─────────────────────────── ОСИ ───────────────────────────
#
# Каждая ось: как её тянуть жребием и как считать расстояние между двумя
# значениями. Расстояние нормировано к 0..1, где 1 — «совсем другое».
#
# kind:
#   pick   выбор из списка. Расстояние 0 или 1, промежуточного нет
#   num    число из диапазона. Расстояние — доля от ширины диапазона
#   pair   диапазон (низ, верх), тянется как два числа

AXES = {
    # ── ритм ──────────────────────────────────────────────────────────
    "arc":              dict(kind="pick", weight=1.6,
                             options=["accelerando", "decelerando", "wave",
                                      "front_loaded", "terraced"]),
    "base_duration":    dict(kind="num", weight=1.2, lo=4.2, hi=7.4),
    "breath_rate":      dict(kind="num", weight=0.8, lo=0.30, hi=0.85),
    "shot_spread":      dict(kind="num", weight=0.7, lo=0.16, hi=0.34),

    # ── открытие ──────────────────────────────────────────────────────
    # Самая заметная зрителю ось: первые пять секунд решают, останется он
    # или нет. Вариантов намеренно больше, чем было (три): при трёх
    # вариантах и глубине памяти два выбирать становится не из чего.
    "opening":          dict(kind="pick", weight=2.0,
                             options=["cold_open", "long_establish",
                                      "quick_cuts", "black_card",
                                      "slow_reveal", "hard_in"]),
    "opening_seconds":  dict(kind="num", weight=0.9, lo=95.0, hi=235.0),

    # ── склейка ───────────────────────────────────────────────────────
    "main_transition":  dict(kind="pick", weight=1.4,
                             options=["smoothleft", "smoothright", "smoothup",
                                      "smoothdown", "dissolve", "hblur",
                                      "circleopen", "fadegrays", "fade",
                                      "fadeslow", "slideleft", "slideright",
                                      "wipeleft"]),
    # Насколько основной переход доминирует. Раньше было жёстко 0.72 у всех
    # роликов — то есть «почерк склейки» повторялся от загрузки к загрузке
    # с точностью до процента, даже когда сам переход менялся.
    "transition_focus": dict(kind="num", weight=1.1, lo=0.38, hi=0.74),
    "transition_dur":   dict(kind="pair", weight=0.8, lo=0.55, hi=1.95,
                             span=(0.35, 0.95)),

    # ── камера ────────────────────────────────────────────────────────
    "motion_amp":       dict(kind="num", weight=1.3, lo=0.72, hi=1.30),
    "motion_bias":      dict(kind="pick", weight=1.0,
                             options=["push", "pan", "mixed", "mixed",
                                      "sweep", "still_leaning"]),

    # ── фактура ───────────────────────────────────────────────────────
    "effect_p":         dict(kind="num", weight=1.0, lo=0.12, hi=0.42),
    "grain":            dict(kind="num", weight=0.6, lo=4.0, hi=12.0),
    # Диапазон — делитель в выражении render.py: vignette=PI/значение, то
    # есть МЕНЬШЕ число здесь = СИЛЬНЕЕ виньетка. Нижняя граница поднята с
    # 0.0 до 3.4 не для красоты: на ff-ep05 жребий выбрал 1.82, что дало
    # PI/1.82 ≈ 99° угла и половину кадра чёрным по краю — измерено прямым
    # рендером тестового паттерна, 53% пикселей темнее 15/255. У старой
    # (дожребиевой) версии стиля пул был {0.0, 3.6, 4.6, 5.6}: максимум
    # затемнения там — 2.7% на 3.6. Нижний пол здесь подобран тем же
    # замером, чтобы новый диапазон не отличался от проверенного по силе
    # эффекта, только по разнообразию внутри него.
    "vignette":         dict(kind="num", weight=0.6, lo=3.4, hi=6.0),
    "sparks":           dict(kind="pick", weight=0.9,
                             options=[0, 1, 2, 3]),
    "spark_opacity":    dict(kind="num", weight=0.5, lo=0.28, hi=0.52),

    # ── материал ──────────────────────────────────────────────────────
    "clip_rhythm":      dict(kind="num", weight=1.1, lo=2.4, hi=4.6),
    "framing_bias":     dict(kind="pick", weight=0.9,
                             options=["wide_leaning", "tight_leaning",
                                      "balanced", "balanced"]),

    # ── типографика ───────────────────────────────────────────────────
    # Кинетический текст на числах. Ось нужна отдельно: ролик без единой
    # плашки и ролик с плашкой на каждой сумме — это разный монтаж, а не
    # разное оформление.
    "text_style":       dict(kind="pick", weight=1.5,
                             options=["none", "none", "stamp", "slide_up",
                                      "typewriter", "underline_wipe"]),
    "text_density":     dict(kind="num", weight=0.8, lo=0.15, hi=0.65),

    # ── звук ──────────────────────────────────────────────────────────
    "duck_depth":       dict(kind="num", weight=0.9, lo=0.30, hi=0.72),
    "duck_style":       dict(kind="pick", weight=1.0,
                             options=["revelation", "beats", "sparse",
                                      "breath"]),
}


def seed_from(video_id: str) -> int:
    return int(hashlib.sha256(video_id.encode()).hexdigest()[:12], 16)


def draw(video_id: str, salt: int = 0) -> dict:
    """
    Бросает вектор стиля. salt сдвигает жребий, не ломая воспроизводимость:
    memory.py пересдаёт с растущим salt, пока не разведёт ролик с историей.
    """
    rng = random.Random(seed_from(video_id) + salt * 7919)
    out = {}
    for name, spec in AXES.items():
        if spec["kind"] == "pick":
            out[name] = rng.choice(spec["options"])
        elif spec["kind"] == "num":
            out[name] = round(rng.uniform(spec["lo"], spec["hi"]), 4)
        elif spec["kind"] == "pair":
            width = rng.uniform(*spec["span"])
            lo = rng.uniform(spec["lo"], max(spec["lo"], spec["hi"] - width))
            out[name] = (round(lo, 3), round(min(lo + width, spec["hi"]), 3))
    return out


def axis_distance(name: str, a, b) -> float:
    """Расстояние по одной оси, 0..1."""
    spec = AXES.get(name)
    if spec is None or a is None or b is None:
        return 0.0
    if spec["kind"] == "pick":
        return 0.0 if a == b else 1.0
    if spec["kind"] == "num":
        span = max(spec["hi"] - spec["lo"], 1e-9)
        return min(1.0, abs(float(a) - float(b)) / span)
    if spec["kind"] == "pair":
        span = max(spec["hi"] - spec["lo"], 1e-9)
        da = abs(float(a[0]) - float(b[0])) / span
        db = abs(float(a[1]) - float(b[1])) / span
        return min(1.0, (da + db) / 2)
    return 0.0


# Ось считается «значимо другой» с этого расстояния. Для списочных осей это
# всегда либо 0, либо 1, порог их не касается. Для числовых 0.34 значит:
# сдвинулось на треть своего диапазона — то есть заметно на глаз, а не в
# четвёртом знаке.
SIGNIFICANT = 0.34


def differences(a: dict, b: dict) -> list:
    """Имена осей, по которым два вектора значимо расходятся."""
    return [n for n in AXES if axis_distance(n, a.get(n), b.get(n)) >= SIGNIFICANT]


def distance(a: dict, b: dict) -> float:
    """
    Взвешенное расстояние между векторами, 0..1.

    Взвешенное, потому что оси неравноценны: тип открытия зритель замечает
    сразу, а разницу в зерне на единицу не замечает никогда. Мерить их
    одинаково значит получать «непохожие» ролики, отличающиеся зерном.
    """
    num = sum(AXES[n]["weight"] * axis_distance(n, a.get(n), b.get(n))
              for n in AXES)
    den = sum(AXES[n]["weight"] for n in AXES) or 1.0
    return num / den


def describe(vec: dict) -> dict:
    """Вектор в виде, пригодном для журнала канала и для монтажного листа."""
    out = {}
    for name, val in vec.items():
        out[name] = list(val) if isinstance(val, tuple) else val
    return out
