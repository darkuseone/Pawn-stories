"""
rails.py — ПРОВЕРКА ГОТОВОГО ПЛАНА на признаки шаблона.

Зачем отдельный слой
--------------------
Разнообразие, заложенное в генератор, и разнообразие, дошедшее до готового
плана, — разные вещи. Между ними стоит материал: когда стока мало, а
архива много, план схлопывается в череду фотографий с одинаковым наездом
независимо от того, какой богатый вектор стиля ему достался. Генератор
при этом отработал честно, и в логе всё хорошо.

Поэтому проверка идёт ПО ФАКТУ, на уже разложенном плане, и меряет ровно
то, что увидит зритель: длительности, движения, переходы, чередование
материала. Все проверки — числовые, ни одна не полагается на «должно быть».

Что здесь НЕ делается
---------------------
Аудит ничего не чинит и ничего не роняет. Он печатает диагноз. Причина:
автоматическая правка плана под метрику даёт ролик, оптимизированный под
метрику, — то есть новый шаблон, просто более хитрый. Решение, что делать
с замечанием, принимает человек: добрать материал, поменять спецификацию
или согласиться.

Единственное исключение — MONOTONY_HARD: слайдшоу из одинаковых кадров с
одинаковым ходом камеры. Это состояние, при котором ролик выкладывать
нельзя, и о нём сообщается отдельной строкой.
"""

import statistics
from collections import Counter

# ── пороги ───────────────────────────────────────────────────────────
#
# Все подобраны на реальных сборках канала, а не назначены. Рядом с каждым
# указано, что именно им ловится.

# Коэффициент вариации длительностей кадра. Ниже 0.22 — это нарезка по
# секундомеру: у живого монтажа разброс всегда больше, потому что кадр
# тянется до конца фразы, а фразы разной длины.
CV_MIN = 0.22

# Сколько подряд кадров-картинок допустимо без единой вставки видео.
# Больше — зритель читает это как слайдшоу, чем бы оно ни было по факту.
MAX_IMAGE_RUN = 7

# Сколько подряд кадров может идти с движением одного НАПРАВЛЕНИЯ (наезд,
# панорама влево и так далее). Три одинаковых подряд — уже узор.
MAX_SAME_MOVE_RUN = 3

# Доля самого частого перехода. Выше — «почерк» склейки становится
# единственным, и ролик опознаётся по нему с любого места.
MAX_TRANSITION_SHARE = 0.78

# Разброс скоростей движения камеры. Ровно одинаковая скорость на всех
# кадрах — самый заметный признак генератора слайдшоу.
SPEED_STDEV_MIN = 0.06

# Доля самой частой длительности (с округлением до полусекунды). Выше —
# кадры нарезаны по сетке.
MAX_DURATION_MODE_SHARE = 0.30

# Аварийный порог: и разброс мёртвый, и движение одно, и вставок нет.
MONOTONY_HARD = "СЛАЙДШОУ"


class Finding:
    __slots__ = ("level", "code", "text")

    def __init__(self, level, code, text):
        self.level = level          # "стоп" | "внимание" | "заметка"
        self.code = code
        self.text = text

    def __str__(self):
        return f"[{self.level}] {self.code}: {self.text}"


def audit(shots, style_vector=None, beats=None):
    """
    Смотрит готовый план и возвращает список замечаний.

    Пустой список — план прошёл. Это не гарантия качества, это отсутствие
    известных признаков шаблона.
    """
    out = []
    if not shots:
        return [Finding("стоп", "ПУСТО", "план пуст")]

    durs = [s["duration"] for s in shots]
    out += _check_duration_spread(durs)
    out += _check_material_runs(shots)
    out += _check_moves(shots)
    out += _check_transitions(shots)
    out += _check_slideshow(shots)
    if beats:
        out += _check_beat_coverage(shots, beats)
    return out


def _check_duration_spread(durs):
    if len(durs) < 6:
        return []
    mean = statistics.fmean(durs)
    if mean <= 0:
        return []
    cv = statistics.pstdev(durs) / mean
    out = []
    if cv < CV_MIN:
        out.append(Finding(
            "внимание", "РОВНЫЙ_РИТМ",
            f"разброс длительностей {cv:.3f} при минимуме {CV_MIN} — "
            f"кадры почти одинаковой длины, это читается как нарезка по "
            f"таймеру. Обычно значит, что доли из beats.py не разошлись: "
            f"проверь строку «долей» выше в логе"))

    # мода длительностей с округлением до полусекунды
    rounded = Counter(round(d * 2) / 2 for d in durs)
    top, times = rounded.most_common(1)[0]
    share = times / len(durs)
    if share > MAX_DURATION_MODE_SHARE:
        out.append(Finding(
            "внимание", "СЕТКА",
            f"{share*100:.0f}% кадров длиной ровно {top} с — длительности "
            f"легли на сетку. Смотри shot_spread в векторе стиля"))
    return out


def _check_material_runs(shots):
    out, run, worst = [], 0, 0
    for s in shots:
        if s["kind"] == "clip":
            run = 0
        else:
            run += 1
            worst = max(worst, run)
    if worst > MAX_IMAGE_RUN:
        out.append(Finding(
            "внимание", "ДЛИННЫЙ_РЯД_ФОТО",
            f"{worst} кадров-картинок подряд без единой вставки видео "
            f"(потолок {MAX_IMAGE_RUN}) — участок читается как слайдшоу. "
            f"Добери сток этапом material либо опусти clip_rhythm"))
    return out


