"""
memory.py — память канала и ПРИНУДИТЕЛЬНОЕ РАЗВЕДЕНИЕ роликов.

Чем отличается от channel.py
----------------------------
channel.py ведёт журнал: что выходило, с каким цветокором, какая тема. Он
умеет сказать «этот LUT был в прошлый раз» и «тема пересекается на 40%».
Этого мало по одной причине: он проверяет ОТДЕЛЬНЫЕ ПОЛЯ, а похожесть
роликов — свойство их сочетания. Два ролика с разными цветокорами, но с
одинаковой аркой темпа, одинаковым открытием и одинаковой плотностью
склеек — это два одинаковых ролика, и ни одна поосевая проверка этого не
скажет.

Здесь работа идёт с вектором целиком (variation.py). Вектор бросается,
сравнивается с последними N роликами канала, и если он к ним слишком
близок — БРОСАЕТСЯ ЗАНОВО с другой солью. Цикл крутится, пока не выполнено
условие: минимум MIN_DIFFERENCES значимых отличий от каждого из последних
роликов и общее расстояние не меньше MIN_DISTANCE.

Почему пересдача, а не правка
-----------------------------
Можно было бы взять близкий вектор и подкрутить в нём три оси до нужного
расстояния. Так делать нельзя: подкрутка идёт по фиксированному правилу,
и через десяток роликов канал получает узнаваемый узор «эти три оси всегда
на краю диапазона». Пересдача целиком оставляет вектор честно случайным.

Сходимость
----------
Цикл ограничен MAX_TRIES. Если за столько попыток развести не удалось —
это не ошибка, а сигнал: пул вариантов исчерпан относительно глубины
памяти. Тогда берётся самый далёкий из виденных вариантов и в лог уходит
внятное предупреждение. Ронять сборку здесь нельзя: ролик собрать надо, а
разнообразие — цель, а не условие выпуска.
"""

import json
from pathlib import Path

from . import variation

ROOT = Path(__file__).parent.parent.parent
LOG = ROOT / "channel" / "log.json"

# Сколько последних роликов учитывать. Глубже смысла нет: зритель сравнивает
# соседние загрузки, а не двадцатую с первой. Плюс чем глубже память, тем
# быстрее исчерпывается пул и тем чаще срабатывает аварийный выход.
DEPTH = 12

# Минимум значимых отличий от КАЖДОГО из последних роликов. Четыре — это
# требование задания; в коде оно стоит как условие цикла, а не как надежда.
MIN_DIFFERENCES = 4

# Минимальное взвешенное расстояние до КАЖДОГО из последних. 0.28 подобрано
# по журналу: ниже этого числа ролики читаются как пара даже при формально
# разных полях.
MIN_DISTANCE = 0.28

# Ближайшие соседи проверяются строже: от прошлой загрузки ролик обязан
# отличаться сильнее, чем от позапрошлой.
NEIGHBOUR_BONUS = {0: 0.10, 1: 0.05}

MAX_TRIES = 240


def load_log():
    if not LOG.exists():
        return {"aired": []}
    try:
        return json.loads(LOG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"aired": []}


def recent_vectors(video_id: str = None, depth: int = DEPTH):
    """
    Векторы последних выложенных роликов, новейший первым.

    Свой собственный id пропускается: пересборка ролика не должна разводить
    его сам с собой — иначе каждая пересборка давала бы другой монтаж, и
    кэш готовых кадров обесценивался бы целиком.

    Записи старого формата (без вектора) пропускаются молча: журнал вёлся
    до появления этого модуля, и требовать от него нового поля нельзя.
    """
    rows = [e for e in load_log().get("aired", []) if not e.get("test")]
    out = []
    for e in reversed(rows):
        if video_id and e.get("id") == video_id:
            continue
        vec = e.get("style_vector")
        if isinstance(vec, dict) and vec:
            out.append((e.get("id", "?"), _retuple(vec)))
        if len(out) >= depth:
            break
    return out


def _retuple(vec: dict) -> dict:
    """JSON не знает кортежей: диапазоны приезжают списками, чиним."""
    out = {}
    for k, v in vec.items():
        spec = variation.AXES.get(k)
        if spec and spec["kind"] == "pair" and isinstance(v, list) and len(v) == 2:
            out[k] = (v[0], v[1])
        else:
            out[k] = v
    return out


