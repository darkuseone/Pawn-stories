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
libass для шапки и субтитров одним файлом. Шапка — то же стекло
(glow/edge/fill), что название и итог в длинном ролике; жёлтого в шортсе
по-прежнему нет, субтитры остаются непрозрачными — Oswald, белый, чёрная
обводка. Вступление вопроса: punch 118%→100%, уезжает наверх. Кусок не
стартует с обрывка предложения. Полоса вжжённых субтитров длинного ролика,
которая иначе просвечивала бы под шапкой шортса вторым, чужим субтитром,
смазывается region-blur'ом до наложения ass (см. render_short).


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
# ЧЕТЫРЕ ШОРТСА С РОЛИКА, а не два. Решение канала: шортс — основной
# источник новых зрителей, а режется он из УЖЕ отрендеренного ролика, то
# есть стоит секунды ffmpeg, а не отдельный прогон. Двойка была зашита в
# трёх местах сразу (want здесь, срез [:2] в youtube.short_titles, проверка
# «ровно два» в smoke) — поэтому и вынесена в одно число.
SHORT_COUNT = 4

SHORT_MIN = 24.0
SHORT_MAX = 52.0

# Шапка с вопросом: блок жмётся к тексту (см. header_layout), но
# начинается с этого отступа от края кадра.
BOX_MARGIN = 36

# Субтитры кусками фразы, а не по одному слову.
SUB_MAX_CHARS = 20
SUB_MAX_LINES = 2
# Кегль 140, а не 70 — вдвое, как и в длинном ролике: подпись в шортсе
# читают с телефона в ленте, и мелкая там проигрывает всему остальному в
# кадре. fit_size ниже ужмёт его обратно, если строка не влезает.
SUB_SIZE = 140
SUB_MARGIN = 60            # поля стиля SUB, они же предел ширины строки

# ── КОМПОНОВКА ВЕРТИКАЛЬНОГО КАДРА ────────────────────────────────────
#
# ШИРОКИЙ КАДР НЕ РАСТЯГИВАЕТСЯ НА ВЕСЬ ЭКРАН. Раньше из 1920x1080
# вырезалась центральная колонка шириной 608 px и разгонялась до 1080 —
# апскейл в 1.78 раза по узкой полоске. Результат был мыльный, а две
# трети композиции уезжали за края: в кадре оставалась случайная
# вертикальная полоса вместо снятого плана. Теперь кадр вписывается
# ЦЕЛИКОМ по ширине (это даунскейл, качество только выигрывает), а
# пустота сверху и снизу занята сильно размытой копией того же кадра —
# и там же живут вопрос и субтитры, то есть место не пропадает.
FRAME_W = 1080

# Из 16:9 берём центральный кроп 4:3: 1440x1080 -> 1080x810, масштаб 0.75.
# Полностью вписанный 16:9 дал бы кадр высотой 608 (треть экрана) — это
# слишком мелко для ленты; 4:3 срезает по четверти ширины с краёв, где у
# этого канала почти всегда фон, и даёт кадр в 42% высоты.
CLEAN_CROP_W = 1440

# Когда чистого clean.mp4 нет (ролик собран до его появления) и резать
# приходится из final.mp4 с вжжёнными субтитрами — берём только ВЕРХ
# кадра, выше их полосы. Кадр получается ниже, зато честный: без чужой
# строки и без её замыливания.
FALLBACK_CROP_W = 1200

# Доля свободного места, уходящая ВВЕРХ. Снизу оставляем больше: там
# субтитры в две строки крупным кеглем, сверху — вопрос.
FRAME_TOP_SHARE = 0.42

# Ниже этой границы текст не ставим: низ Shorts закрывает интерфейс
# YouTube — название канала, описание, кнопки и прогресс-бар.
SAFE_BOTTOM = 1660

# ── КАРТОЧКА ПОД ТЕКСТОМ ──────────────────────────────────────────────
# Полупрозрачная подложка под вопросом и под финальным призывом. Именно
# карточка, а не свечение по буквам: стекло `type.glass_events` вокруг
# текста делало вопрос размазанным, потому что `\blur18` размывает сами
# буквы. Здесь заливка — отдельный слой, текст поверх неё резкий.
PANEL_FILL = "&H0A121A&"    # тёмный тёплый, в порядке ASS это &HBBGGRR&
PANEL_EDGE = "&HE8F4FF&"    # холодный светлый кант, как у стекла в длинном
PANEL_ALPHA = "&H4A&"       # ~71% непрозрачности: фон под ней виден
PANEL_PAD_X = 38
PANEL_PAD_Y = 26
PANEL_RADIUS = 26

