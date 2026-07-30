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
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import channel
import render
import vet
import style as style_mod
from render import W, H, FPS

ROOT = Path(__file__).parent.parent
LUTS = ROOT / "assets" / "luts"
OVERLAYS = ROOT / "assets" / "overlays"
# Подложка по умолчанию. Своя для ролика задаётся полем music в
# спецификации — у каждого канала она обычно разная.
MUSIC = ROOT / "assets" / "music" / "bed.mp3"

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

    def __init__(self, pool, total: float):
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

    def take(self, t: float, text: str = ""):
        n = len(self.pool)
        if not n:
            raise SystemExit("пустой пул материала")
        want = words_of(text)
        k = min(n - 1, max(0, int(t / self.total * n)))
        self.calls += 1

        def score(j):
            path, _tag, kw = self.pool[j]
            overlap = len(want & kw)
            same = 1 if path == self.last else 0
            # порядок важен: сначала не повторяться, потом смысл,
            # потом реже показанное, потом ближе по таймлайну
            return (same, -overlap, self.used.get(j, 0), abs(j - k), j)

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
        return (f"{self.hits} из {self.calls} кадров подобраны по смыслу "
                f"({self.hits/self.calls*100:.0f}%)")

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


def plan_shots(marks, st, assets, total, job_reject=None, job=None):
    """
    Раскладывает материал по таймлайну.

    Ключевое: кадр всегда заканчивается на границе предложения.
    Робот копит предложения, пока не наберётся нужная длительность,
    и режет там. Поэтому склейки попадают в паузы речи.
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
        raise SystemExit("нет сгенерированных изображений")

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
        try:
            return kw.get((p.name.split("_")[0], int(p.name.split("_")[1])), set())
        except (IndexError, ValueError):
            return set()

    gen_pick = ShotPicker([(p, "gen", kw_of(p)) for p in images], total)
    arch_pick = ShotPicker([(p, "arch", kw_of(p)) for p in archive], total)
    clip_pick = ShotPicker([(p, "clip", kw_of(p)) for p in clips], total)

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
    run_kind, run_len = None, 0
    while t < intro_end:
        # чередуем видео и фото, но не даём трём одинаковым идти подряд
        want_clip = st.rng.random() < st.intro_clip_share and clip_available()
        if run_len >= 2:
            want_clip = run_kind != "clip" and clip_available()
        kind = "clip" if want_clip else "image"
        run_len = run_len + 1 if kind == run_kind else 1
        run_kind = kind

        rng_pair = (st.intro_clip_duration_range if kind == "clip"
                    else st.intro_photo_duration_range)
        dur = round(st.rng.uniform(*rng_pair), 3)

        # ТИП ОТКРЫТИЯ. Первые секунды решают, останется зритель или нет, и
        # именно они у всех роликов канала были одинаковыми: поле opening
        # вычислялось, но нигде не применялось. Три варианта расходятся
        # только длительностями первых кадров и проявлением из чёрного —
        # таймлайн при этом не сдвигается ни на кадр, поэтому звук остаётся
        # на месте (сдвиг здесь стоил бы рассинхрона на весь ролик).
        if idx == 0 and st.opening == "long_footage":
            # один установочный кадр, потом перебивка — «выдох» перед бегом
            dur = round(st.rng.uniform(5.5, 8.5), 3)
        elif st.opening == "quick_cuts" and t < intro_start + 14.0:
            # первые четырнадцать секунд — самый короткий возможный шаг
            lo = rng_pair[0]
            dur = round(min(dur, lo + (rng_pair[1] - lo) * 0.3), 3)
        tr = st.rng.choice(st.transitions)
        # переход во вступлении короткий: длинное растворение съедает
        # весь смысл быстрой перебивки
        trd = round(st.rng.uniform(*st.intro_transition_duration_range), 2)

        # Слот выбран (видео или фото), но чем именно его закрыть — решает
        # пропорция. Во вступлении она работает так же, как в теле: иначе
        # первые три минуты, самые смотримые, съезжали бы в генерацию.
        got = mix.pick(["clip"] if kind == "clip" else ["gen", "arch"])

        if got == "clip":
            src, _ = clip_pick.take(t, said_at(t, dur))
            shots.append(dict(kind="clip", file=src, tag="clip",
                              src_start=cutter.take_start(src, dur),
                              move=repeat_move(clip_pick.last_repeat),
                              start=round(t, 3), duration=dur,
                              transition=tr, transition_dur=trd,
                              effect=st.effect()))
        else:
            shots.append(put_image(
                got, t, said=said_at(t, dur), start=round(t, 3), duration=dur,
                # во вступлении по фотографии всегда идёт скольжение
                # или наезд, статики тут быть не должно
                move=st.rng.choice(INTRO_MOVES),
                speed=round(st.rng.uniform(0.9, 1.35), 3),
                transition=tr, transition_dur=trd,
                effect=st.effect()))
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
    # Оценка числа кадров в теле. Она нужна только затем, чтобы st.clip знала,
    # насколько ролик близок к финалу, и удлиняла кадры к концу. Раньше сюда
    # шло len(marks)//3 — число предложений, делённое на три, — и оно
    # занижало счёт примерно вдвое: при базовом кадре в пять секунд на кадр
    # приходится меньше двух предложений, а не три. Из-за занижения прогресс
    # у последних кадров уходил далеко за единицу и замедление разгонялось
    # втрое против заказанного.
    body_seconds = max(total - intro_end, 1.0)
    est_body_shots = max(8, int(body_seconds / max(st.base_dur, 1.0)))
    body_idx = 0

    while i < len(marks):
        is_anchor = idx in anchors
        cfg = st.clip(body_idx, est_body_shots, is_anchor=is_anchor)
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
        clip_ok = (not is_anchor and since_clip >= next_gap
                   and dur <= CLIP_MAX_SECONDS and clip_available())
        got = mix.pick((["clip"] if clip_ok else []) + ["gen", "arch"])

        said = " ".join(m["text"] for m in marks[first:best + 1])

        if got == "clip":
            src, _ = clip_pick.take(start, said)
            shots.append(dict(kind="clip", file=src, tag="clip",
                              src_start=cutter.take_start(src, dur),
                              move=repeat_move(clip_pick.last_repeat),
                              start=start, duration=dur,
                              **{k: cfg[k] for k in
                                 ("transition", "transition_dur", "effect")}))
            since_clip = 0
            n = st.body_clip_every_n_shots
            next_gap = max(1, n + st.rng.choice([-1, 0, 0, 1]))
        else:
            since_clip += 1
            shots.append(put_image(
                got, start, said=said, start=start, duration=dur,
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
    if sh["kind"] == "clip":
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


def join(group, out: Path, st, sparks, first=False):
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
        post.append(f"vignette=PI/{st.vignette:.2f}")
    # Открытие из чёрного. Стоит ПОСЛЕ цветокора и зерна, иначе чёрное
    # перестаёт быть чёрным: LUT поднимает нулевой уровень до 0.05, и
    # проявление идёт не из черноты, а из коричневой мути.
    if first and st.opening == "black_card":
        post.append("fade=t=in:st=0:d=1.4")
    post.append("setsar=1")
    fc.append(f'[{last}]' + ",".join(post) + '[out]')

    cmd = (f'ffmpeg -y {ins} -filter_complex "{";".join(fc)}" -map "[out]" '
           f'-c:v libx264 -crf {st.crf} -preset {st.preset} '
           f'-pix_fmt yuv420p -an "{out}"')
    subprocess.run(cmd, shell=True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
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
        recent_sparks=av["sparks"])
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
    # Карточка стиля кладётся рядом с роликом: из неё channel.py потом
    # запишет ролик в журнал. Записывается ПОСЛЕ выкладки отдельной командой,
    # а не здесь: пересобранный десять раз ролик не должен десять раз
    # выталкивать из списка недавних настоящие.
    (out / "style.json").write_text(
        json.dumps(st.summary(), ensure_ascii=False, indent=1),
        encoding="utf-8")

    marks = json.loads((assets / "marks.json").read_text())
    total = json.loads((assets / "state.json").read_text())["total_audio"]

    log("── план кадров")
    shots = plan_shots(marks, st, assets, total, job.get("reject"), job)
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
    want = st.generated_share
    if abs(sec["gen"] / mtotal - want) > 0.05:
        log(f"  ! генерации {sec['gen']/mtotal*100:.1f}% при заказанных "
            f"{want*100:.0f}% — не хватило реального материала, "
            f"добери его этапом material")
    (out / "shots.json").write_text(json.dumps(
        [{k: str(v) for k, v in s.items() if k != 'framing'} for s in shots],
        indent=1, ensure_ascii=False))

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
            join(group, seg, st, sparks, first=(gi == 0))
        segs.append(seg)
        log(f"  группа {gi//SEG_SIZE + 1}/{math.ceil(len(shots)/SEG_SIZE)}")

    log("── сшивка")
    silent = tmp / "silent.mp4"
    render.concat_segments(segs, silent)

    log("── звук")
    mixed = tmp / "audio.m4a"
    bed = Path(job["music"]) if job.get("music") else MUSIC
    if not bed.is_absolute():
        bed = ROOT / bed
    # Подложки может не быть: она делается отдельно и появляется в репозитории
    # позже кода. Раньше это роняло всю сборку на последнем шаге — после
    # сорока минут рендера, из-за отсутствующего mp3. Теперь ролик собирается
    # с одним голосом, а в лог уходит внятное предупреждение.
    if bed.exists():
        log(f"  подложка: {bed.name}")
    else:
        log(f"  ! подложки нет ({bed}) — собираю только с голосом")
        bed = None
    render.build_audio(assets / "voice_full.m4a", bed, mixed, total,
                       bed_gain_db=job.get("bed_gain_db", -26.0))

    log("── финал")
    final = out / "final.mp4"
    render.mux(silent, mixed, final)
    render.write_srt(marks, out / "subs.srt")

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
    log("готово:", final)


if __name__ == "__main__":
    main(sys.argv[1])