def _ok_against(vec, history):
    """
    Развёден ли вектор с историей. Возвращает (да/нет, что мешает).

    Проверяются оба условия сразу: и число значимых отличий, и взвешенное
    расстояние. Только числа отличий мало — четыре оси могут разойтись на
    мелочах при общем сходстве; только расстояния мало — оно набирается
    двумя сильными осями при полном совпадении остальных.
    """
    for i, (vid, old) in enumerate(history):
        need_dist = MIN_DISTANCE + NEIGHBOUR_BONUS.get(i, 0.0)
        diffs = variation.differences(vec, old)
        dist = variation.distance(vec, old)
        if len(diffs) < MIN_DIFFERENCES:
            return False, f"с {vid} совпадает слишком многое ({len(diffs)} отличий)"
        if dist < need_dist:
            return False, f"до {vid} расстояние {dist:.3f} < {need_dist:.2f}"
    return True, ""


def draw_diverged(video_id: str, depth: int = DEPTH, log=print):
    """
    Вектор стиля, разведённый с последними роликами канала.

    Возвращает (вектор, отчёт). Отчёт уходит в лог и в монтажный лист: там
    видно, сколько попыток понадобилось и чем этот ролик отличается от
    предыдущего. Это же — готовое доказательство, что разнообразие не
    декларируется, а проверяется на каждой сборке.
    """
    history = recent_vectors(video_id, depth)
    if not history:
        vec = variation.draw(video_id, 0)
        return vec, dict(tries=1, history=0,
                         note="журнал пуст — разводить не с чем")

    best, best_score, best_salt = None, -1.0, 0
    for salt in range(MAX_TRIES):
        vec = variation.draw(video_id, salt)
        ok, why = _ok_against(vec, history)
        # запоминаем лучший на случай, если развести так и не выйдет:
        # score — расстояние до БЛИЖАЙШЕГО соседа, его и максимизируем
        score = min(variation.distance(vec, old) for _vid, old in history)
        if score > best_score:
            best, best_score, best_salt = vec, score, salt
        if ok:
            prev_id, prev = history[0]
            diffs = variation.differences(vec, prev)
            return vec, dict(
                tries=salt + 1, history=len(history), salt=salt,
                distance_to_prev=round(variation.distance(vec, prev), 3),
                differs_from_prev=diffs,
                note=f"разведён с {len(history)} последними за {salt + 1} попыток")

    # Пул исчерпан. Это не ошибка — это сообщение о том, что глубина памяти
    # подошла к числу различимых сочетаний. Берём самый далёкий вариант.
    prev_id, prev = history[0]
    log(f"  ! не удалось развести за {MAX_TRIES} попыток — беру самый далёкий "
        f"(расстояние до ближайшего {best_score:.3f}); "
        f"уменьши DEPTH в editorial/memory.py или добавь вариантов в оси")
    return best, dict(tries=MAX_TRIES, history=len(history), salt=best_salt,
                      distance_to_prev=round(variation.distance(best, prev), 3),
                      differs_from_prev=variation.differences(best, prev),
                      note="пул исчерпан, взят самый далёкий вариант")


# ─────────────────────── ПАМЯТЬ НА МАТЕРИАЛ ───────────────────────

def used_assets(depth: int = DEPTH) -> dict:
    """
    Какие файлы материала уже шли в эфир и сколько раз.

    Зачем: пул стока и архива у канала общий, и один и тот же клип, годный
    по теме, будет выигрывать подбор в каждом ролике подряд. Внутри ролика
    от этого спасает износ в ShotPicker, между роликами — не спасает ничто.
    Здесь счётчик переживает ролик: файл, отработавший в прошлой загрузке,
    начинает следующую с гандикапом.

    Ключ — ИМЯ ФАЙЛА, а не путь: у каждого ролика своя папка work/<id>, и
    полные пути между роликами не совпадают никогда.
    """
    rows = [e for e in load_log().get("aired", []) if not e.get("test")]
    out = {}
    for e in list(reversed(rows))[:depth]:
        for name, times in (e.get("assets_used") or {}).items():
            out[name] = out.get(name, 0) + int(times)
    return out