# Финальная карточка: призыв досмотреть длинный ролик. Висит последние
# CTA_TAIL секунд куска, поверх картинки, звук не трогает.
CTA_TAIL = 3.0
CTA_FS = 74

FONT = type_mod.font_name()

# Вступление вопроса: punch 118%→100%, затем уезжает наверх. Не путать
# с прежним «крупный вопрос на полкадра, потом ужимается в шапку» —
# масштаб ровно 118→100, как в ТЗ, не 190%.
INTRO_HOLD = 0.85
INTRO_MOVE = 0.55
PUNCH_MS = 380


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


# ЧТО ДЕЛАЕТ КУСОК ИНТЕРЕСНЫМ. Отбор по одной доле оказался слишком
# грубым: «развязка с числами» — это про то, где beats.py увидел цифры, а
# не про то, где история цепляет. На ff-ep09 так и вышло: оба шортса
# уехали на проходных абзацах, где просто чаще попадались числа.
#
# Здесь текст оценивается по признакам, которые в этом жанре и держат
# внимание: сумма денег, единственность находки, запрет и уничтожение,
# неожиданный поворот. Ни одного запроса к модели — пересборок у ролика
# пять-десять, и платный отбор означал бы плату за каждую.
#
# СУММЫ В СЦЕНАРИИ НАПИСАНЫ СЛОВАМИ, А НЕ ЦИФРАМИ, и это не мелочь, а
# причина, по которой первая версия отбора была слепой: текст пишется под
# начитку («one million American dollars», «eleven thousand euros»), цифр
# в нём почти нет, и regex по \d не находил НИ ОДНОЙ суммы во всём
# ролике — ни в одном из 246 окон ff-ep09. Денежный признак молча весил
# ноль везде, и куски ранжировались по чему угодно, кроме главного.
NUMWORD = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
           r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
           r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
           r"couple|several|dozens?|tens")
MONEY = re.compile(
    r"[$£€]\s?[\d,.]+"
    r"|\b[\d,.]+\s?(?:dollars?|euros?|pounds?|thousand|million|billion)\b"
    rf"|\b(?:{NUMWORD})[\s-]+(?:hundred|thousand|million|billion)\b"
    r"|\b(?:hundreds?|thousands?|millions?|billions?)\s+(?:of\s+)?"
    r"(?:dollars?|euros?|pounds?)\b", re.I)

INTEREST = (
    (2.6, MONEY),
    (1.5, re.compile(r"\b(?:sold for|paid|worth|fortune|price|auction|listed for)\b", re.I)),
    (1.8, re.compile(r"\b(?:only|rarest|rare|single|last|first|never|nobody|no one|entire)\b", re.I)),
    (1.8, re.compile(r"\b(?:destroy(?:ed|ing)?|recall(?:ed)?|banned|illegal|sued|lawsuit|court|ordered|secret|hidden|wiped)\b", re.I)),
    (1.4, re.compile(r"\b(?:turns out|actually|it turned out|realized|discovered|accident|mistake|forgot|somehow)\b", re.I)),
    (0.9, re.compile(r"\b(?:but|except|instead|until)\b", re.I)),
)

# Начало фразы, которое само по себе работает крючком: вопрос, обещание
# подробности, конкретный год. Шортс начинается с ПЕРВОГО предложения
# куска, и если оно звучит как середина абзаца, зритель уходит на нём же.
HOOK_START = re.compile(
    r"^(?:here'?s|here is|what|why|how|imagine|picture|nobody|no one|someone|"
    r"in \d{4}|by \d{4}|in (?:january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\b|"
    r"the (?:asking price|number|answer|catch|problem|item|seller|listing)|"
    r"a single|one of|that'?s (?:when|where|how|the))", re.I)


def interest(text: str) -> float:
    """Насколько кусок текста цепляет. Бесплатно, по лексике жанра."""
    if not text:
        return 0.0
    score = 0.0
    for w, rx in INTEREST:
        hits = len(rx.findall(text))
        if hits:
            # Второе и третье совпадение весят меньше первого: абзац с
            # перечислением сумм не в три раза интереснее абзаца с одной.
            score += w * (1.0 + 0.35 * (min(hits, 4) - 1))
    return score / max(1.0, len(text.split()) / 28.0)


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


# Доля, в которой кусок начинается. Крючок ролика (первый абзац) написан
# как крючок и в шортсе работает так же — занижать его незачем.
BEAT_W = {"revelation": 1.0, "escalation": 0.75, "hook": 0.9, "setup": 0.35,
          "reflection": 0.4, "cta": 0.2}