def _check_moves(shots):
    out = []
    images = [s for s in shots if s["kind"] != "clip" and s.get("move")]
    if len(images) < 6:
        return out

    # направление важнее конкретного имени: push_in и push_left это оба
    # наезда, и три наезда подряд выглядят одинаково, как ни называй
    def family(name):
        for f in ("push", "pull", "pan", "tilt", "sweep", "drift", "hold"):
            if name.startswith(f):
                return f
        return name

    run, worst, prev = 1, 1, None
    for s in images:
        fam = family(s["move"])
        run = run + 1 if fam == prev else 1
        prev = fam
        worst = max(worst, run)
    if worst > MAX_SAME_MOVE_RUN:
        out.append(Finding(
            "внимание", "ОДНО_ДВИЖЕНИЕ",
            f"{worst} кадров подряд с движением одного типа "
            f"(потолок {MAX_SAME_MOVE_RUN})"))

    speeds = [s.get("speed", 1.0) for s in images]
    if len(speeds) > 5:
        sd = statistics.pstdev(speeds)
        if sd < SPEED_STDEV_MIN:
            out.append(Finding(
                "стоп", "ОДНА_СКОРОСТЬ",
                f"разброс скоростей камеры {sd:.4f} при минимуме "
                f"{SPEED_STDEV_MIN} — все наезды идут с одной скоростью. "
                f"Это ровно тот признак, по которому слайдшоу опознаётся "
                f"автоматически"))
    return out


def _check_transitions(shots):
    if len(shots) < 8:
        return []
    c = Counter(s.get("transition", "?") for s in shots)
    top, times = c.most_common(1)[0]
    share = times / len(shots)
    if share > MAX_TRANSITION_SHARE:
        return [Finding(
            "внимание", "ОДИН_ПЕРЕХОД",
            f"переход «{top}» стоит в {share*100:.0f}% склеек "
            f"(потолок {MAX_TRANSITION_SHARE*100:.0f}%) — опусти "
            f"transition_focus в векторе стиля")]
    return []


def _check_slideshow(shots):
    """
    Аварийная проверка: совпало всё сразу.

    По отдельности каждый из признаков — замечание. Вместе они означают
    ролик, который нельзя выкладывать: череда картинок с одинаковым
    медленным наездом и одинаковым растворением. Именно такое сочетание
    площадка и описывает словом «шаблонное».
    """
    images = [s for s in shots if s["kind"] != "clip"]
    if len(shots) < 10 or len(images) / len(shots) < 0.92:
        return []
    durs = [s["duration"] for s in shots]
    mean = statistics.fmean(durs) or 1.0
    cv = statistics.pstdev(durs) / mean
    moves = Counter(s.get("move") for s in images)
    speeds = [s.get("speed", 1.0) for s in images]
    if (cv < CV_MIN * 0.8
            and moves.most_common(1)[0][1] / len(images) > 0.5
            and statistics.pstdev(speeds) < SPEED_STDEV_MIN):
        return [Finding(
            "стоп", MONOTONY_HARD,
            "план состоит почти целиком из картинок с одним движением, "
            "одной скоростью и ровными длительностями. Выкладывать нельзя: "
            "добери сток и архив этапом material")]
    return []


def _check_beat_coverage(shots, beats):
    """
    Все ли доли получили монтаж, соответствующий замыслу.

    Ловит расхождение между тем, что задумал beats.py, и тем, что вышло:
    например, доля-развязка, которой не досталось ни одного длинного кадра,
    потому что весь длинный материал кончился раньше.
    """
    out = []
    for b in beats:
        inside = [s for s in shots
                  if b.start - 0.01 <= s["start"] < b.end - 0.01]
        if not inside:
            continue
        mean = statistics.fmean(s["duration"] for s in inside)
        want = b.intent["tempo"]
        # у развязки кадры обязаны быть заметно длиннее среднего по ролику
        if b.kind == "revelation" and len(inside) >= 3:
            overall = statistics.fmean(s["duration"] for s in shots)
            if mean < overall * 1.10:
                out.append(Finding(
                    "заметка", "РАЗВЯЗКА_БЕЗ_ПАУЗЫ",
                    f"доля-развязка на {b.start/60:.1f} мин смонтирована не "
                    f"медленнее ролика ({mean:.1f} с против {overall:.1f} с) — "
                    f"обычно значит, что длинных кадров не хватило материала"))
    return out


def report(findings, log=print):
    """Печатает замечания и возвращает True, если есть уровня «стоп»."""
    if not findings:
        log("  проверка плана: замечаний нет")
        return False
    hard = False
    for f in findings:
        log(f"  {f}")
        hard = hard or f.level == "стоп"
    return hard


def metrics(shots):
    """
    Числа, которые стоит хранить в журнале канала.

    По ним видно дрейф: если у пяти роликов подряд одинаковый разброс
    длительностей, генератор где-то заклинило, и никакая поосевая проверка
    этого не покажет.
    """
    if not shots:
        return {}
    durs = [s["duration"] for s in shots]
    mean = statistics.fmean(durs)
    images = [s for s in shots if s["kind"] != "clip"]
    speeds = [s.get("speed", 1.0) for s in images] or [1.0]
    trans = Counter(s.get("transition", "?") for s in shots)
    return dict(
        shots=len(shots),
        mean_duration=round(mean, 2),
        cv_duration=round(statistics.pstdev(durs) / mean, 3) if mean else 0.0,
        clip_share=round(1 - len(images) / len(shots), 3),
        speed_stdev=round(statistics.pstdev(speeds), 4),
        top_transition=trans.most_common(1)[0][0],
        top_transition_share=round(
            trans.most_common(1)[0][1] / len(shots), 3),
        distinct_transitions=len(trans),
        distinct_moves=len({s.get("move") for s in images if s.get("move")}),
    )
