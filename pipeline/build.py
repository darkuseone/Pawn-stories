"""
build.py — собирает ролик из готовых материалов.

Порядок работы:
  1. План кадров. Границы берутся из тайм-кодов ElevenLabs, поэтому
     кадр меняется на конце предложения, а не по секундомеру.
  2. Рендер каждого кадра отдельно, параллельно по числу ядер.
  3. Склейка группами по 12 с переходами, цветокором и оверлеями.
  4. Финальная сшивка без перекодирования, звук, субтитры.

Два цветокора работают одновременно и осознанно:
  тёплая семья → сгенерированные кадры и стоковое видео
  архивный     → подлинные фото и хроника из архивов
Так зритель на уровне ощущения отличает документ от всего остального.
Стоковое видео идёт по семейному, а не по архивному: современный сток в
сепии читается как подделка под старину, а не как хроника.

Материал смешивается в заданной пропорции: по умолчанию 30% экранного
ВРЕМЕНИ отдаётся генерации, остальные 70% — стоковому видео и подлинным
фото. Держит пропорцию MaterialMix, проверяется замером в конце сборки.
"""

import json
import math
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job

import channel
import render
import vet
import style as style_mod
from render import W, H, FPS
from editorial import beats as beats_mod
from editorial import pacing as pacing_mod
from editorial import memory as memory_mod
from editorial import rails, textcard, edl

ROOT = Path(__file__).parent.parent
LUTS = ROOT / "assets" / "luts"
OVERLAYS = ROOT / "assets" / "overlays"
# Папка подложек. Конкретный файл выбирает движок стиля (style.music_pool)
# и разводит с последними роликами — как цветокор и переход. Поле music в
# спецификации остаётся жёстким переопределением на случай, когда ролику
# нужна именно эта дорожка.
MUSIC_DIR = ROOT / "assets" / "music"

SEG_SIZE = 12          # кадров в одной группе склейки

# Движения для фотографий во вступлении: только скольжение и наезд.
# Наклоны там читаются вяло, а статики быть не должно вовсе.
INTRO_MOVES = ["push_in", "pan_right", "pan_left",
               "sweep_in", "push_right", "push_left"]

# Сток длиннее этого в кадр не ставим: клип приходит на 10-20 секунд, и на
# кадре в 20+ он уходит на второй круг петли — видно как рывок назад.
# Долгий кадр всегда достаётся фотографии.
CLIP_MAX_SECONDS = 15.0

# Потолок повторов ОДНОГО стокового клипа. На ff-ep03 отбраковка (vet.py)
# оставила от 35 скачанных клипов только 3 годных, а ритм «через каждые три
# кадра — сток» на теле в 210+ кадров требовал под сотню клиповых слотов.
# Без потолка это значит один и тот же клип на второй, сорок первой и
# семьдесят четвёртой минуте — зритель это увидел и справедливо назвал
# конвейером. Как только КАЖДЫЙ клип в пуле показан столько раз, слот
# уходит фотографии или генерации: обеих в разы больше, повтор там
# растворяется в разнообразии.
MAX_CLIP_REPEATS = 3

# Потолок повторов ОДНОЙ фотографии или картинки — архивной или
# сгенерированной. Раньше был только у видео (MAX_CLIP_REPEATS): у ShotPicker
# для фото и генерации совпадение слов гасло от показов (см. score()), но
# ничего не запрещало показать удачно совпавший файл четвёртый и пятый раз
# подряд по ролику, если конкуренции по смыслу не было. На ff-ep06 (26 мин,
# небогатый архив по теме) один и тот же снимок вышел больше трёх раз —
# зритель увидел это как повтор, а не как эпизод, отличный от предыдущего.
#
# Значение то же, что у клипов, не отдельная догадка: то же соотношение
# «показ этого канала — это дыра в материале, а не решение монтажа».
# Пул, исчерпанный по потолку, не блокирует показ вовсе (кадр всё равно
# нужен), просто перестаёт быть предпочтением — см. over_cap в score().
MAX_IMAGE_REPEATS = 3

# Пауза перед каждой новой историей: чёрная карточка с названием главы,
# и голос по-настоящему замолкает на это время — не наплыв поверх звука,
# а тишина, иначе «пауза» это вопрос к монтажёру, а не к диктору. 2.6 —
# середина заказанных «2-3 секунды»: короче читается как техническая
# заминка, длиннее держит зрителя в неизвестности дольше, чем нужно для
# смены темы.
CHAPTER_PAUSE = 2.6

# Ходы камеры для ПОВТОРНЫХ показов клипа, см. render.FOOTAGE_MOVES. Первый
# показ идёт как снят, второй и третий — с медленным наездом или уводом.
# ClipCutter к этому моменту уже отдаёт другой КУСОК файла, так что вместе
# это разные план и движение: узнать в них один исходник трудно.
CLIP_REPEAT_MOVES = ["drift_in", "drift_left", "drift_out", "drift_right",
                     "drift_up"]


def log(*a):
    print(*a, flush=True)


def cores():
    return max(1, (os.cpu_count() or 2))


# ───────────────────────── НАРЕЗКА ФУТАЖА ─────────────────────────

class ClipCutter:
    """
    Раздаёт КУСКИ стоковых клипов, а не клипы целиком.

    Сток приходит по 14-20 секунд. Если ставить такой клип в кадр целиком,
    зритель двадцать секунд смотрит одну и ту же панораму — ровно то, из-за
    чего ролик и выключают. Поэтому из каждого файла режутся разные куски по
    несколько секунд, и один и тот же файл, попадаясь второй раз, показывает
    другое место.

    Курсор идёт по файлу вперёд и заворачивается в начало, когда упирается
    в конец. Между кусками пропуск в полсекунды: соседние отрезки одного
    файла не должны выглядеть как склейка внутри одного движения камеры.
    """

    GAP = 0.5

    def __init__(self):
        self.length = {}
        self.cursor = {}

    def duration(self, path: Path) -> float:
        key = str(path)
        if key not in self.length:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", key], capture_output=True, text=True)
            try:
                self.length[key] = float(r.stdout.strip())
            except ValueError:
                self.length[key] = 0.0       # не прочиталось — берём с начала
        return self.length[key]

    def take_start(self, path: Path, dur: float) -> float:
        """Отдаёт секунду начала следующего куска этого файла."""
        key = str(path)
        total = self.duration(path)
        start = self.cursor.get(key, 0.0)
        # кусок не помещается в остаток файла — заходим на второй круг
        if total and start + dur > total - 0.15:
            start = 0.0
        self.cursor[key] = start + dur + self.GAP
        return round(start, 3)


STOP_WORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
    "with", "from", "that", "this", "it", "is", "was", "were", "are", "be",
    "been", "you", "your", "they", "them", "their", "not", "but", "what",
    "when", "who", "how", "why", "all", "one", "two", "its", "has", "had",
    "have", "will", "would", "can", "could", "about", "into", "than", "then",
    "there", "here", "very", "just", "more", "most", "some", "any", "out",
    "closeup", "close", "up", "shot", "cinematic", "detail", "warm", "light",
}


def words_of(text: str):
    """Значимые слова строки. Общие и служебные выброшены."""
    import re
    return {w for w in re.findall(r"[a-zA-Z]+", (text or "").lower())
            if len(w) > 2 and w not in STOP_WORDS}