# Фраза, которая ОБЕЩАЕТ продолжение, а не закрывает мысль. В середине
# ролика это нормальная связка, но последним предложением шортса она
# оставляет зрителя ни с чем: «дальше самое интересное» — и конец.
DANGLING = re.compile(
    r"\b(?:worth sitting|here'?s (?:the|what)|i want to be upfront|"
    r"what happened next|the part that|comes next|turns out next|"
    r"is the part|first,|to understand)\b", re.I)


def window_score(part, kind: str) -> float:
    """
    Насколько кусок годится в самостоятельный шортс.

    Считается по трём местам, каждое решает свою задачу: НАЧАЛО (досмотрят
    ли первую секунду), КОНЕЦ (есть ли выплата) и СЕРЕДИНА (не провисает
    ли). Сумма денег весит отдельно и ОСОБЕННО в конце: правило канала —
    выплата ближе к концу куска, а не в его середине.
    """
    if not part:
        return 0.0
    first = (part[0].get("text") or "").strip()
    last = (part[-1].get("text") or "").strip()
    body = " ".join((m.get("text") or "") for m in part)
    hook = 1.6 * interest(first) + (2.2 if HOOK_START.search(first) else 0.0)
    if is_fragment_start(first):
        hook -= 4.0          # начало с обрывка убивает кусок целиком
    tail = part[-2:]
    payoff = 1.4 * interest(last) + 0.6 * max(interest(m.get("text") or "")
                                              for m in tail)
    if MONEY.search(" ".join((m.get("text") or "") for m in tail)):
        payoff += 1.8        # сумма прозвучала под конец — это и есть выплата
    if DANGLING.search(last):
        payoff -= 4.0        # кусок обрывается на обещании, а не на ответе
    core = 2.0 * hook + 1.9 * payoff + 0.9 * interest(body)
    # КУСОК БЕЗ СУММЫ — НЕ ШОРТС ЭТОГО КАНАЛА. Слова про суд, запрет и
    # уничтожение набирают вес и сами по себе: на ff-ep09 судебная справка
    # без единой цифры («ordered recalled and destroyed», «the ruling made
    # it unmistakably clear») обошла куски с настоящей выплатой. Жанр
    # держится на сумме — без неё кусок пересказывает предысторию.
    core *= 1.0 if MONEY.search(body) else 0.72
    return core * (0.65 + 0.5 * BEAT_W.get(kind, 0.3))


def scan_windows(marks, beats, total):
    """
    Все куски по границам предложений, отсортированные по window_score.

    Возвращает список (score, t0, t1, lo, hi, beat) — beat берётся по
    началу куска и нужен дальше только ради номера блока (свой вопрос в
    шапке) и названия доли в логе.
    """
    def beat_at(t):
        for b in beats:
            if b.start - 0.01 <= t <= b.end + 0.01:
                return b
        return beats[0] if beats else None

    out = []
    for i in range(len(marks)):
        for j in range(i, len(marks)):
            dur = marks[j]["end"] - marks[i]["start"]
            if dur > SHORT_MAX:
                break
            if dur < SHORT_MIN:
                continue
            b = beat_at(marks[i]["start"])
            if b is None:
                continue
            part = marks[i:j + 1]
            out.append((window_score(part, b.kind),
                        max(0.0, marks[i]["start"] - 0.15),
                        min(total, marks[j]["end"] + 0.35), i, j, b))
    out.sort(key=lambda x: -x[0])
    return out