class ShotPicker:
    """
    Выдаёт материал, который СООТВЕТСТВУЕТ ТОМУ, ЧТО ЗВУЧИТ В ЭТУ СЕКУНДУ.

    Было два поколения этой логики, и оба недостаточны.

    Первое: выдача по кругу. Список кончался и начинался заново, поэтому на
    сороковой минуте зритель видел иллюстрацию к первому абзацу.

    Второе: привязка позиции в списке к позиции на таймлайне. Это чинило
    круг, но опиралось на предположение, что материал лежит в порядке
    сценария. Для сгенерированных картинок так и есть — промпты пишутся по
    порядку. Для стока и архива НЕТ: они приходят в порядке выдачи поисковика,
    и «сорок процентов списка» не значит ровно ничего.

    Здесь выбор идёт ПО СМЫСЛУ. У каждого файла есть слова: у генерации — из
    промпта, у стока и архива — из запроса, по которому он скачан. У кадра
    есть текст предложений, которые под ним звучат. Берётся файл с наибольшим
    пересечением слов.

    При равном пересечении (а оно часто нулевое — половина предложений не
    содержит предметных слов вовсе) выбор падает обратно на позицию в
    таймлайне: это по-прежнему лучше случайного. Плюс штраф за повторный
    показ и запрет на два одинаковых кадра подряд.

    Выбор детерминирован: тот же id и тот же материал дают тот же ролик.
    """

    WINDOW = 3

    def __bool__(self):
        return bool(self.pool)

    # Насколько тяжело весит показ файла В ПРОШЛЫХ РОЛИКАХ по сравнению с
    # показом в этом. Половина: файл, отработавший в прошлой загрузке, не
    # запрещён — он просто уступает свежему при прочих равных. Полный вес
    # означал бы, что удачный клип нельзя показать во второй раз никогда,
    # а материала на канале конечное количество.
    PRIOR_WEIGHT = 0.5

    def __init__(self, pool, total: float, prior=None, cap: int = None):
        # pool: [(path, tag, keywords), ...]
        self.pool = pool
        self.total = max(total, 0.001)
        self.used = {}
        self.last = None
        self.hits = 0          # сколько раз попали по смыслу
        self.calls = 0
        # сколько раз выданный файл показывали ДО этого раза: 0 — первый
        # показ. По нему монтаж решает, вешать ли на клип ход камеры.
        self.last_repeat = 0
        # Потолок повторов ОДНОГО файла В ЭТОМ РОЛИКЕ. None — без потолка,
        # для обратной совместимости с местами, где ShotPicker используется
        # не для показа зрителю (если такие появятся). cap проверяется по
        # СЫРОМУ счётчику показов в этом видео, не по used из score() —
        # тот блендит с памятью канала (prior), а потолок должен ловить
        # именно повтор внутри одного ролика, который и увидел зритель.
        self.cap = cap

        # ПАМЯТЬ МЕЖДУ РОЛИКАМИ. Износ внутри ролика (см. score) спасает от
        # шести показов одной вазы в одном ролике, но ничего не знает про
        # прошлые загрузки: пул стока и архива у канала общий, и клип,
        # годный по теме, выигрывает подбор в каждом ролике подряд. Зритель
        # канала видит одну и ту же врезку в трёх видео и справедливо
        # называет это конвейером.
        #
        # Здесь счётчик приезжает из журнала канала и садится в стартовые
        # показы. Ключ — имя файла: полные пути у каждого ролика свои.
        prior = prior or {}
        self.prior = {}
        for j, (path, _tag, _kw) in enumerate(pool):
            n = prior.get(Path(path).name, 0)
            if n:
                self.prior[j] = n * self.PRIOR_WEIGHT

    def take(self, t: float, text: str = ""):
        n = len(self.pool)
        if not n:
            raise SystemExit("пустой пул материала")
        want = words_of(text)
        k = min(n - 1, max(0, int(t / self.total * n)))
        self.calls += 1

        def score(j):
            path, _tag, kw = self.pool[j]
            # показы в этом ролике плюс половина показов в прошлых
            used = self.used.get(j, 0) + self.prior.get(j, 0.0)
            # СМЫСЛОВОЕ СОВПАДЕНИЕ ГАСНЕТ ОТ ПОКАЗОВ.
            #
            # Раньше overlap стоял в ключе выше счётчика показов, и файл,
            # чьи слова совпадали с частой темой ролика, выигрывал раз за
            # разом: на прогоне ff-ep03 одна белая ваза с птицами вышла
            # шесть раз — «porcelain vase» звучит в этом эпизоде постоянно,
            # и она обыгрывала всё остальное на каждом кадре.
            #
            # Теперь каждый показ съедает единицу совпадения. Трижды
            # показанный файл с тремя совпавшими словами равен свежему без
            # единого совпадения — смысл по-прежнему решает, но не даёт
            # права на бесконечный повтор.
            overlap = len(want & kw) - used
            same = 1 if path == self.last else 0
            # ПОТОЛОК ПОВТОРОВ — ПЕРВЫЙ КЛЮЧ, ВЫШЕ ВСЕГО ОСТАЛЬНОГО. Раньше
            # его не было вовсе для картинок (только у стокового видео, см.
            # MAX_CLIP_REPEATS), и файл с удачным семантическим совпадением
            # выигрывал подбор в каждом кадре подряд весь ролик — на
            # ff-ep06 архивное фото показалось больше трёх раз за 26 минут.
            # over_cap стоит ПЕРЕД same: пока в пуле есть хоть один файл
            # младше потолка, он побеждает даже тот же кадр, что был только
            # что — иначе на исчерпанном пуле (все выше потолка) кадр
            # застрял бы, гоняя один и тот же файл через один.
            raw = self.used.get(j, 0)
            over_cap = 1 if (self.cap is not None and raw >= self.cap) else 0
            # порядок важен: сначала потолок, потом не повторяться, потом
            # смысл (с учётом износа), потом реже показанное, потом ближе
            # по таймлайну
            return (over_cap, same, -overlap, used, abs(j - k), j)

        best = min(range(n), key=score)
        if len(want & self.pool[best][2]):
            self.hits += 1
        self.last_repeat = self.used.get(best, 0)
        self.used[best] = self.used.get(best, 0) + 1
        self.last = self.pool[best][0]
        return self.pool[best][0], self.pool[best][1]

    def report(self):
        if not self.calls:
            return "не использовался"
        carried = sum(1 for v in self.prior.values() if v)
        tail = f", {carried} файлов уже шли в эфир" if carried else ""
        return (f"{self.hits} из {self.calls} кадров подобраны по смыслу "
                f"({self.hits/self.calls*100:.0f}%){tail}")

    def used_names(self):
        """Имена показанных файлов и число показов — для журнала канала."""
        return {Path(self.pool[j][0]).name: n
                for j, n in self.used.items() if n}

    def exhausted(self, cap: int) -> bool:
        """Правда, когда КАЖДЫЙ файл в пуле уже показан cap раз и больше.

        Не «средний повтор», а именно минимум по пулу: пока есть хоть один
        файл младше потолка, score() в take() и так предпочтёт его — ждать
        нужно, пока честно закончатся вообще все варианты.
        """
        n = len(self.pool)
        if not n:
            return True
        return min(self.used.get(j, 0) for j in range(n)) >= cap


class MaterialMix:
    """
    Держит долю сгенерированного в ролике — по ВРЕМЕНИ, а не по числу кадров.

    Зачем вообще: сюжет про находку — это рассказ про КОНКРЕТНЫЙ предмет.
    Вот эта монета, вот этот сервиз. Генератор такую монету не найдёт, он её
    нарисует, и рисунок выдаёт себя за фотографию реального предмета. Под
    предмет идёт только настоящее — архивное фото или сток. Генерация
    закрывает общие планы, руки крупно, интерьер лавки, атмосферу.

    Почему по времени, а не по кадрам: кадры разной длины. Двадцать
    двухсекундных кусков во вступлении и двадцать десятисекундных в теле —
    это одинаковое число кадров и впятеро разное экранное время. Зритель
    считает время.

    Почему на ходу, а не заранее списком: длительность кадра известна только
    в момент раскладки, она зависит от границ предложений. Поэтому решение
    принимается по накопленному счёту — «генерации пока меньше заказанного,
    следующий кадр её». Так к концу ролика доля сходится к заданной, и она же
    выдержана НА ЛЮБОМ отрезке, а не только в среднем: зритель, включивший с
    двадцатой минуты, видит ту же пропорцию.

    Случайность здесь не используется намеренно: доля материала — это не
    стилевой жребий, а требование к ролику, и она не должна плавать от id.
    """

    def __init__(self, target: float, gen: bool, arch: bool, clips: bool):
        self.target = max(0.0, min(1.0, float(target)))
        self.have = {"gen": bool(gen), "arch": bool(arch), "clip": bool(clips)}
        self.sec = {"gen": 0.0, "arch": 0.0, "clip": 0.0}
        self.shots = {"gen": 0, "arch": 0, "clip": 0}

    def pick(self, allowed):
        """
        Чем закрыть очередной кадр. allowed — что тут вообще уместно:
        под якорный (длинный) кадр сток не годится, во вступлении есть свои
        слоты под видео и под фото.
        """
        can = [k for k in allowed if self.have[k]]
        if not can:
            # ничего из разрешённого нет — берём что есть вообще
            can = [k for k in ("gen", "arch", "clip") if self.have[k]]
        if not can:
            raise SystemExit("нет материала ни одного вида")
        if len(can) == 1:
            return can[0]

        total = sum(self.sec.values())
        behind = total <= 0 or (self.sec["gen"] / total) < self.target

        # Генерация — только когда она отстаёт от заказанной доли.
        # На пустом счёте (total == 0) behind истинно, но первым кадром
        # ролика генерацию ставить не хочется: открывать историю про
        # настоящую находку рисунком — ровно то, чего мы избегаем.
        if "gen" in can and behind and total > 0:
            return "gen"
        real = [k for k in ("clip", "arch") if k in can]
        return real[0] if real else can[0]

    def charge(self, kind: str, seconds: float):
        self.sec[kind] += max(seconds, 0.0)
        self.shots[kind] += 1

    def report(self):
        total = sum(self.sec.values()) or 1.0
        return {k: dict(seconds=round(self.sec[k], 1),
                        share=round(self.sec[k] / total, 3),
                        shots=self.shots[k])
                for k in ("gen", "arch", "clip")}


# ───────────────────────── ПЛАН КАДРОВ ─────────────────────────