def pick_windows(beats, marks, total, want=SHORT_COUNT):
    """
    want кусков, которые не пересекаются и не стоят вплотную.

    Два шортса из соседних абзацев — это один и тот же шортс дважды: то же
    место ролика, тот же материал в кадре, тот же смысл. Поэтому каждый
    следующий берётся только если он отстоит от уже взятых.

    Первый проход берёт кандидатов ТОЛЬКО из ещё не занятых script_blocks:
    лучшая развязка по числам не обязана распределяться по одной на блок
    (число может оказаться там, где чисел просто больше), и без этого
    условия оба куска на ff-ep06 достались одному и тому же блоку — с
    одним и тем же вопросом в шапке у обоих, что и обесценивает вопрос.
    Второй проход снимает ограничение по блоку и просто добирает
    недостающее — на коротком ролике с одной сильной развязкой второго
    блока может не быть вовсе, и это не повод остаться без второго шортса.
    """
    # ОКНА ПЕРЕБИРАЮТСЯ ПО ПРЕДЛОЖЕНИЯМ, А НЕ СТРОЯТСЯ ВОКРУГ ДОЛИ.
    # Раньше кандидат был один на долю: rank_beats выбирала долю, а
    # window_for наращивал окно назад от её конца — и чем оно начиналось,
    # никто не смотрел. На ff-ep09 так и вышло: «By every account, it
    # plays roughly like Tetris always plays» в начале шортса — это
    # антикрючок, зритель уходит на первой же секунде.
    #
    # Теперь перебираются ВСЕ окна по границам предложений (98 фраз —
    # это тысячи вариантов, доли секунды счёта), и каждое оценивается
    # window_score: чем начинается, чем заканчивается, что внутри. Доля
    # из beats осталась множителем, а не единственным критерием.
    cand = scan_windows(marks, beats, total)

    # «Другой блок» — ПРЕДПОЧТЕНИЕ, А НЕ ЦЕНА ЛЮБОЙ ЦЕНОЙ. Требование
    # завелось, чтобы у двух шортсов были разные вопросы в шапке, и это
    # верно — пока в другом блоке есть что показать. На ff-ep09 оно
    # притащило вторым куском судебную справку без единой суммы просто
    # потому, что она из другой главы. Слабого кандидата (меньше доли
    # FLOOR от лучшего) первый проход больше не берёт: второй проход
    # возьмёт сильный кусок, пусть и из той же главы.
    floor = cand[0][0] * 0.62 if cand else 0.0

    def take(out, require_new_block):
        for score, t0, t1, lo, hi, b in cand:
            if len(out) >= want:
                break
            if require_new_block and (any(b.block == o[4].block for o in out)
                                      or score < floor):
                continue
            if any(not (t1 <= o0 or t0 >= o1) for o0, o1, *_ in out):
                continue        # пересекается с уже взятым
            if any(min(abs(t0 - o1), abs(o0 - t1)) < 12.0 for o0, o1, *_ in out):
                continue        # стоит вплотную
            out.append((t0, t1, lo, hi, b))
        return out

    return take(take([], require_new_block=True), require_new_block=False)


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


def frame_layout(clean: bool) -> dict:
    """
    Геометрия видеокадра внутри вертикального экрана 1080x1920.

    clean=True — источник без вжжённых надписей (out/clean.mp4): берём
    кадр целиком по высоте, центральный кроп 4:3, масштаб 0.75 (вниз).

    clean=False — источник final.mp4, в нём уже вжжены субтитры длинного
    ролика. Их полосу считаем той же формулой, что type.sub_long_y (центр
    блока на 1080-BOTTOM-half, высота блока 2*half, худший случай — две
    строки), и берём только то, что ВЫШЕ неё. Смазывать её, как делала
    первая версия этой правки, нельзя: размытая полоса под кадром видна и
    читается как грязь.
    """
    if clean:
        cw, ch = CLEAN_CROP_W, 1080
    else:
        fs, mgn = type_mod.SUB_LONG_FS, type_mod.SUB_LONG_BOTTOM
        ch = 1080 - mgn - int(fs * 1.2 * 2) - 18
        cw = FALLBACK_CROP_W
    ch -= ch % 2
    cx = (1920 - cw) // 2
    fh = int(round(ch * FRAME_W / cw))
    fh -= fh % 2
    top = int((H - fh) * FRAME_TOP_SHARE)
    top -= top % 2
    return dict(cw=cw, ch=ch, cx=cx, cy=0, fh=fh, top=top, bottom=top + fh)