def keywords_for(assets: Path, job):
    """
    Слова каждого файла материала — по ним подбирается кадр под текст.

    Генерация: слова промпта, по которому её нарисовали. Промпты лежат в
    спецификации по порядку, а файлы называются img_001, img_002 — связь
    прямая по номеру.

    Сток и архив: слова ЗАПРОСА, по которому файл скачан. Запрос пишется в
    манифест при скачивании. У материала, добранного до появления этого
    поля, запроса нет — тогда в ход идёт имя файла, где остался хотя бы
    источник. Это хуже, но не пусто, и ролик собирается.
    """
    # Ключ — НОМЕР файла, а не имя целиком. Имена различаются суффиксом:
    # генерация кладётся как img_001.jpg, сток как clip_003_pexels.mp4,
    # синтетика для отладки как img_001_mock.jpg. Совпадение по полному
    # имени молча промахивалось на всём, кроме генерации, и подбор по
    # смыслу давал ноль попаданий там, где он должен работать.
    def num(name: str):
        try:
            return int(name.split("_")[1])
        except (IndexError, ValueError):
            return None

    out = {}                       # (префикс, номер) -> слова
    prompts = job.get("image_prompts") or []
    for n, p in enumerate(prompts, 1):
        out[("img", n)] = words_of(p)

    # кадры добора (img_900+) лежат вне порядка сценария, их промпты
    # сохранены отдельно при генерации
    fill = assets / "images" / "_fill_prompts.json"
    if fill.exists():
        try:
            for k, p in enumerate(json.loads(fill.read_text(encoding="utf-8"))):
                out[("img", 900 + k)] = words_of(p)
        except json.JSONDecodeError:
            pass

    for folder in ("footage", "archive"):
        man = assets / folder / "_manifest.json"
        if not man.exists():
            continue
        try:
            rows = json.loads(man.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for row in rows:
            name = Path(row.get("file", "")).name
            n = num(name)
            if n is not None:
                out[(name.split("_")[0], n)] = words_of(row.get("q", ""))

    missing = 0
    for folder, pat in (("footage", "clip_*"), ("archive", "arch_*")):
        for p in (assets / folder).glob(pat):
            n = num(p.name)
            if n is None:
                continue
            if not out.get((p.name.split("_")[0], n)):
                out[(p.name.split("_")[0], n)] = words_of(p.stem.replace("_", " "))
                missing += 1
    if missing:
        log(f"  ! у {missing} файлов нет запроса в манифесте — "
            f"подбор по смыслу для них слабее")
    return out


def opening_plan(st, intro_start, intro_end):
    """
    Как обходится ВСТУПЛЕНИЕ при выпавшем типе открытия.

    Вынесено отдельно, потому что раньше это был самый заметный шаблон
    канала: поле opening вычислялось, защищалось от повторов, печаталось в
    сводку — и применялось ровно в двух местах из шести возможных. Все
    ролики открывались почти одинаково.

    Ни один вариант не двигает таймлайн: меняется раскладка кусков внутри
    вступления, а не его длина. Сдвиг здесь стоил бы рассинхрона звука на
    весь ролик.
    """
    o = st.opening
    p = dict(first_long=None, tight_until=0.0, tight_factor=1.0,
             ramp_until=0.0, first_is_clip=False, end=intro_end)

    if o == "long_establish":
        # один установочный кадр, потом перебивка — «выдох» перед бегом
        p["first_long"] = (5.5, 8.5)
    elif o == "quick_cuts":
        # первые четырнадцать секунд — самый короткий возможный шаг
        p["tight_until"] = intro_start + 14.0
        p["tight_factor"] = 0.30
    elif o == "cold_open":
        # без разгона вовсе: всё вступление на коротком шаге
        p["tight_until"] = intro_end
        p["tight_factor"] = 0.45
    elif o == "slow_reveal":
        # длинный медленный первый кадр, потом двадцать секунд разгона
        p["first_long"] = (6.0, 9.0)
        p["ramp_until"] = intro_start + 20.0
    elif o == "hard_in":
        # открывает видео на полном темпе, вступление короче обычного
        p["first_is_clip"] = True
        p["tight_until"] = intro_start + 8.0
        p["tight_factor"] = 0.35
        p["end"] = intro_start + (intro_end - intro_start) * 0.7
    # black_card ничего не меняет в раскладке: проявление из чёрного
    # делается фильтром на первой группе склейки, см. join()
    return p


def plan_shots(marks, st, assets, total, job_reject=None, job=None):
    """
    Раскладывает материал по таймлайну.

    ДВА РЕЖИМА, и это принципиально.

    Вступление режется НЕ по предложениям. Предложение у диктора длится в
    среднем пять с половиной секунд, а во вступлении нужны куски по две-
    четыре — уложить их в границы предложений физически нельзя.

    Тело режется по предложениям И ПО НАРРАТИВНЫМ ДОЛЯМ. Раньше здесь
    работала одна формула на весь ролик: база плюс замедление к финалу.
    Формула честная, но ФОРМА КРИВОЙ у всех загрузок канала была одна, и
    её видно на графике длительностей даже когда всё остальное разное.
    Теперь длительность просит editorial/pacing.py, а он смотрит на замысел
    доли из editorial/beats.py: под нагнетанием кадры укорачиваются, под
    развязкой стоят долго, после сильного куска ставится выдох.

    Границы кадров при этом по-прежнему падают на концы предложений: смена
    кадра в паузе речи читается как решение монтажёра, посреди фразы — как
    сбой. Это правило не менялось и меняться не должно.

    Возвращает список кадров. Разбор и кривые остаются на объекте st
    (st.beats, st.pacing) — оттуда их забирают проверка плана и монтажный
    лист, чтобы не считать одно и то же дважды.
    """
    def keep(paths, kind, rejected):
        """Выкидывает забракованное — роботом в vet.py или руками в reject."""
        out = [p for p in paths if int(p.name.split("_")[1]) not in rejected]
        if rejected:
            log(f"  {kind}: отклонено {len(paths) - len(out)} из {len(paths)}")
        return out

    # Отбраковка идёт из двух мест сразу. Робот (vet.py) отсеивает то, что не
    # по теме и что испорчено по форме; поле reject в спецификации остаётся
    # ручным довеском — им отменяют или дополняют решение робота, не трогая
    # код. Файлы при этом не удаляются ни в том, ни в другом случае.
    rej = dict(job_reject or {})
    auto = vet.rejected_from(assets)
    for kind, nums in auto.items():
        if nums:
            log(f"  {kind}: робот забраковал {len(nums)} — {nums}")
        rej[kind] = sorted(set(rej.get(kind, [])) | set(nums))

    images = sorted((assets / "images").glob("img_*.jpg"))
    archive = keep(sorted((assets / "archive").glob("arch_*.jpg")),
                   "архив", set(rej.get("arch", [])))
    clips = keep(sorted((assets / "footage").glob("clip_*.mp4")),
                 "футаж", set(rej.get("clip", [])))
    if not images:
        # Сюда попадать больше не должно: этап assets теперь падает сразу,
        # как только генерация вернула ноль (см. assets.main и fill_gaps).
        # Проверка остаётся на случай, когда монтаж запускают отдельно,
        # этапом render, по кэшу, собранному ещё старым кодом.
        raise SystemExit(
            f"в {assets / 'images'} нет ни одного img_*.jpg.\n"
            f"Монтаж без генерации собрать нельзя: она закрывает общие "
            f"планы, руки и интерьер, под которые нет ни стока, ни архива.\n"
            f"Причина почти всегда в этапе изображений — смотри его лог "
            f"выше по прогону.\n"
            f"Чинится перезапуском с stage=assets: озвучка и уже "
            f"скачанный материал возьмутся из кэша, заново уйдёт только "
            f"генерация.")

    anchors = set(st.anchor_positions(max(len(marks) // 3, 8)))
    shots, i, idx = [], 0, 0

    # Два отдельных списка вместо одного перемешанного. Раньше генерация и
    # архив склеивались в общий пул в пропорции «сколько чего скачалось», и
    # доля генерации получалась какой придётся: 40 картинок и 20 фото давали
    # две трети рисованного. Теперь пропорцией заведует MaterialMix, а
    # каждая семья ходит по своему списку — привязка позиции в списке к
    # позиции на таймлайне (пункт 10 задания) при этом сохраняется для обеих.
    # Слова каждого файла: у генерации — из промпта, по которому её рисовали,
    # у стока и архива — из запроса, по которому он скачан. По ним ShotPicker
    # и подбирает кадр под то, что звучит.
    kw = keywords_for(assets, job)

    def kw_of(p: Path):
        # РАЗБИРАЕТСЯ .stem, А НЕ .name. Иначе у генерации, чьё имя не имеет
        # третьего сегмента (img_001.jpg против clip_003_pexels.mp4),
        # split("_")[1] отдаёт "001.jpg", int() падает, и слова ВСЕЙ
        # генерации оказываются пустыми — она перестаёт подбираться по
        # смыслу вовсе и ложится под текст случайно. На ff-ep06 это дало
        # «подбор: генерация — 0 из 51 (0%)» при 35% экранного времени.
        #
        # Почему не ловилось раньше: у синтетики mock.py имя img_001_mock.jpg,
        # третий сегмент есть, int("001") проходит — smoke.py показывал
        # честные 50%, а боевой прогон молча давал ноль.
        try:
            stem = p.stem
            return kw.get((stem.split("_")[0], int(stem.split("_")[1])), set())
        except (IndexError, ValueError):
            return set()

    # Сколько раз каждый файл уже показывали В ПРОШЛЫХ роликах канала.
    # Внутрь ShotPicker это садится половинным весом: не запрет, а гандикап.
    prior = memory_mod.used_assets()
    if prior:
        log(f"  память канала: {len(prior)} файлов уже шли в эфир")

    gen_pick = ShotPicker([(p, "gen", kw_of(p)) for p in images], total, prior,
                         cap=MAX_IMAGE_REPEATS)
    arch_pick = ShotPicker([(p, "arch", kw_of(p)) for p in archive], total, prior,
                          cap=MAX_IMAGE_REPEATS)
    clip_pick = ShotPicker([(p, "clip", kw_of(p)) for p in clips], total, prior)

    # ── РАЗБОР СЦЕНАРИЯ НА ДОЛИ ──────────────────────────────────────
    # Считается по тайм-кодам и по тексту, без единого запроса к модели:
    # разбор идёт на каждой пересборке монтажа, а пересборок у ролика пять
    # -десять. Платный разбор означал бы, что правка одной склейки стоит
    # как новый ролик.
    script_blocks = (job or {}).get("script_blocks") or []
    story = beats_mod.analyze(marks, script_blocks, total)
    for row in beats_mod.report(story):
        log(row)
    pace = pacing_mod.Pacing(st.rng, story, total, st.base_dur,
                             arc_name=st.arc, breath_rate=st.breath_rate)
    for row in pace.report():
        log(row)
    # оставляем на движке: их заберут проверка плана и монтажный лист
    st.beats, st.pacing = story, pace

    def beat_at(t: float):
        """Доля, накрывающая секунду t, и её номер. Хвост — последней доле."""
        for n, b in enumerate(story):
            if b.start - 0.01 <= t < b.end:
                return n, b
        return (len(story) - 1, story[-1]) if story else (0, None)

    clip_cap_hit = [False]

    def repeat_move(times_before: int):
        """Ход камеры для клипа: на первом показе нет, дальше разный."""
        if times_before <= 0:
            return None
        return CLIP_REPEAT_MOVES[(times_before - 1) % len(CLIP_REPEAT_MOVES)]

    def clip_available():
        """Ложь, когда пул стока исчерпан по MAX_CLIP_REPEATS — см. константу."""
        if not clip_pick.exhausted(MAX_CLIP_REPEATS):
            return True
        if not clip_cap_hit[0]:
            clip_cap_hit[0] = True
            log(f"  сток: весь пул ({len(clips)}) показан по {MAX_CLIP_REPEATS} "
                f"раз — дальше слоты видео уходят фото и генерации")
        return False

    gen_cap_hit, arch_cap_hit = [False], [False]

    def gen_available():
        """Ложь, когда ВСЯ генерация показана по MAX_IMAGE_REPEATS раз.

        Пул картинок и архива исчерпывается редко (их обычно на порядок
        больше, чем стока), но на бедном по теме ролике — как раз тот
        случай, что и привёл к правке. Слот в этом случае не пропадает:
        MaterialMix.pick() уходит в оставшийся разрешённый вид, а если и
        тот исчерпан — score() внутри ShotPicker всё равно вернёт
        файл, просто не отдавая ему предпочтения (см. over_cap).
        """
        if not gen_pick or not gen_pick.exhausted(MAX_IMAGE_REPEATS):
            return True
        if not gen_cap_hit[0]:
            gen_cap_hit[0] = True
            log(f"  генерация: весь пул ({len(images)}) показан по "
                f"{MAX_IMAGE_REPEATS} раз — добери stage: assets или подними "
                f"generated_share")
        return False

    def arch_available():
        """Ложь, когда весь архив показан по MAX_IMAGE_REPEATS раз."""
        if not arch_pick or not arch_pick.exhausted(MAX_IMAGE_REPEATS):
            return True
        if not arch_cap_hit[0]:
            arch_cap_hit[0] = True
            log(f"  архив: весь пул ({len(archive)}) показан по "
                f"{MAX_IMAGE_REPEATS} раз — добери stage: material по "
                f"archive_queries")
        return False

    mix = MaterialMix(st.generated_share, bool(images), bool(archive),
                      bool(clips))
    if not archive:
        log("  ! подлинных фото нет — под предметы пойдёт генерация; "
            "добери архив этапом material")
    if not clips:
        log("  ! стокового видео нет — вступление будет из одних фотографий")

    def put_image(kind, t_pos, said="", **extra):
        """Ставит кадр-картинку нужной семьи и записывает его в счёт."""
        src, tag = (gen_pick if kind == "gen" else arch_pick).take(t_pos, said)
        fr_name, fr = st.framing(src.name)
        return dict(kind="image", file=src, tag=tag,
                    framing=fr, framing_name=fr_name, **extra)

    def said_at(t_pos: float, span: float = 8.0) -> str:
        """Что звучит в эту секунду — текст предложений, накрывающих кадр."""
        return " ".join(m["text"] for m in marks
                        if m["end"] > t_pos and m["start"] < t_pos + span)

    # ВСТУПЛЕНИЕ. Первые минуты — быстрая перебивка: короткие куски видео
    # вперемешку с фотографиями. Ролик, который открывается статичной
    # картинкой на двенадцать секунд, зритель закрывает на первой минуте;
    # но и подряд идущие длинные стоковые клипы работают ровно так же.
    #
    # ЗДЕСЬ КАДР РЕЖЕТСЯ НЕ ПО ПРЕДЛОЖЕНИЯМ. Это сознательное отступление
    # и только для вступления. Предложение у диктора длится в среднем пять
    # с половиной секунд, а нужны куски по две-четыре — уложить их в границы
    # предложений физически нельзя. В теле ролика правило остаётся: там
    # склейки по-прежнему падают в паузы речи.
    #
    # Потолок в треть хронометража нужен ради тестов: на настоящем ролике
    # он не срабатывает и вступление длится ровно заказанные секунды.
    # Вступление идёт и без стокового видео: тогда это быстрая перебивка из
    # одних фотографий с обязательным движением. Раньше при пустой папке
    # футажа вступления не было вовсе, и ролик открывался кадром на семь
    # секунд — то есть ровно тем, от чего вступление и спасает.
    intro_end = min(st.intro_footage_seconds, total * 0.35)

    since_clip = 0       # сколько кадров-картинок подряд уже прошло
    next_gap = st.body_clip_every_n_shots
    cutter = ClipCutter()

    # --- вступление ---
    t = marks[0]["start"] if marks else 0.0
    intro_start = t
    op = opening_plan(st, intro_start, intro_end)
    intro_end = op["end"]
    log(f"  открытие: {st.opening}, вступление до {intro_end:.0f} с")

    run_kind, run_len = None, 0
    while t < intro_end:
        # чередуем видео и фото, но не даём трём одинаковым идти подряд
        want_clip = st.rng.random() < st.intro_clip_share and clip_available()
        if run_len >= 2:
            want_clip = run_kind != "clip" and clip_available()
        # hard_in открывает стоковым видео: ролик стартует движением, а не
        # фотографией. Если стока нет, правило молча уступает — вступление
        # из одних фотографий лучше, чем отсутствие вступления.
        if idx == 0 and op["first_is_clip"] and clip_available():
            want_clip = True
        kind = "clip" if want_clip else "image"
        run_len = run_len + 1 if kind == run_kind else 1
        run_kind = kind

        rng_pair = (st.intro_clip_duration_range if kind == "clip"
                    else st.intro_photo_duration_range)
        dur = round(st.rng.uniform(*rng_pair), 3)

        # ТИП ОТКРЫТИЯ, шесть вариантов вместо прежних трёх. Расходятся они
        # длительностями первых кадров, первым материалом и проявлением из
        # чёрного; таймлайн при этом не сдвигается ни на кадр, поэтому звук
        # остаётся на месте (сдвиг стоил бы рассинхрона на весь ролик).
        lo, hi = rng_pair
        if idx == 0 and op["first_long"]:
            dur = round(st.rng.uniform(*op["first_long"]), 3)
        elif t < op["tight_until"]:
            dur = round(min(dur, lo + (hi - lo) * op["tight_factor"]), 3)
        elif t < op["ramp_until"]:
            # разгон: от длинного шага к короткому за отведённые секунды
            k = (t - intro_start) / max(op["ramp_until"] - intro_start, 0.1)
            dur = round(hi - (hi - lo) * min(1.0, k), 3)

        tr, trd = st.pick_transition(short=True)
        # переход во вступлении короткий: длинное растворение съедает
        # весь смысл быстрой перебивки

        # Слот выбран (видео или фото), но чем именно его закрыть — решает
        # пропорция. Во вступлении она работает так же, как в теле: иначе
        # первые три минуты, самые смотримые, съезжали бы в генерацию.
        allowed = (["clip"] if kind == "clip" else
                  (["gen"] if gen_available() else []) +
                  (["arch"] if arch_available() else []))
        got = mix.pick(allowed)

        if got == "clip":
            src, _ = clip_pick.take(t, said_at(t, dur))
            shots.append(dict(kind="clip", file=src, tag="clip",
                              src_start=cutter.take_start(src, dur),
                              move=repeat_move(clip_pick.last_repeat),
                              start=round(t, 3), duration=dur,
                              transition=tr, transition_dur=trd,
                              effect=st.effect("hook"),
                              beat_kind="hook", why=f"вступление ({st.opening})"))
        else:
            # Во вступлении по фотографии всегда идёт скольжение или наезд,
            # статики тут быть не должно. Набор ограничивается ПАРАМЕТРОМ,
            # а не подменой результата: раньше движение бралось из движка и
            # перевыбиралось здесь своим жребием, если не подошло, — мимо
            # счётчика семей. Проверка плана нашла на этом девять наездов
            # подряд в первых трёх минутах.
            mv, sp = st.pick_move(1.05, allow_hold=False, only=INTRO_MOVES)
            shots.append(put_image(
                got, t, said=said_at(t, dur), start=round(t, 3), duration=dur,
                move=mv, speed=sp,
                transition=tr, transition_dur=trd,
                effect=st.effect("hook"),
                beat_kind="hook", why=f"вступление ({st.opening})"))
        mix.charge(got, dur)
        t += dur
        idx += 1

    # Тело начинается с предложения, которое в момент t ЕЩЁ ЗВУЧИТ, а не с
    # первого начавшегося после t.
    #
    # Разница не косметическая. Если предложение длинное и накрывает границу
    # вступления, то, пропустив его, робот получает первый кадр тела уже
    # после его конца — а дыру закрывает последним кадром вступления, растянув
    # его. На тестовом прогоне вступление из кусков по три секунды кончалось
    # кадром на шестнадцать секунд: ровно та статика, ради ухода от которой
    # перебивка и делалась.
    while i < len(marks) - 1 and marks[i]["end"] <= t:
        i += 1
    # но начать раньше уже поставленного кадра нельзя — таймлайн не идёт назад
    if shots and marks[i]["start"] <= shots[-1]["start"]:
        i = min(i + 1, len(marks) - 1)

    # --- тело ---
    #
    # Длительность кадра просит НЕ формула по позиции на таймлайне, а
    # editorial/pacing.py — он смотрит на замысел доли, на форму арки ролика
    # и на микроритм внутри доли. Формула осталась запасным путём: если
    # разбор не дал ни одной доли (пустые тайм-коды, странный сценарий),
    # план обязан собраться хоть как-то.
    body_seconds = max(total - intro_end, 1.0)
    est_body_shots = max(8, int(body_seconds / max(st.base_dur, 1.0)))
    body_idx = 0

    while i < len(marks):
        start_probe = marks[i]["start"]
        bi, beat = beat_at(start_probe)
        # since_clip — сколько кадров-картинок подряд идёт без вставки видео.
        # Он уже ведётся ниже для чередования материала; здесь тот же счётчик
        # работает вторую службу: длинный однородный ряд поднимает
        # вероятность эффекта (см. style.effect).
        #
        # seam — первый кадр тела, сразу за вступлением. Классическое место
        # ухода зрителя: перебивка кончилась, начался «обычный» ролик.
        seam = (body_idx == 0)
        if beat is not None:
            cfg = st.shot_for(beat, pace, bi, start_probe,
                              same_run=since_clip, seam=seam)
            is_anchor = False        # выдохи из pacing делают ту же работу
        else:
            is_anchor = idx in anchors
            cfg = st.clip(body_idx, est_body_shots, is_anchor=is_anchor,
                          same_run=since_clip, seam=seam)
        want = cfg["duration"]

        first = i
        start = marks[i]["start"]
        # ищем границу предложения БЛИЖАЙШУЮ к желаемой длительности,
        # а не первую её превышающую — иначе кадры systematически
        # получаются длиннее задуманного
        #
        # ПАУЗА ПОСЛЕ ПРЕДЛОЖЕНИЯ ПЕРЕВЕШИВАЕТ. Из двух границ, одинаково
        # близких к желаемой длительности, выбирается та, после которой
        # диктор молчит дольше. Смена кадра в тишине читается как решение
        # монтажёра; смена ровно на первом слове следующей фразы — как
        # случайность, даже когда она попадает в границу предложения.
        # Бонус ограничен секундой: длинная пауза не должна перетягивать
        # кадр далеко от заказанной длины.
        def gap_after(idx_mark):
            if idx_mark + 1 >= len(marks):
                return 0.0
            return max(0.0, marks[idx_mark + 1]["start"] - marks[idx_mark]["end"])

        def cost(idx_mark):
            err = abs(marks[idx_mark]["end"] - start - want)
            return err - min(gap_after(idx_mark), 1.0) * 0.8

        j, best, best_err = i, i, cost(i)
        while j < len(marks) - 1 and marks[j]["end"] - start < want * 1.6:
            j += 1
            err = cost(j)
            if err < best_err:
                best, best_err = j, err
        end = marks[best]["end"]
        i = best + 1

        # Кому достаётся кадр — футажу или картинке.
        #
        # Во вступлении картинок нет вообще: пока не кончились заказанные
        # секунды, каждый кадр это видео.
        #
        # Дальше по телу вставка идёт примерно каждый n-й кадр. Именно
        # ПРИМЕРНО: шаг гуляет на единицу в обе стороны, потому что ровный
        # ритм «через три на четвёртый» читается как машинный так же
        # отчётливо, как нарезка по секундомеру.
        #
        # Якорный кадр под футаж не отдаём: он намеренно длинный, до двадцати
        # секунд, а сток редко бывает длиннее пятнадцати и уходил бы в петлю.
        # Долгий кадр — это всегда изображение.
        dur = round(end - start, 3)
        # Доля решает, уместен ли здесь сток вообще. Под развязку сток почти
        # не идёт: там на экране должен быть конкретный предмет, а не
        # абстрактные руки в темноте из чужого стока. Под нагнетание,
        # наоборот, идёт охотно — там нужно движение.
        beat_wants_clip = (st.rng.random() < pace.clip_share(beat) * 2.0
                           if beat is not None else True)
        clip_ok = (not is_anchor and since_clip >= next_gap
                   and beat_wants_clip
                   and dur <= CLIP_MAX_SECONDS and clip_available())
        got = mix.pick((["clip"] if clip_ok else []) +
                      (["gen"] if gen_available() else []) +
                      (["arch"] if arch_available() else []))

        said = " ".join(m["text"] for m in marks[first:best + 1])
        meta = dict(why=cfg.get("why", ""), beat_kind=cfg.get("beat_kind"))

        if got == "clip":
            src, _ = clip_pick.take(start, said)
            shots.append(dict(kind="clip", file=src, tag="clip",
                              src_start=cutter.take_start(src, dur),
                              move=repeat_move(clip_pick.last_repeat),
                              start=start, duration=dur, **meta,
                              **{k: cfg[k] for k in
                                 ("transition", "transition_dur", "effect")}))
            since_clip = 0
            n = st.body_clip_every_n_shots
            next_gap = max(1, n + st.rng.choice([-1, 0, 0, 1]))
        else:
            since_clip += 1
            shots.append(put_image(
                got, start, said=said, start=start, duration=dur, **meta,
                **{k: cfg[k] for k in
                   ("move", "speed", "transition", "transition_dur",
                    "effect")}))
        mix.charge(got, dur)
        idx += 1
        body_idx += 1

    # Кадр держится до НАЧАЛА следующего, а не до конца своего последнего
    # предложения. Границы остаются теми же (это те же тайм-коды), но паузы
    # между предложениями теперь покрыты кадром. Иначе они не покрыты ничем,
    # и картинка уезжает вперёд звука на сумму всех пауз.
    for cur, nxt in zip(shots, shots[1:]):
        cur["duration"] = round(nxt["start"] - cur["start"], 3)
    # хвост: последний кадр дотягиваем до конца звука
    if shots:
        shots[-1]["duration"] = round(max(total - shots[-1]["start"], 0.1), 3)

    # Попадание по смыслу — величина, которую надо видеть. Низкая доля
    # означает, что запросы к стокам написаны словами, которых нет в
    # сценарии, и кадры ложатся под текст случайно.
    log(f"  подбор: генерация — {gen_pick.report()}")
    log(f"  подбор: архив     — {arch_pick.report()}")
    log(f"  подбор: сток      — {clip_pick.report()}")

    # Что показано и сколько раз — уедет в журнал канала, чтобы следующий
    # ролик начал подбор с гандикапом на эти файлы. Генерация не считается:
    # картинки рисуются под конкретный сценарий и в других роликах не
    # встречаются в принципе.
    st.assets_used = {**arch_pick.used_names(), **clip_pick.used_names()}
    return shots


def material_report(shots):
    """
    Что в итоге получилось по видам материала.

    Считается по ГОТОВОМУ плану, а не по счётчикам MaterialMix: длительности
    кадров правятся после раскладки (кадр тянется до начала следующего), и
    доли, посчитанные до этой правки, врут на секунды. Печатать надо то, что
    в ролике, а не то, что задумывалось — иначе проверка замером теряет смысл.
    """
    sec = {"gen": 0.0, "arch": 0.0, "clip": 0.0}
    cnt = {"gen": 0, "arch": 0, "clip": 0}
    for s in shots:
        tag = s.get("tag") or ("clip" if s["kind"] == "clip" else "gen")
        sec[tag] = sec.get(tag, 0.0) + s["duration"]
        cnt[tag] = cnt.get(tag, 0) + 1
    total = sum(sec.values()) or 1.0
    return sec, cnt, total


# ───────────────────────── ПАУЗЫ ПЕРЕД ГЛАВАМИ ─────────────────────────
#
# Работают ПОСЛЕ plan_shots(), не внутри неё: список кадров уже расставлен
# по смыслу (beats/pacing/variation), карточка — чисто технический сдвиг
# готового плана, а не редакторское решение. Так правка не задевает ни
# один из модулей pipeline/editorial — они по-прежнему видят и разбирают
# ИСХОДНЫЙ, беспаузный сценарий, ровно как до этой правки.

def chapter_boundaries(job, beats, total):
    """
    Момент начала каждой НОВОЙ истории (кроме самой первой) и её название.

    Названия берутся из youtube.chapters — той же карты, что уже пишет
    описание ролика на YouTube (см. youtube.chapters()). Второй источник
    времени заводить не пришлось: beats.py уже знает, какой доле какой
    script_blocks она принадлежит (Beat.block), а первая доля нового
    block и есть момент, откуда начинается следующая история.
    """
    names = (job.get("youtube") or {}).get("chapters") or []
    blocks = job.get("script_blocks") or []
    if not names or len(names) != len(blocks) or not beats:
        return []
    first_start = {}
    for b in beats:
        if b.block not in first_start or b.start < first_start[b.block]:
            first_start[b.block] = b.start
    out = [(first_start[i], names[i]) for i in range(1, len(blocks))
           if i in first_start]
    return sorted(out)


def _shift_at(t: float, boundaries, pause: float) -> float:
    """Сколько секунд паузы уже накопилось ДО момента t (t включительно)."""
    return pause * sum(1 for bt, _ in boundaries if bt <= t)


def insert_chapter_cards(shots, boundaries, total, pause=CHAPTER_PAUSE):
    """
    Вставляет кадр-карточку на каждой границе и раздвигает таймлайн.

    Сдвиг НЕ последовательный (сдвинуть, потом сдвинуть ещё), а посчитан
    для каждого кадра сразу по его ИСХОДНОМУ времени — иначе кадр у самой
    границы дважды попадает под сдвиг соседней и уезжает не на ту сумму.

    Длительности после вставки считаются ТЕМ ЖЕ способом, что и в конце
    plan_shots (до старта следующего кадра): другой способ здесь завёл бы
    рассинхрон между этой функцией и той, а они обязаны давать одно и то
    же на кадрах, которых сдвиг не коснулся.
    """
    if not boundaries:
        return shots, total
    out = []
    for sh in shots:
        sh = dict(sh)
        sh["start"] = round(sh["start"] + _shift_at(sh["start"], boundaries, pause), 3)
        out.append(sh)
    for i, (t_orig, name) in enumerate(boundaries):
        # НЕ ставить move/speed/framing/effect даже в None: rails.metrics()
        # читает их как s.get("speed", 1.0) — а .get() отдаёт дефолт только
        # когда ключа НЕТ вовсе, не когда он есть и равен None. Явный
        # speed=None здесь один раз уже уронил статистику stdev по всему
        # ролику (TypeError на statistics.pstdev с None в списке чисел).
        # Просто не заводить ключ — и любой .get(key) или .get(key, default)
        # у любого потребителя отработает как для кадра без этого поля.
        out.append(dict(
            kind="card", tag="card", file=Path(f"chapter_card_{i + 1}.png"),
            start=round(t_orig + pause * i, 3), duration=pause,
            transition="fade", transition_dur=0.7,
            beat_kind="card", why=f"пауза перед главой «{name}»", card_text=name))
    out.sort(key=lambda s: s["start"])
    for cur, nxt in zip(out, out[1:]):
        cur["duration"] = round(nxt["start"] - cur["start"], 3)
    new_total = round(total + pause * len(boundaries), 3)
    if out:
        out[-1]["duration"] = round(max(new_total - out[-1]["start"], 0.1), 3)
    return out, new_total


def shift_marks(marks, boundaries, pause=CHAPTER_PAUSE):
    """Тайм-коды слов на новую, раздвинутую пауза́ми шкалу времени.

    Нужна субтитрам (render.write_srt) и marks_final.json для шортсов —
    оба читают финальный ролик, а в нём пауза уже настоящая тишина, и
    без сдвига любое слово после первой же паузы поехало бы вперёд звука
    на её длину, а после второй — на сумму двух, и так далее.
    """
    if not boundaries:
        return marks
    out = []
    for m in marks:
        m = dict(m)
        m["start"] = round(m["start"] + _shift_at(m["start"], boundaries, pause), 3)
        m["end"] = round(m["end"] + _shift_at(m["end"], boundaries, pause), 3)
        out.append(m)
    return out


def voice_with_pauses(voice: Path, boundaries, pause: float, tmp: Path) -> Path:
    """
    Копия начитки с настоящей тишиной на границах глав.

    Кэш (assets/voice_full.m4a) не трогается — режется КОПИЯ во временной
    папке. Тишина — отдельный файл anullsrc, куски голоса вырезаются
    -ss/-t с перекодированием (не stream copy): точность реза важнее
    скорости, а на получасовой начитке перекодировать пять-шесть кусков
    и один синус тишины — секунды, не минуты.
    """
    silence = tmp / "_chapter_silence.m4a"
    if not silence.exists():
        subprocess.run(
            f"ffmpeg -y -f lavfi -i anullsrc=r=48000:cl=stereo -t {pause:.3f} "
            f"-c:a aac -b:a 192k {shlex.quote(str(silence))}",
            shell=True, check=True, capture_output=True)
    pieces, prev_t = [], 0.0
    for k, (t_orig, _name) in enumerate(boundaries):
        seg = tmp / f"_voice_seg_{k:02d}.m4a"
        if not seg.exists():
            subprocess.run(
                f"ffmpeg -y -ss {prev_t:.3f} -i {shlex.quote(str(voice))} "
                f"-t {t_orig - prev_t:.3f} -c:a aac -b:a 192k "
                f"{shlex.quote(str(seg))}",
                shell=True, check=True, capture_output=True)
        pieces += [seg, silence]
        prev_t = t_orig
    tail = tmp / "_voice_tail.m4a"
    if not tail.exists():
        subprocess.run(
            f"ffmpeg -y -ss {prev_t:.3f} -i {shlex.quote(str(voice))} "
            f"-c:a aac -b:a 192k {shlex.quote(str(tail))}",
            shell=True, check=True, capture_output=True)
    pieces.append(tail)
    out = tmp / "voice_paused.m4a"
    render.concat_segments(pieces, out)
    return out


# ───────────────────────── НАСТРОЙКИ ИЗ JSON ─────────────────────────

# Что можно переопределить блоком style_override в спецификации ролика.
# Ключ слева — как это называется в JSON, значение — поле StyleEngine.
# Список закрытый намеренно: опечатка в имени должна быть замечена, а не
# молча проигнорирована.
OVERRIDABLE = {
    "haze_enabled":              "haze_enabled",
    "sparks_enabled":            "sparks_enabled",
    "spark_opacity":             "spark_opacity",
    "spark_speed_px_sec":        "spark_speed_px_sec",
    "spark_flicker":             "spark_flicker",
    "grain":                     "grain",
    "vignette":                  "vignette",
    "transitions":               "transitions",
    "transition_duration_range": "tr_dur_range",
    "hard_cut_probability":      "hard_cut_probability",
    "intro_footage_seconds":     "intro_footage_seconds",
    "intro_clip_duration_range": "intro_clip_duration_range",
    "intro_photo_duration_range": "intro_photo_duration_range",
    "intro_clip_share":          "intro_clip_share",
    "intro_transition_duration_range": "intro_transition_duration_range",
    "body_clip_every_n_shots":   "body_clip_every_n_shots",
    "effects_enabled":           "effects_enabled",
    "effects":                   "effects",
    "effect_probability":        "effect_probability",
    # Цветокор ролика. Без этого поля он выпадает случайно из тёплой семьи
    # LUTS в style.py.
    "lut":                       "lut",
    "archive_lut":               "archive_lut",
    # Доля экранного времени под генерацию. Остальное — сток и архив.
    "generated_share":           "generated_share",
    # Сжатие: упирается в лимит GitHub Releases в 2 ГБ, см. style.py.
    "crf":                       "crf",
    "preset":                    "preset",
    # ── оси редакторского слоя ───────────────────────────────────────
    # Штатно они выпадают жребием и разводятся с историей канала. Здесь
    # они открыты для ручной правки: иногда под конкретный сюжет нужна
    # заведомо определённая арка или заведомо отсутствующие плашки, и
    # переписывать ради этого код не надо.
    #
    # ВНИМАНИЕ: жёстко заданная ось перестаёт участвовать в разведении.
    # Прибив здесь arc и opening, вы вернёте канал ровно к тому состоянию,
    # из-за которого весь этот слой и появился.
    "arc":                       "arc",
    "opening":                   "opening",
    "motion_amp":                "motion_amp",
    "motion_bias":               "motion_bias",
    "framing_bias":              "framing_bias",
    "transition_focus":          "transition_focus",
    "shot_spread":               "shot_spread",
    "breath_rate":               "breath_rate",
    "text_style":                "text_style",
    "text_density":              "text_density",
    "duck_style":                "duck_style",
    "duck_depth":                "duck_depth",
}

# Оси со списком допустимых значений. Опечатка должна ронять сборку сразу,
# а не превращаться в тихо неработающую настройку: именно так поле opening
# однажды прожило несколько роликов, ни на что не влияя.
ENUMS = {
    "arc": tuple(pacing_mod.ARCS),
    "opening": style_mod.OPENINGS,
    "motion_bias": tuple(style_mod.MOTION_BIAS),
    "framing_bias": tuple(style_mod.FRAMING_BIAS),
    "text_style": ("none",) + textcard.STYLES,
    "duck_style": ("revelation", "beats", "sparse", "breath"),
}

# Поля, которые не присваиваются напрямую, а перебрасывают жребий.
# base_dur и decel рисуются один раз при создании движка и задают темп всего
# ролика; поменять их можно только новым броском в заданных границах.
REDRAW = {
    "base_duration_range": "base_dur",
    "deceleration_range":  "decel",
}

PAIRS = {"spark_speed_px_sec", "spark_flicker", "transition_duration_range",
         "intro_clip_duration_range", "intro_photo_duration_range",
         "intro_transition_duration_range"}


def apply_style_override(st, job):
    """
    Накладывает блок style_override из спецификации поверх StyleEngine.

    Смысл ровно один: менять картинку правкой JSON, без правки кода.
    Блока нет — всё работает на умолчаниях, как раньше.

    fps стоит особняком. Это не поле стиля, а константа модуля render,
    от которой считаются и длина клипа в кадрах, и запас PAD, и частота
    слоя искр. Поэтому её правим до того, как что-либо из этого посчитано,
    и правим во всех трёх местах сразу.
    """
    ov = job.get("style_override") or {}
    if not ov:
        return st

    # ключи с подчёркивания — комментарии, так заведено в самих спецификациях
    unknown = [k for k in ov
               if not k.startswith("_") and k not in OVERRIDABLE
               and k not in REDRAW and k != "fps"]
    if unknown:
        raise SystemExit(
            "style_override: неизвестные поля " + ", ".join(sorted(unknown)) +
            "\nдопустимые: " + ", ".join(
                sorted(list(OVERRIDABLE) + list(REDRAW) + ["fps"])))

    if "fps" in ov:
        fps = int(ov["fps"])
        render.FPS = fps
        globals()["FPS"] = fps
        globals()["PAD"] = round(2 / fps, 3)
        log(f"  fps -> {fps}")

    # Бросок в новых границах. Случайность сохраняется — уходит только
    # привязка к диапазону, зашитому в код.
    for key, field in REDRAW.items():
        if key in ov:
            lo, hi = ov[key]
            setattr(st, field, round(st.rng.uniform(float(lo), float(hi)), 3))
            log(f"  {key} -> {ov[key]} => {getattr(st, field)}")

    for key, field in OVERRIDABLE.items():
        if key not in ov:
            continue
        val = ov[key]
        if key in PAIRS:
            val = tuple(val)
        if key == "effects":
            bad = [e for e in val if e not in style_mod.EFFECTS]
            if bad:
                raise SystemExit(
                    "style_override.effects: нет таких эффектов " +
                    ", ".join(bad) + "\nесть: " +
                    ", ".join(sorted(style_mod.EFFECTS)))
        if key in ENUMS and val not in ENUMS[key]:
            raise SystemExit(
                f"style_override.{key}: значение {val!r} недопустимо"
                f"\nесть: " + ", ".join(map(str, ENUMS[key])))
        setattr(st, field, val)
        log(f"  {key} -> {val}")
    return st


def check_luts(st):
    """
    Оба цветокора должны существовать на диске ДО начала рендера.

    ffmpeg на отсутствующий .cube ругается уже внутри группы склейки, а
    stderr там заглушен — на выходе получается голое «код возврата 1» после
    получаса рендера кадров.
    """
    missing = [name for name in (st.lut, st.archive_lut)
               if not (LUTS / f"{name}.cube").exists()]
    if missing:
        have = sorted(p.stem for p in LUTS.glob("*.cube"))
        raise SystemExit(
            "нет цветокоров: " + ", ".join(missing) +
            "\nесть: " + ", ".join(have) +
            "\n(таблицы считает pipeline/make_luts.py)")


def xfade_dur(shot):
    """Сколько таймлайна съест переход после этого кадра."""
    return 0.04 if shot["transition"] == "cut" else shot["transition_dur"]


PAD = round(2 / FPS, 3)     # два кадра запаса в хвосте каждого клипа


def set_render_durations(shots):
    """
    xfade склеивает соседние кадры ВНАХЛЁСТ: каждый переход вычитает свою
    длительность из общего таймлайна. Если рендерить кадр ровно на его
    предложения, картинка уходит вперёд звука на сумму всех переходов —
    на тестовом ролике это 8 секунд из 67, на 38-минутном около шести
    минут, и -shortest в mux молча срезает конец начитки.

    Поэтому кадр рендерится длиннее: предложения + переход + два кадра
    запаса. Запас обязателен. ffmpeg отдаёт целое число кадров и округляет
    длину ВНИЗ, то есть клип выходит короче заказанного почти на кадр.
    xfade при этом просит кадры за концом клипа и молча обрывает всю группу:
    на прогоне это дало 13.9 секунды вместо 114.8 при нулевом коде возврата.
    Особенно легко ловится на 'cut' — там нахлёст 0.04 с приходится ровно
    на последний кадр.

    Хвост запаса не виден: xfade отбрасывает всё, что осталось от кадра
    после перехода. У последнего кадра группы перехода нет (группы сшиваются
    встык), поэтому и запаса нет — иначе он оказался бы на экране.
    """
    for gi in range(0, len(shots), SEG_SIZE):
        group = shots[gi:gi + SEG_SIZE]
        for k, sh in enumerate(group):
            extra = 0.0 if k == len(group) - 1 else xfade_dur(sh) + PAD
            sh["render_dur"] = round(sh["duration"] + extra, 3)


# ───────────────────────── РЕНДЕР ─────────────────────────

def render_one(args):
    n, sh, tmp = args
    out = tmp / f"clip_{n:04d}.mp4"
    if out.exists():
        return out
    if sh["kind"] == "card":
        render.render_card(sh["card_text"], out, sh["render_dur"])
    elif sh["kind"] == "clip":
        render.render_footage_clip(Path(sh["file"]), out, sh["render_dur"],
                                   start=sh.get("src_start", 0.0),
                                   effect=sh.get("effect"),
                                   move=sh.get("move"))
    else:
        prep = tmp / f"prep_{n:04d}.jpg"
        render.prepare_image(Path(sh["file"]), prep, sh["framing"])
        render.render_clip(prep, out, sh["move"], sh["speed"], sh["render_dur"],
                           effect=sh.get("effect"))
        prep.unlink(missing_ok=True)
    return out


def grade_for(shot, st):
    """
    Цветокор ОДНОГО кадра.

    Архивный грейд получает только подлинное фото из архива. Генерация и
    стоковое видео идут по семейному, тёплому: современный сток, покрашенный
    в сепию, читается не как хроника, а как подделка под старину.

    Раньше эта функция вызывалась один раз на группу из двенадцати кадров и
    красила всю группу по ПЕРВОМУ кадру. Пока генерации было большинство,
    ошибка была редкой и незаметной. При доле генерации в 30% большинство
    групп начинается реальным материалом — и весь смысл двух цветокоров
    пропадал: архив красился семейным, генерация архивным, через кадр
    вперемешку. Теперь грейд лежит на каждом входе отдельно, см. join().
    """
    if shot.get("tag") == "arch":
        return LUTS / f"{st.archive_lut}.cube"
    return LUTS / f"{st.lut}.cube"


def film_look():
    """
    Киношная приглушённая база поверх LUT.

    curves приподнимает тени: чёрный перестаёт быть провалом и становится
    матовой плёночной растяжкой — именно это отличает «кино» от «фото с
    телефона». Верх подтянут вниз, чтобы белое не выбивало.
    eq снимает насыщенность: атмосфера приглушённая, а не открыточная.

    РАЗВОДКА ПЕРЕВЁРНУТА против исходного проекта. Там красный поднимался
    слабее синего, тень уходила в холод и получалась синяя тень с тёплым
    светом — правильная картинка для сонного исторического канала. Здесь
    наоборот: тень тёплая, коричневая, а холод не появляется нигде. Лавка
    древностей освещена лампой накаливания, у неё синих теней не бывает.
    Насыщенность приспущена слабее прежнего (0.88 против 0.82): тёплая
    картинка при 0.82 выцветает в грязно-бежевую.

    ПОРЯДОК ЗДЕСЬ ВАЖЕН и стоил отдельного замера. Сначала eq, потом curves.
    Наоборот не работает: contrast у eq утягивает тени вниз сильнее, чем
    curves их поднимает, и вместо матовой растяжки получается ровно то же
    проваленное чёрное, только с потерянной насыщенностью. Замер на тестовом
    кадре: тени 31 -> 26 при обратном порядке против 31 -> 41 при этом.

    Одна строка на весь ролик, крутится здесь. Теплее — поднимаем первое
    число у r и опускаем у b, глубже тени — уменьшаем оба.
    """
    return (
        "eq=saturation=0.88:contrast=0.97,"
        "curves=r='0/0.072 0.5/0.508 1/0.972'"
        ":g='0/0.058 0.5/0.500 1/0.962'"
        ":b='0/0.042 0.5/0.492 1/0.934'"
    )


def text_for_group(group, moments):
    """
    Плашки, попадающие в эту группу склейки, с пересчётом в локальное время.

    Группа рендерится отдельным файлом, и время внутри неё идёт от нуля.
    Абсолютная секунда плашки известна из плана, начало группы — из первого
    её кадра; разница и есть локальное время. Считать это где-то ещё нельзя:
    только здесь известно, какие кадры попали в какую группу.
    """
    if not moments or not group:
        return []
    g0 = group[0]["start"]
    g1 = group[-1]["start"] + group[-1]["duration"]
    out = []
    for m in moments:
        if g0 <= m["t"] < g1:
            it = dict(m)
            it["t_local"] = max(0.0, m["t"] - g0)
            out.append(it)
    return out


def join(group, out: Path, st, sparks, first=False, moments=None):
    ins = " ".join(f'-i "{c["file"]}"' for c in group)
    if sparks is not None:
        ins += f' -stream_loop -1 -i "{sparks}"'
    sp = len(group)

    # Цветокор ложится НА КАЖДЫЙ ВХОД до склейки, а не на готовую группу
    # после неё. Иначе вся дюжина кадров красится по первому из них, и
    # архивное фото получает семейный грейд, а генерация — архивный.
    # Считается это ровно столько же: кадров на входе почти столько же,
    # сколько на выходе, xfade их не размножает.
    fc = [f'[{k}:v]lut3d=file={grade_for(sh, st)}[g{k}]'
          for k, sh in enumerate(group)]

    prev, off = "g0", 0.0
    for k in range(1, len(group)):
        tr = group[k - 1]
        d = xfade_dur(tr)
        name = "fade" if tr["transition"] == "cut" else tr["transition"]
        # смещение — ровно граница предложения, а не длина файла: кадр k
        # встаёт на ту секунду звука, где начинается его первое предложение
        off += tr["duration"]
        fc.append(f'[{prev}][g{k}]xfade=transition={name}:'
                  f'duration={d:.3f}:offset={off:.3f}[x{k}]')
        prev = f"x{k}"

    # Плёночная база — общая на ролик, поэтому лежит уже на склеенном.
    fc.append(f'[{prev}]' + film_look() + '[graded]')
    # Слой один — искры, и его может не быть вовсе. Дымку с канала убрали:
    # атмосферность уже в LUT через подъём чёрного, второй слой её только мылил.
    if sparks is not None:
        flip = ",hflip" if st.spark_flip else ""
        # setpts здесь нет: скорость искр задана в пикселях в секунду и
        # запечена прямо в петлю. Растягивать её ещё и по времени значило бы
        # умножать одно на другое и терять контроль над числом.
        fc.append(f'[{sp}:v]scale={W}:{H}{flip},setsar=1[spv]')
        # all_opacity — единственная ручка силы наложения
        fc.append(f'[graded][spv]blend=all_mode=screen:'
                  f'all_opacity={st.spark_opacity}:shortest=1[h2]')
        last = "h2"
    else:
        last = "graded"

    post = []
    if st.grain:
        post.append(f"noise=alls={st.grain}:allf=t+u")
    if st.vignette:
        # Зажим В ТОЧКЕ ПРИМЕНЕНИЯ, а не только на источнике жребия.
        # st.vignette — делитель в PI/значение, то есть МЕНЬШЕ число здесь
        # значит СИЛЬНЕЕ виньетка. На ff-ep05 сюда пришло 1.82 (жребий из
        # editorial/variation.py тянул от 0.0), и PI/1.82 дало половину
        # кадра чёрным по краю — измерено прямым рендером тестового
        # паттерна: 53% пикселей темнее 15/255 против 1-3% на безопасных
        # значениях 3.6-5.6. Диапазон в variation.py уже поднят до 2.8+, но
        # эта строка — вторая линия защиты: чем бы ни задали st.vignette
        # (жребий, style_override из спецификации, правка кода), кадр не
        # может уйти в почти сплошной чёрный круг. Порог 3.2 — чуть ниже
        # безопасного пола в variation.py (3.4), запас на округление.
        safe = max(st.vignette, 3.2)
        post.append(f"vignette=PI/{safe:.2f}")
    # Открытие из чёрного. Стоит ПОСЛЕ цветокора и зерна, иначе чёрное
    # перестаёт быть чёрным: LUT поднимает нулевой уровень до 0.05, и
    # проявление идёт не из черноты, а из коричневой мути.
    if first and st.opening == "black_card":
        post.append("fade=t=in:st=0:d=1.4")
    # Плашки ставятся ПОСЛЕ виньетки. Иначе виньетка гасит нижние углы, а
    # плашка стоит именно там — текст уходил бы в тень ровно у той половины
    # раскладок, где он внизу.
    chain = textcard.filter_chain(text_for_group(group, moments or []))
    if chain:
        post.append(chain)
    post.append("setsar=1")
    fc.append(f'[{last}]' + ",".join(post) + '[out]')

    cmd = (f'ffmpeg -y {ins} -filter_complex "{";".join(fc)}" -map "[out]" '
           f'-c:v libx264 -crf {st.crf} -preset {st.preset} '
           f'-pix_fmt yuv420p -an "{out}"')
    subprocess.run(cmd, shell=True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def duck_points(st, story, pace):
    """
    Где подложка уходит под голос. Возвращает [(секунда, длительность)].

    Четыре манеры, ось duck_style. Разные не ради разного: место, где
    музыка расступается, зритель запоминает, и если у всех роликов канала
    она расступается в одинаковых местах — это такая же подпись, как
    одинаковое открытие.

      revelation  яма на каждой развязке. Самая «документальная» манера:
                  музыка уходит ровно там, где называют сумму
      beats       на каждой границе доли. Подложка дышит вместе с
                  структурой, ям много и они короткие
      sparse      три-пять ям на весь ролик, глубокие и длинные
      breath      ямы совпадают с выдохами из pacing.py — музыка молчит
                  там, где молчит монтаж
    """
    if not story:
        return []
    style = st.duck_style
    pts = []

    if style == "revelation":
        pts = [(b.start, min(b.duration, 14.0))
               for b in story if b.kind == "revelation"]
    elif style == "beats":
        pts = [(b.start, 5.0) for b in story[1:]]
    elif style == "breath":
        pts = [(story[i].start, 8.0) for i in sorted(pace.breaths)
               if i < len(story)]
    else:                       # sparse
        strong = [b for b in story
                  if b.kind in ("revelation", "escalation", "cta")]
        pool = strong or story[1:]
        n = min(len(pool), st.rng.choice([3, 4, 5]))
        pts = [(b.start, min(b.duration, 18.0))
               for b in st.rng.sample(pool, n)] if pool else []

    # Ямы ближе четырёх секунд друг к другу сливаются в одну долгую — это
    # уже не приём, а просто тихая музыка. Схлопываем.
    pts.sort()
    merged = []
    for t0, d in pts:
        if merged and t0 - merged[-1][0] < 4.0:
            merged[-1] = (merged[-1][0], max(merged[-1][1], t0 + d - merged[-1][0]))
        else:
            merged.append((t0, d))
    return merged


def ensure_overlays(st):
    """Готовый слой искр, либо None если искры выключены спецификацией."""
    if not st.sparks_enabled:
        log("  искры выключены спецификацией")
        return None
    OVERLAYS.mkdir(parents=True, exist_ok=True)
    sp = OVERLAYS / f"sparks_{st.sparks_variant}.mp4"
    if not sp.exists():
        # Штатно файл лежит в репозитории и этот код не выполняется.
        # Он остаётся страховкой: генерация трёх вариантов занимает минуты,
        # и платить их каждым прогоном не за что.
        log("  искр нет, генерирую (разово)")
        import overlays
        overlays.make_sparks(sp, seconds=20, seed=st.sparks_variant,
                             count=overlays.SPARK_COUNTS[st.sparks_variant - 1],
                             size_range=tuple(st.spark_size),
                             px_sec=tuple(st.spark_speed_px_sec),
                             flicker=tuple(st.spark_flicker))
    return sp


# ───────────────────────── ГЛАВНОЕ ─────────────────────────

def main(job_path):
    job = load_job(job_path)
    base = Path("work") / job["id"]
    assets = base / "assets"
    tmp = base / "tmp"
    out = base / "out"
    for d in (tmp, out):
        d.mkdir(parents=True, exist_ok=True)

    # Чего не брать — из журнала канала, а не из спецификации. Поля
    # recent_luts / recent_openings в спецификации остаются как ручное
    # переопределение, но пустыми они больше не значат «повторяй что хочешь»:
    # раньше их надо было заполнять руками, и они, разумеется, всегда были
    # пустыми — защита от повторов существовала только на бумаге.
    av = channel.avoid()
    if any(av.values()):
        log("── журнал канала: не повторяю " +
            ", ".join(f"{k}={v}" for k, v in av.items() if v))
    for problem in channel.check(job):
        log(f"  ! {problem}")

    st = style_mod.StyleEngine(
        job["id"],
        recent_luts=job.get("recent_luts") or av["lut"],
        recent_openings=job.get("recent_openings") or av["opening"],
        recent_transitions=av["main_transition"],
        recent_sparks=av["sparks"],
        recent_music=av["music"])
    # Цветокор можно задать и на верхнем уровне спецификации, и внутри
    # style_override. Раньше верхний уровень читался только для archive_lut,
    # а lut рядом с ним молча игнорировался — и ролик, в спецификации
    # которого написано "lut": "warm_amber", собирался на случайном.
    for key in ("lut", "archive_lut"):
        if job.get(key):
            setattr(st, key, job[key])
    # style_override накладывается СРАЗУ после создания движка: ниже от этих
    # полей зависят и план кадров, и запас PAD, и генерация искр.
    if job.get("style_override"):
        log("── настройки из спецификации")
        apply_style_override(st, job)
    # Проверяется ПОСЛЕ наложения override: цветокор можно задать и там.
    # Отсутствующий .cube ffmpeg сообщает где-то на середине группы склейки,
    # а с заглушенным stderr — вообще никак.
    check_luts(st)
    log("стиль:", json.dumps(st.summary(), ensure_ascii=False))
    d = st.divergence
    log(f"  разведение: {d.get('note')}"
        + (f", отличий от прошлого {len(d['differs_from_prev'])}"
           if d.get("differs_from_prev") else ""))

    marks = json.loads((assets / "marks.json").read_text())
    total = json.loads((assets / "state.json").read_text())["total_audio"]

    log("── план кадров")
    shots = plan_shots(marks, st, assets, total, job.get("reject"), job)

    # Паузы перед главами — ПОСЛЕ плана, а не внутри него: раскладка по
    # смыслу (beats/pacing/variation) уже принята, дальше только техника.
    boundaries = chapter_boundaries(job, getattr(st, "beats", []), total)
    if boundaries:
        shots, total = insert_chapter_cards(shots, boundaries, total)
        log(f"── паузы перед главами: {len(boundaries)} шт., "
            f"по {CHAPTER_PAUSE:.1f} с")
        for t, name in boundaries:
            log(f"  {t/60:5.1f} мин  «{name}»")

    set_render_durations(shots)
    log(f"  {len(shots)} кадров на {total/60:.1f} мин, "
        f"средний {total/len(shots):.1f} сек")

    # Замер, а не обещание. Доля считается по готовому плану и печатается
    # всегда: если материала не хватило и генерация полезла за свои 30%,
    # это должно быть видно в логе, а не обнаруживаться при просмотре.
    sec, cnt, mtotal = material_report(shots)
    names = {"gen": "генерация", "arch": "архивное фото", "clip": "сток видео"}
    for k in ("gen", "arch", "clip"):
        log(f"  {names[k]:<14} {sec[k]/mtotal*100:5.1f}%  "
            f"{sec[k]/60:6.1f} мин  {cnt[k]:4d} кадров")
    if cnt.get("card"):
        log(f"  {'паузы глав':<14} {sec['card']/mtotal*100:5.1f}%  "
            f"{sec['card']/60:6.1f} мин  {cnt['card']:4d} кадров")
    want = st.generated_share
    if abs(sec["gen"] / mtotal - want) > 0.05:
        log(f"  ! генерации {sec['gen']/mtotal*100:.1f}% при заказанных "
            f"{want*100:.0f}% — не хватило реального материала, "
            f"добери его этапом material")
    (out / "shots.json").write_text(json.dumps(
        [{k: str(v) for k, v in s.items() if k != 'framing'} for s in shots],
        indent=1, ensure_ascii=False))

    # ── ПРОВЕРКА ПЛАНА ───────────────────────────────────────────────
    # По факту, на разложенном плане, а не по намерению генератора. Между
    # намерением и результатом стоит материал: когда стока мало, план
    # схлопывается в череду фотографий с одинаковым наездом независимо от
    # того, какой богатый вектор стиля ему достался.
    log("── проверка плана на признаки шаблона")
    findings = rails.audit(shots, st.vector, getattr(st, "beats", None), job)
    metrics = rails.metrics(shots)
    hard = rails.report(findings, log)
    log(f"  разброс длительностей {metrics['cv_duration']}, "
        f"скоростей {metrics['speed_stdev']}, "
        f"переходов {metrics['distinct_transitions']}, "
        f"движений {metrics['distinct_moves']}")
    if hard:
        log("  ! есть замечания уровня «стоп» — ролик соберётся, но "
            "выкладывать его в таком виде не стоит")

    # ── ПЛАШКИ ───────────────────────────────────────────────────────
    # Собственная графика конвейера: единственный элемент кадра, которого
    # нет ни в одном исходном материале.
    moments = textcard.moments(getattr(st, "beats", []), marks, st.vector,
                               st.rng)
    # Разбор чисел идёт по ИСХОДНОМУ сценарию (см. заголовок раздела «паузы
    # перед главами» выше), а плашка ложится на РАЗДВИНУТЫЙ таймлайн — иначе
    # text_for_group() сверяет её время со сдвинутыми группами кадров и не
    # находит совпадения ни разу после первой же паузы, то есть все плашки
    # после первой главы молча пропадают.
    if boundaries:
        for m in moments:
            m["t"] = round(m["t"] + _shift_at(m["t"], boundaries, CHAPTER_PAUSE), 3)
    if moments:
        log(f"── плашки: {len(moments)} шт., стиль {st.text_style}")
        for m in moments:
            log(f"  {m['t']/60:5.1f} мин  {m['text']}")
    elif st.text_style != "none":
        log("── плашки: чисел в развязках не нашлось, ролик без них")

    # Карточка стиля кладётся рядом с роликом: из неё channel.py потом
    # запишет ролик в журнал. Пишется ЗДЕСЬ, а не до плана: в неё входят
    # вектор стиля (по нему следующий ролик будет разводиться), метрики
    # готового плана и список показанного материала. Всё это известно
    # только после раскладки.
    #
    # В журнал карточка попадает ПОСЛЕ выкладки отдельной командой, а не
    # отсюда: пересобранный десять раз ролик не должен десять раз
    # выталкивать из списка недавних настоящие.
    card = st.summary()
    card["style_vector"] = {k: (list(v) if isinstance(v, tuple) else v)
                            for k, v in st.vector.items()}
    card["plan_metrics"] = metrics
    card["assets_used"] = getattr(st, "assets_used", {})
    card["beats"] = [b.summary() for b in getattr(st, "beats", [])]
    (out / "style.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")

    log("── рендер кадров")
    with ThreadPoolExecutor(max_workers=cores()) as ex:
        files = list(ex.map(render_one,
                            [(n, s, tmp) for n, s in enumerate(shots)]))
    for s, f in zip(shots, files):
        s["file"] = f

    log("── склейка и цветокор")
    sparks = ensure_overlays(st)
    segs = []
    for gi in range(0, len(shots), SEG_SIZE):
        group = shots[gi:gi + SEG_SIZE]
        seg = tmp / f"seg_{gi//SEG_SIZE:03d}.mp4"
        if not seg.exists():
            join(group, seg, st, sparks, first=(gi == 0), moments=moments)
        segs.append(seg)
        log(f"  группа {gi//SEG_SIZE + 1}/{math.ceil(len(shots)/SEG_SIZE)}")

    log("── сшивка")
    silent = tmp / "silent.mp4"
    render.concat_segments(segs, silent)

    log("── звук")
    mixed = tmp / "audio.m4a"
    # Порядок: поле music в спецификации -> выбор движка стиля -> ничего.
    if job.get("music"):
        bed = Path(job["music"])
        chosen_by = "спецификация"
    elif getattr(st, "music", None):
        bed = MUSIC_DIR / st.music
        chosen_by = "жребий, разведён с последними роликами"
    else:
        bed = None
        chosen_by = None
    if bed is not None and not bed.is_absolute():
        bed = ROOT / bed
    # Подложки может не быть: она делается отдельно и появляется в репозитории
    # позже кода. Раньше это роняло всю сборку на последнем шаге — после
    # сорока минут рендера, из-за отсутствующего mp3. Теперь ролик собирается
    # с одним голосом, а в лог уходит внятное предупреждение.
    #
    # Отдельно про ПРОПАВШИЙ ФАЙЛ ИЗ СПЕЦИФИКАЦИИ: подложки канала переименовали
    # (bed.mp3 -> bed1..bed5), и старые спецификации указывают на файл, которого
    # больше нет. Молча собрать такой ролик без музыки — худший исход: заметно
    # это только на готовом файле. Поэтому если файл ЗАДАН ЯВНО и не найден,
    # берём жребий движка и говорим об этом громко.
    if bed is not None and not bed.exists():
        fallback = MUSIC_DIR / st.music if getattr(st, "music", None) else None
        if chosen_by == "спецификация" and fallback and fallback.exists():
            log(f"  ! подложки {bed.name} нет (её переименовали?) — "
                f"беру {fallback.name} жребием")
            bed, chosen_by = fallback, "жребий вместо пропавшего файла"
        else:
            log(f"  ! подложки нет ({bed}) — собираю только с голосом")
            bed = None
    if bed is not None:
        log(f"  подложка: {bed.name} ({chosen_by})")
    elif not style_mod.music_pool():
        log("  ! подложек нет в assets/music — собираю только с голосом")
    ducks = duck_points(st, getattr(st, "beats", []), getattr(st, "pacing", None))
    if boundaries:
        # Те же ямы подложки, но на раздвинутой шкале — см. «плашки» выше:
        # без сдвига яма после первой паузы приходится не под ту фразу.
        ducks = [(round(t + _shift_at(t, boundaries, CHAPTER_PAUSE), 3), d)
                 for t, d in ducks]
    if ducks and bed:
        log(f"  подложка уходит в {len(ducks)} местах "
            f"({st.duck_style}, глубина {st.duck_depth})")
    voice = assets / "voice_full.m4a"
    if boundaries:
        log(f"── врезаю тишину в начитку: {len(boundaries)} пауз по "
            f"{CHAPTER_PAUSE:.1f} с")
        voice = voice_with_pauses(voice, boundaries, CHAPTER_PAUSE, tmp)
    render.build_audio(voice, bed, mixed, total,
                       bed_gain_db=job.get("bed_gain_db", -26.0),
                       duck_points=ducks, duck_depth=st.duck_depth)

    log("── финал")
    final = out / "final.mp4"
    render.mux(silent, mixed, final)
    # marks_final.json — тайм-коды на шкале ГОТОВОГО final.mp4 (с паузами,
    # если они есть). subs.srt пишется из них же, а не из сырых marks:
    # иначе субтитры после первой паузы обгоняли бы звук на её длину.
    # shorts.py режет куски из final.mp4 и читает этот файл, если он есть,
    # вместо assets/marks.json — тот остаётся кэшем на исходной шкале.
    final_marks = shift_marks(marks, boundaries, CHAPTER_PAUSE) if boundaries else marks
    render.write_srt(final_marks, out / "subs.srt")
    (out / "marks_final.json").write_text(
        json.dumps(final_marks, ensure_ascii=False), encoding="utf-8")

    # ПРОВЕРКА ЗАМЕРОМ, а не на глаз. Расхождение видео и звука — симптом
    # перекрытия переходов: код возврата ноль, лог чистый, а конец начитки
    # молча срезан -shortest. Видно это только здесь.
    vd = render.duration_of(final, "v")
    ad = render.duration_of(final, "a")
    size_gb = final.stat().st_size / 2**30
    log(f"  видео {vd:.3f} с, звук {ad:.3f} с, тайм-коды {total:.3f} с")
    log(f"  файл  {size_gb:.2f} ГБ  ({final.stat().st_size * 8 / vd / 1e6:.1f} Мбит/с)")
    if abs(vd - ad) > 0.5:
        log(f"  ! видео и звук разошлись на {abs(vd - ad):.2f} с — "
            f"это перекрытие переходов, смотри set_render_durations")
    if size_gb > 1.9:
        log(f"  ! {size_gb:.2f} ГБ при лимите GitHub Releases в 2 ГБ — "
            f'подними crf в style_override (сейчас {st.crf})')

    # ── МОНТАЖНЫЙ ЛИСТ ───────────────────────────────────────────────
    # Строится из того же плана, что ушёл в рендер, поэтому разойтись с
    # роликом не может технически. Стоит ноль: все решения уже приняты, тут
    # они только записываются.
    log("── монтажный лист")
    paths = edl.export_all(
        out, shots=shots, beats=getattr(st, "beats", []),
        style_card=card, vector=st.vector, metrics=metrics,
        findings=findings, divergence=st.divergence,
        text_moments=moments,
        pacing_report=getattr(st, "pacing", None).report()
        if getattr(st, "pacing", None) else [],
        pacing_decisions=getattr(st, "pacing", None).decisions
        if getattr(st, "pacing", None) else {},
        fps=render.FPS, title=(job.get("youtube") or {}).get("title", job["id"]))
    for name, p in paths.items():
        log(f"  {name}: {p}")

    log("готово:", final)


if __name__ == "__main__":
    main(sys.argv[1])