def header_layout(question: str, style: str = "") -> dict:
    """
    Геометрия шапки: вопрос сверху, стекло (см. build_ass), без плоской
    заливки-плашки — сам блик blur18 читается как размытая табличка под
    текстом, без отдельного drawbox.

    Кегль поднят вдвое (64/54/44 -> 96/80/66) той же логикой, что и у
    субтитров длинного ролика: маленький текст на телефоне в ленте
    проигрывает всему остальному в кадре. wrap ужат с 24 до 17 символов
    на строку — иначе больший кегль тут же упирался бы в fit_size и
    возвращался к прежнему размеру, съедая весь выигрыш.

    Пустой вопрос — пустой макет: без него десять секунд провисела бы
    пустая анимация без единой буквы.
    style оставлен в сигнатуре, чтобы старые вызовы не падали, и игнорируется.
    """
    frame = style if isinstance(style, dict) else frame_layout(True)
    lines = wrap(question.strip(), 20) if question else []
    if not lines:
        return dict(lines=[], size=0, cx=W // 2, intro_cy=0, block_cy=0)
    size = 100 if len(lines) <= 2 else (88 if len(lines) == 3 else 78)
    size = fit_size(lines, W - 2 * BOX_MARGIN, size, floor=44)
    # Зона над кадром. Вопрос в неё вписывается и по высоте тоже: при
    # четырёх строках крупного кегля он иначе заезжал бы на картинку.
    zone = frame["top"] - BOX_MARGIN
    if size * 1.22 * len(lines) > zone:
        size = max(40, int(zone / (1.22 * len(lines))))
    ph = size * 1.22 * len(lines)
    block_cy = BOX_MARGIN + (zone - ph) / 2 + ph / 2
    return dict(lines=lines, size=size, cx=W // 2,
                intro_cy=int(frame["top"] + frame["fh"] / 2),
                block_cy=block_cy)


def sub_center(frame: dict) -> int:
    """Центр блока субтитров — под кадром, выше интерфейса YouTube."""
    return int((frame["bottom"] + SAFE_BOTTOM) / 2)


def rounded_box(w: int, h: int, r: int) -> str:
    """
    Прямоугольник со скруглёнными углами командами рисования libass (\\p1).

    Углы — квадратичные безье через саму вершину: ASS рисует ими дугу,
    визуально неотличимую от радиуса. Координаты отсчитываются от точки
    \\pos при \\an7, то есть от левого верхнего угла плашки.
    """
    r = max(0, min(r, w // 2, h // 2))
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} "
            f"l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} "
            f"l 0 {r} b 0 0 0 0 {r} 0")


def panel_events(lines, size: int, cy: int, start: float, end: float,
                 layer: int = 0, extra: str = "") -> list[str]:
    """
    Полупрозрачная карточка под текстом: заливка + тонкий светлый кант.

    ЭТО НЕ СВЕЧЕНИЕ ТЕКСТА. Первая попытка сделала «табличку» стеклом
    `type.glass_events` — свечением по самим буквам (`\\blur18`), и вопрос
    из-за него читался размазанным. Карточка должна быть подложкой, а
    текст на ней — резким: заливка рисуется отдельным слоем (\\p1), текст
    идёт поверх без единого блюра.

    Фон над кадром и под ним — уже размытая копия кадра (см. render_short),
    поэтому полупрозрачная заливка поверх него читается как матовое
    стекло, и настоящий blur под плашкой рисовать незачем.
    """
    if not lines:
        return []
    text_w = int(type_mod.measure_width(lines, size))
    bw = min(W - 2 * BOX_MARGIN, text_w + 2 * PANEL_PAD_X)
    bh = int(size * 1.22 * len(lines)) + 2 * PANEL_PAD_Y
    x0, y0 = (W - bw) // 2, int(cy - bh / 2)
    box = rounded_box(bw, bh, PANEL_RADIUS)
    tag = f"\\an7\\pos({x0},{y0})\\bord0\\shad0{extra}\\p1"
    t0, t1 = ass_time(start), ass_time(end)
    return [
        f"Dialogue: {layer},{t0},{t1},PANEL,,0,0,0,,"
        f"{{{tag}\\1c{PANEL_FILL}\\1a{PANEL_ALPHA}}}{box}{{\\p0}}",
        f"Dialogue: {layer},{t0},{t1},PANEL,,0,0,0,,"
        f"{{{tag}\\1a&HFF&\\bord2\\3c{PANEL_EDGE}\\3a&H90&}}{box}{{\\p0}}",
    ]


def karaoke_ready(marks, lo, hi) -> bool:
    """Есть ли ИЗМЕРЕННЫЕ тайм-коды слов у всех предложений куска."""
    part = marks[lo:hi + 1]
    return bool(part) and all(m.get("words") for m in part)


def build_ass(subs, layout: dict, out: Path, word_marks=None,
              t0: float = 0.0, t1: float = 0.0, frame: dict = None,
              cta: str = ""):
    """
    Шапка — РЕЗКИЙ текст на полупрозрачной карточке (`panel_events`).

    До этого шапка была стеклом `type.glass_events`: свечение шло по самим
    буквам (`\\blur18`), и вопрос читался размазанным — на готовом шортсе
    это сразу видно. Подложка и свечение это разные вещи: карточка должна
    быть ПОД текстом отдельным слоем, а буквы на ней — без единого блюра.
    Фон над кадром уже размыт (render_short), поэтому полупрозрачная
    заливка поверх него и даёт матовое стекло.

    Вступление вопроса в libass: масштаб 118%→100% и \\move наверх, тот же
    приём, что был. Не Remotion, не deshake, не второй Ken Burns.

    ПРИЗЫВ В КОНЦЕ (`cta`) — карточка на последние CTA_TAIL секунд: шортс
    обязан звать на длинный ролик, иначе он просто отдаёт развязку и
    отпускает зрителя.

    ПОДСВЕТКА ЗАЛИВКОЙ. word_marks — предложения с измеренными тайм-кодами
    слов (assets.words_between по посимвольному выравниванию ElevenLabs).
    Строка показывается целиком приглушённой, и каждое слово к своему
    тайм-коду выходит на полную яркость И ТАК И ОСТАЁТСЯ (type.karaoke_line)
    — та же механика, что в длинном ролике, иначе шортс перестаёт читаться
    как его кусок. Нет измеренных слов — строка выводится без заливки:
    подпись есть всегда, выдуманных тайм-кодов слов нет.
    """
    q_size = layout["size"] or 1
    all_lines = [l for _, _, c in subs for l in wrap(c, SUB_MAX_CHARS)]
    frame = frame or frame_layout(True)
    sub_cy = sub_center(frame)
    sub_size = fit_size(all_lines, W - 2 * SUB_MARGIN, SUB_SIZE, floor=34)
    # Подпись живёт ПОД кадром, в полосе до интерфейса YouTube. Если две
    # строки крупного кегля в неё не помещаются, кегль ужимается — иначе
    # верхняя строка заезжает на картинку, а нижняя уходит под кнопки.
    room = (SAFE_BOTTOM - frame["bottom"])
    sub_size = min(sub_size, max(34, int(room / (1.2 * SUB_MAX_LINES))))
    # Заливка субтитра — тот же #f5f5f7, что в длинном ролике (type.SUB_FULL);
    # приглушённую ступень задаёт \\1a внутри события. Шапка с вопросом
    # остаётся чисто белой: она не заливается и должна быть ярче подписи.
    white, black = "&H00FFFFFF", "&H00000000"
    sub_fill = "&H00" + type_mod.SUB_FULL.strip("&H&")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: PANEL,{FONT},20,{white},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Q,{FONT},{q_size},{white},{black},&H00000000,-1,0,0,0,100,100,0,0,1,5,0,5,40,40,40,1
Style: CTA,{FONT},{CTA_FS},{white},{black},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,5,40,40,40,1
Style: SUB,{FONT},{sub_size},{sub_fill},{black},&H00000000,-1,0,0,0,100,100,0,0,1,7,0,5,{SUB_MARGIN},{SUB_MARGIN},60,1

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
        move = (f"\\move({cx},{intro_cy},{cx},{top_cy},{hold_ms},{move_end})"
                f"\\fscx118\\fscy118\\t(0,{PUNCH_MS},\\fscx100\\fscy100)")
        # Карточка едет вместе с текстом: те же \move и punch, только
        # точка отсчёта у неё левый верхний угол (\an7), а у текста центр.
        pw = min(W - 2 * BOX_MARGIN,
                 int(type_mod.measure_width(layout["lines"], q_size))
                 + 2 * PANEL_PAD_X)
        ph = int(q_size * 1.22 * len(layout["lines"])) + 2 * PANEL_PAD_Y
        p_move = (f"\\move({cx - pw // 2},{intro_cy - ph // 2},"
                  f"{cx - pw // 2},{top_cy - ph // 2},{hold_ms},{move_end})"
                  f"\\fscx118\\fscy118\\t(0,{PUNCH_MS},\\fscx100\\fscy100)")
        box = rounded_box(pw, ph, PANEL_RADIUS)
        for tag in (f"\\1c{PANEL_FILL}\\1a{PANEL_ALPHA}",
                    f"\\1a&HFF&\\bord2\\3c{PANEL_EDGE}\\3a&H90&"):
            rows.append(
                f"Dialogue: 0,{ass_time(0)},{ass_time(99999)},PANEL,,0,0,0,,"
                f"{{\\an7{p_move}\\bord0\\shad0{tag}\\fad(90,0)\\p1}}"
                f"{box}{{\\p0}}")
        rows.append(
            f"Dialogue: 2,{ass_time(0)},{ass_time(99999)},Q,,0,0,0,,"
            f"{{\\an5{move}\\fad(90,0)}}" + text)
    if cta and t1 > t0:
        dur = t1 - t0
        c_start = max(0.0, dur - CTA_TAIL)
        c_lines = type_mod.wrap_to_width(cta, CTA_FS, W - 2 * BOX_MARGIN - 2 * PANEL_PAD_X,
                                         max_lines=2) or [cta]
        c_cy = int(frame["bottom"] - CTA_FS * 1.22 * len(c_lines) / 2 - PANEL_PAD_Y - 40)
        rows += panel_events(c_lines, CTA_FS, c_cy, c_start, dur,
                             layer=5, extra="\\fad(220,120)")
        rows.append(
            f"Dialogue: 6,{ass_time(c_start)},{ass_time(dur)},CTA,,0,0,0,,"
            f"{{\\an5\\pos({W // 2},{c_cy})\\fad(220,120)}}"
            + "\\N".join(ass_escape(l) for l in c_lines))
    kara = []
    if word_marks:
        kara = type_mod.sub_events(word_marks, t0, t1 or 10 ** 9, sub_size,
                                   W - 2 * SUB_MARGIN, style="SUB",
                                   max_lines=SUB_MAX_LINES)
    if kara:
        # Шортс центрируется, а не выключается влево, как длинный: кадр
        # 1080 в ширину, и колонка с ровным левым краем в нём читается как
        # съехавшая вбок подпись, а не как выключка.
        for s, e, _style, body, enter in kara:
            rows.append(
                f"Dialogue: 1,{ass_time(s)},{ass_time(e)},SUB,,0,0,0,,"
                + type_mod.sub_tag(W // 2, sub_cy, an=5, enter=enter)
                + body)
    else:
        for s, e, c in subs:
            rows.append(
                f"Dialogue: 1,{ass_time(s)},{ass_time(e)},SUB,,0,0,0,,"
                f"{{\\pos({W//2},{sub_cy})}}"
                + "\\N".join(ass_escape(l) for l in wrap(c, SUB_MAX_CHARS)))
    out.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return out


# ─────────────────────── РЕНДЕР ───────────────────────

def render_short(src: Path, t0: float, t1: float, ass: Path, dst: Path,
                 frame: dict, crf: int = 20):
    """
    Вырезает кусок, вписывает кадр в 9:16 и накладывает шапку и субтитры.

    КАДР ВПИСЫВАЕТСЯ, А НЕ РАСТЯГИВАЕТСЯ. Прежняя версия брала из
    1920x1080 центральную колонку 608 px и разгоняла её до 1080 — апскейл
    в 1.78 раза по узкой полоске: мыло на любой фактуре и случайный
    вертикальный срез вместо снятого плана. Теперь берётся широкий кроп
    (frame_layout) и масштабируется ВНИЗ до ширины экрана, а свободное
    место сверху и снизу занимает сильно размытая и притемнённая копия
    того же кадра. Это стандартная раскладка вертикальной нарезки: видео
    остаётся собой, фон не спорит с ним, текст ложится на фон, а не на
    картинку.

    Фон делается из ТОГО ЖЕ кропа (split), а не из исходного кадра: так у
    фона и кадра совпадают цвет и свет, и стык не читается. Размытому фону
    апскейл безразличен — его качество никто не видит.

    hqdn3d убран вместе с апскейлом: он глушил зерно, разогнанное прежним
    увеличением. При масштабе вниз зерно усредняется само, а лишний
    фильтр только мылит.

    -ss ДО -i, а не после: так ffmpeg перематывает по ключевым кадрам и не
    декодирует всё от начала файла. На получасовом ролике разница — секунды
    против минут.
    """
    ass_f = f"ass={ass.as_posix()}:fontsdir={type_mod.fontsdir()}"
    fc = (
        f"[0:v]setpts=PTS-STARTPTS,"
        f"crop={frame['cw']}:{frame['ch']}:{frame['cx']}:{frame['cy']},"
        f"split=2[fg][bgsrc];"
        f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},boxblur=34:2,eq=brightness=-0.04:saturation=0.9[bg];"
        f"[fg]scale={FRAME_W}:{frame['fh']}:flags=lanczos[fgs];"
        f"[bg][fgs]overlay=0:{frame['top']}:shortest=1,setsar=1[v];"
        f"[v]{ass_f}[vout];"
        # ЗВУК ЧЕРЕЗ ФИЛЬТР, А НЕ НАПРЯМУЮ. С -map 0:a дорожка уезжала в
        # шортс с ИСХОДНЫМИ тайм-кодами длинного ролика: у куска с 918-й
        # секунды первый аудиопакет получал pts 42.7 при видео от нуля, и
        # первые сорок секунд шортса шли в тишине — ровно то, что видно
        # как «озвучка пропадает». asetpts сбрасывает начало в ноль,
        # aresample добивает дырки и держит дорожку непрерывной.
        f"[0:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[aout]"
    )
    run(["ffmpeg", "-v", "error", "-y",
         "-ss", f"{t0:.3f}", "-t", f"{t1-t0:.3f}", "-i", str(src),
         "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
         "-r", str(FPS),
         "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         str(dst)])
    # ЗВУК ПРОВЕРЯЕТСЯ ЗАМЕРОМ, А НЕ НА ВЕРУ. У роликов, собранных до
    # фикса build.voice_with_pauses, разметка аудиодорожки врёт: после
    # паузы главы быстрый переход попадает в пустоту, и кусок выходит
    # немым или с обрезанным звуком. Молчать об этом нельзя — именно так
    # два шортса уехали к человеку без озвучки.
    v, a = duration_of(dst), duration_of(dst, "a")
    if a < v * 0.9:
        log(f"  ! звука в куске {a:.1f} с при видео {v:.1f} с. Дорожка "
            f"исходного ролика собрана до фикса разрывов — пересобери его "
            f"(stage: render), одним shorts.py это не лечится")
    return dst


def duration_of(p: Path, stream: str = "") -> float:
    """Длина файла, а с stream='a' или 'v' — длина этой дорожки."""
    sel = ["-select_streams", stream] if stream else []
    what = "stream=duration" if stream else "format=duration"
    r = subprocess.run(["ffprobe", "-v", "error", *sel, "-show_entries",
                        what, "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip().splitlines()[0].rstrip(","))
    except (ValueError, IndexError):
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

def main(job_path, want=SHORT_COUNT):
    job = load_job(job_path)
    work = ROOT / "work" / job["id"]
    assets, out = work / "assets", work / "out"
    final = out / "final.mp4"
    if not final.exists():
        raise SystemExit(f"нет {final} — сначала собери ролик (build.py)")

    # ИСТОЧНИК — РОЛИК БЕЗ ВЖЖЁННЫХ НАДПИСЕЙ, если он есть. В final.mp4
    # уже стоят субтитры длинного ролика, и в вертикальном кадре они
    # читаются чужой строкой поверх своей. clean.mp4 — тот же монтаж до
    # прохода ASS (build.py кладёт его рядом и в релиз), шкала времени та
    # же. Его нет только у роликов, собранных до этой правки: тогда режем
    # из final.mp4 и берём верх кадра, выше полосы его субтитров.
    clean = out / "clean.mp4"
    src, is_clean = (clean, True) if clean.exists() else (final, False)
    if not is_clean:
        log("  ! clean.mp4 (монтаж без вжжённых надписей) не найден — режу "
            "из final.mp4 и беру только ВЕРХ кадра, выше его субтитров. "
            "Кадр из-за этого ниже; полный вернётся после любого прогона "
            "stage: render или auto — они кладут clean.mp4 в релиз")

    frame = frame_layout(is_clean)
    log(f"── кадр: {frame['cw']}x{frame['ch']} из исходника -> "
        f"{FRAME_W}x{frame['fh']} на {frame['top']} px сверху, "
        f"масштаб {FRAME_W / frame['cw']:.2f}, фон размытый")

    marks = load_final_marks(job, assets, out)
    total = duration_of(src)
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

    # Призыв досмотреть длинный ролик — тот же, что стоит в концовке
    # самого ролика (channel/defaults.json → outro_cta), чтобы шортс и
    # ролик звали одними словами. Пустое значение = карточки нет.
    cta = (job.get("shorts_cta") or job.get("outro_cta") or "").strip()
    log("── оформление: Oswald, карточка под вопросом, субтитры белым"
        + (f", призыв в конце «{cta}»" if cta else ", без призыва в конце"))

    made, questions_used = [], []
    for n, (t0, t1, lo, hi, beat) in enumerate(windows, 1):
        # Свой вопрос под свой блок сценария, а не один на оба шортса: два
        # куска почти всегда режутся из РАЗНЫХ историй ролика (разные
        # script_blocks), и общий вопрос либо не относится ко второму
        # шортсу, либо выдаёт его развязку раньше, чем видео до неё дошло.
        # per_block ключуется строкой номера блока (JSON не умеет int-ключи).
        question = per_block.get(str(beat.block), default_question)
        layout = header_layout(question, frame)
        subs = lines_with_times(marks, lo, hi, t0)
        kara_ok = karaoke_ready(marks, lo, hi)
        ass = build_ass(subs, layout, out / f"short_{n}.ass",
                        word_marks=marks[lo:hi + 1] if kara_ok else None,
                        t0=t0, t1=t1, frame=frame, cta=cta)
        dst = out / f"short_{n}.mp4"
        log(f"── шортс {n}: {t0:.1f}–{t1:.1f} с ({t1-t0:.1f} с), "
            f"доля «{beat.kind}», блок {beat.block}, субтитров {len(subs)}, "
            f"{'подсветка по замеру' if kara_ok else 'БЕЗ подсветки (нет слов в marks)'}, "
            f"вопрос: {question or '(нет)'}")
        render_short(src, t0, t1, ass, dst, frame,
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
