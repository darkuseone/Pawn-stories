"""
style.py — движок антишаблонности.

Что изменилось против прежней версии
------------------------------------
Раньше стиль был россыпью независимых полей, каждое со своим жребием.
Работало, но у такого устройства два изъяна, и оба видны только на канале
целиком, а не на одном ролике.

Первый: нельзя было ответить на вопрос «насколько этот ролик отличается от
предыдущего». Защита от повторов сводилась к «не бери тот же цветокор», и
две загрузки, различающиеся одним цветом, спокойно её проходили.

Второй: ритм считался одной формулой на весь ролик — база плюс замедление
к финалу. Числа в формуле менялись, ФОРМА КРИВОЙ не менялась никогда. У
всех загрузок канала кадры равномерно удлинялись от начала к концу. Это
подпись, и её видно на графике длительностей даже когда всё остальное
разное.

Теперь стиль — это ВЕКТОР из двадцати четырёх осей (editorial/variation.py).
Вектор бросается целиком, сравнивается с последними роликами канала и
пересдаётся, пока не разойдётся с ними минимум по четырём значимым осям
(editorial/memory.py). Ритм берётся от структуры сценария
(editorial/beats.py) и от выпавшей формы кривой (editorial/pacing.py), а не
от одной зашитой формулы.

Что осталось прежним
--------------------
Воспроизводимость: seed из id ролика. Тот же id даёт тот же ролик — это
нужно для отладки и, главное, для пересборки монтажа из кэша: иначе каждая
пересборка давала бы другой план и обесценивала готовые кадры.

Интерфейс для build.py: те же поля и те же методы. Спецификации роликов,
написанные под старую версию, работают без правок.
"""

import random

from editorial import variation, memory

# Пул цветокоров по умолчанию: из него движок берёт случайный.
# Конкретный цвет ролика задаётся полем lut в спецификации — тогда жребий
# не бросается вовсе.
#
# Семья ТЁПЛАЯ. Холодная синяя (steel_blue, deep_navy, slate_ash, cold_teal,
# blue_amber) делалась под сонный исторический канал и здесь убрана целиком:
# лавка древностей, дерево, латунь и ламповый свет — это янтарь и медь.
# Таблицы считает pipeline/make_luts.py, там же описано, из чего состоит
# каждый оттенок.
LUTS = ["warm_amber", "oak_brass", "lamp_glow", "dust_gold", "copper_dusk"]

# Архивная семья — под подлинное фото и хронику. Отличается от семьи канала
# не оттенком (он тоже тёплый), а плотностью: насыщенность ниже, контраст
# выше, картинка читается как отпечаток, а не как кадр.
ARCHIVE_LUTS = ["archive_sepia", "archive_silver", "archive_paper"]

# Движения камеры. w0/w1 — ширина виртуального холста в начале и конце
# (растёт = наезд, падает = отъезд). x/y — положение окна кадра 0..1.
#
# Амплитуды подняты: раньше холст гулял в пределах 2000-2300 при кадре 1920,
# то есть запас на панораму составлял иногда 80 пикселей — движение читалось
# как «почти стоп-кадр». Теперь холст 3000 (см. PREP_W в render.py), а рабочий
# диапазон 2180-2720, и на панораму есть от 260 до 800 пикселей.
# Нижняя граница не опускается ниже 1920: там кадр перестал бы помещаться.
MOVES = {
    "push_in":      dict(w0=2180, w1=2680, x0=0.50, x1=0.50, y0=0.50, y1=0.50),
    "pull_out":     dict(w0=2700, w1=2200, x0=0.50, x1=0.50, y0=0.50, y1=0.50),
    "pan_right":    dict(w0=2520, w1=2560, x0=0.06, x1=0.94, y0=0.50, y1=0.50),
    "pan_left":     dict(w0=2520, w1=2560, x0=0.94, x1=0.06, y0=0.50, y1=0.50),
    "tilt_down":    dict(w0=2560, w1=2600, x0=0.50, x1=0.50, y0=0.06, y1=0.94),
    "tilt_up":      dict(w0=2560, w1=2600, x0=0.50, x1=0.50, y0=0.94, y1=0.06),
    "push_left":    dict(w0=2200, w1=2700, x0=0.72, x1=0.28, y0=0.42, y1=0.58),
    "push_right":   dict(w0=2200, w1=2700, x0=0.28, x1=0.72, y0=0.58, y1=0.42),
    # наезд с проходом слева направо и обратно — самое «живое» движение
    "sweep_in":     dict(w0=2240, w1=2700, x0=0.10, x1=0.62, y0=0.52, y1=0.46),
    "sweep_out":    dict(w0=2700, w1=2230, x0=0.66, x1=0.16, y0=0.44, y1=0.56),
    "drift_out":    dict(w0=2700, w1=2210, x0=0.36, x1=0.64, y0=0.64, y1=0.36),
    # спокойный кадр — но не мёртвый: лёгкий дрейф остаётся всегда
    "hold_drift":   dict(w0=2300, w1=2440, x0=0.44, x1=0.56, y0=0.53, y1=0.47),
}

# Семьи движений. Нужны для двух вещей: чтобы ось motion_bias могла
# наклонить ролик в сторону наездов или панорам, и чтобы rails.py ловил
# три однотипных движения подряд — push_in и push_left это оба наезда, и
# зритель видит их как одно, как их ни называй.
MOVE_FAMILY = {
    "push": ["push_in", "push_left", "push_right"],
    "pull": ["pull_out", "drift_out"],
    "pan": ["pan_right", "pan_left"],
    "tilt": ["tilt_down", "tilt_up"],
    "sweep": ["sweep_in", "sweep_out"],
    "hold": ["hold_drift"],
}

# Наклон набора движений под ось motion_bias. Веса, а не жёсткие списки:
# ролик «с наездами» должен изредка давать панораму, иначе это не наклон,
# а новый шаблон.
MOTION_BIAS = {
    "push":          dict(push=4.0, pull=1.4, pan=1.0, tilt=0.6, sweep=1.6, hold=0.5),
    "pan":           dict(push=1.0, pull=0.8, pan=4.0, tilt=1.6, sweep=1.2, hold=0.6),
    "sweep":         dict(push=1.4, pull=1.2, pan=1.4, tilt=0.6, sweep=4.0, hold=0.5),
    "mixed":         dict(push=1.6, pull=1.3, pan=1.6, tilt=1.0, sweep=1.4, hold=0.8),
    "still_leaning": dict(push=1.0, pull=1.0, pan=1.2, tilt=0.8, sweep=0.7, hold=2.6),
}

# Переходы. Все мягкие и тянутся во времени — резкой склейки в списке нет
# вовсе. Справа отмечено, какой пункт CapCut этим закрывается.
#
# fadeblack (CapCut «Black Fade») в списке НЕТ сознательно: он был убран по
# отдельной просьбе — уход в чёрное посреди ролика читается как обрыв записи.
# Вернуть — дописать строкой в style_override, код для этого уже есть.
TRANSITIONS = [
    "smoothleft",    # Wispy Wipe
    "smoothright",   # Wispy Wipe
    "smoothup",
    "smoothdown",
    "dissolve",      # Mix
    "hblur",         # Blur
    "circleopen",
    "fadegrays",
    "fade",          # Cross Fade
    "fadeslow",      # Slow Fade
    "slideleft",     # Passerby
    "slideright",    # Passerby
    "wipeleft",      # Shadow Sweep
]

# Эффекты на отдельный кадр. Названия — из CapCut, цепочки собраны на
# фильтрах ffmpeg и являются ПРИБЛИЖЕНИЕМ, а не портированием: чужие
# пресеты закрыты, повторяется характер, а не точная кривая.
#
# Держатся намеренно слабыми. Поверх кадра дальше ещё лягут LUT, плёночная
# база и зерно; эффект в полную силу тут перебьёт весь цветокор ролика.
EFFECTS = {
    # выцветшая плёнка: поднятый чёрный, ушедшая насыщенность, крупная грязь
    "vintage_blemish": ("curves=r='0/0.06 0.5/0.52 1/0.95'"
                        ":g='0/0.05 0.5/0.50 1/0.92'"
                        ":b='0/0.04 0.5/0.47 1/0.88',"
                        "eq=saturation=0.72,noise=alls=14:allf=t"),
    # кассета: расслоение красного и синего плюс тяжёлые тени
    "vhs_dark": ("rgbashift=rh=-3:bh=3,eq=contrast=1.08:brightness=-0.04,"
                 "noise=alls=10:allf=t"),
    # почти монохром, уголь по бумаге
    "charcoal_film": "eq=saturation=0.22:contrast=1.10:gamma=0.94",
    # закатный свет: тёплые тени и середина, холод убран со светов
    "sunset_2": ("colorbalance=rs=0.06:rm=0.08:bh=-0.06,"
                 "eq=saturation=1.08:gamma_r=1.04"),
    # раскалённый отпечаток: красный вперёд, синий назад
    "heat_print": ("colorchannelmixer=rr=1.10:gg=0.98:bb=0.86,"
                   "eq=contrast=1.06:saturation=1.12"),
    # плотный техниколор: каналы разводятся друг от друга.
    # Синий приспущен (было bb=1.12): на тёплой семье цветокоров подъём
    # синего тянул кадр в холод и спорил с LUT ролика — единственный эффект
    # в наборе, который это делал.
    "technicolor_flash": ("colorchannelmixer=rr=1.12:rg=-0.06:gg=1.08"
                          ":gb=-0.06:bb=0.94:br=-0.04,"
                          "eq=saturation=1.16:contrast=1.05"),
    # латунный блик: света уходят в жёлто-золотое, тени остаются на месте.
    # Под витрины, монеты, оклады — то, ради чего канал и смотрят.
    "brass_gleam": ("curves=r='0/0.02 0.5/0.54 1/1.00'"
                    ":g='0/0.02 0.5/0.51 1/0.97'"
                    ":b='0/0.03 0.5/0.46 1/0.86',"
                    "eq=saturation=1.10:contrast=1.04"),
    # призматическая кайма по краям предметов
    "prism_1": "chromashift=cbh=-4:crh=4",
    # дыхание огня: тёплый сдвиг плюс медленная пульсация яркости.
    # eval=frame обязателен, иначе выражение посчитается один раз и
    # пульсации не будет вовсе.
    "by_the_fireplace": ("colorbalance=rs=0.05:rm=0.07:bm=-0.04,"
                         "eq=brightness='0.030*sin(2*PI*t*1.6)':eval=frame"),
}

# Кадрирование при повторном использовании одного изображения.
# Одна картинка показывается 2-3 раза, но каждый раз это другой кадр.
FRAMINGS = {
    "wide":         dict(scale=1.00, cx=0.50, cy=0.50),
    "tight_left":   dict(scale=1.35, cx=0.30, cy=0.45),
    "tight_right":  dict(scale=1.35, cx=0.70, cy=0.45),
    "detail_low":   dict(scale=1.60, cx=0.50, cy=0.70),
    "detail_high":  dict(scale=1.55, cx=0.45, cy=0.28),
    "medium":       dict(scale=1.18, cx=0.55, cy=0.50),
}

# Наклон кадрирования. Ролик «крупными планами» и ролик «общими» — это
# разный монтаж при одном и том же материале, и ось того стоит.
FRAMING_BIAS = {
    "wide_leaning":  {"wide": 3.0, "medium": 2.2, "tight_left": 1.0,
                      "tight_right": 1.0, "detail_low": 0.6, "detail_high": 0.6},
    "tight_leaning": {"wide": 0.8, "medium": 1.2, "tight_left": 2.4,
                      "tight_right": 2.4, "detail_low": 2.0, "detail_high": 2.0},
    "balanced":      {"wide": 1.6, "medium": 1.6, "tight_left": 1.4,
                      "tight_right": 1.4, "detail_low": 1.2, "detail_high": 1.2},
}

# Типы открытия. Первые пять секунд решают, останется зритель или нет, и
# именно они у всех роликов канала были одинаковыми дольше всего: поле
# opening когда-то вычислялось, но нигде не применялось.
#
#   cold_open       без разгона, сразу середина действия, самые короткие куски
#   long_establish  один длинный установочный кадр, потом перебивка
#   quick_cuts      предельно частая нарезка первые секунды
#   black_card      проявление из чёрного
#   slow_reveal     первый кадр медленный и длинный, дальше разгон
#   hard_in         открывает стоковое видео на полном темпе, без вступления
#
# long_footage оставлен синонимом long_establish: под этим именем открытия
# записаны в журнале канала, и переименование обнулило бы память.
OPENINGS = ("cold_open", "long_establish", "quick_cuts", "black_card",
            "slow_reveal", "hard_in")
OPENING_ALIASES = {"long_footage": "long_establish"}

# Предел множителя амплитуды камеры. Выше — рабочий холст 3000 пикселей
# перестаёт вмещать наезд, и картинка идёт на растяжение: push_in с
# амплитудой 500 при множителе 1.45 доходит до 2905 из 3000, это потолок.
SPEED_CLAMP = (0.55, 1.45)


def seed_from(video_id: str) -> int:
    return variation.seed_from(video_id)


class StyleEngine:
    """
    Визуальный стиль ролика и параметры каждого кадра.

    Создаётся один раз на сборку. Всё, что он решил, лежит в .vector и
    уходит в монтажный лист: доказательство осмысленности решений строится
    из тех же чисел, по которым собран ролик.
    """

    def __init__(self, video_id: str, recent_luts=None, recent_openings=None,
                 recent_transitions=None, recent_sparks=None,
                 diverge=True, log=print):
        """
        recent_* — что использовалось в последних роликах канала. Списки
        приходят из channel.py и работают как жёсткий запрет: без них
        случайность регулярно выдаёт три похожих ролика подряд, а YouTube
        показывает соседние загрузки рядом.

        diverge=False выключает пересдачу вектора. Нужно ровно в одном
        месте — в тестах, где журнал канала трогать нельзя.
        """
        self.video_id = video_id
        self.log = log

        # ── ВЕКТОР СТИЛЯ ──────────────────────────────────────────────
        # Бросается целиком и разводится с историей канала: минимум четыре
        # значимо разошедшихся оси с каждым из последних роликов, иначе
        # пересдача. Подробности — editorial/memory.py.
        if diverge:
            self.vector, self.divergence = memory.draw_diverged(video_id,
                                                                log=log)
        else:
            self.vector = variation.draw(video_id, 0)
            self.divergence = dict(tries=1, history=0,
                                   note="разведение выключено")
        v = self.vector

        self.rng = random.Random(seed_from(video_id) + 17)
        r = self.rng

        recent_luts = list(recent_luts or [])[-3:]
        recent_openings = [OPENING_ALIASES.get(o, o)
                           for o in list(recent_openings or [])[-2:]]
        recent_transitions = list(recent_transitions or [])[-2:]
        recent_sparks = list(recent_sparks or [])[-2:]

        lut_pool = [l for l in LUTS if l not in recent_luts] or LUTS
        self.lut = r.choice(lut_pool)
        # Архивный цветокор задаётся спецификацией; здесь только умолчание,
        # чтобы движок был работоспособен сам по себе.
        self.archive_lut = ARCHIVE_LUTS[0]

        # ДОЛЯ ГЕНЕРАЦИИ. Сколько экранного ВРЕМЕНИ (не кадров) отдаётся
        # сгенерированным изображениям; остальное — стоковое видео и
        # подлинные фото из архивов.
        #
        # Это не вкусовщина. Сюжет про находку — рассказ про КОНКРЕТНЫЙ
        # предмет: вот эта монета, вот этот сервиз. Генератор такую монету
        # не найдёт, он её нарисует, и рисунок выдаёт себя за фотографию
        # реального предмета. Поэтому под предмет идёт только настоящее, а
        # генерация закрывает общие планы, руки крупно, интерьер, атмосферу.
        self.generated_share = 0.30

        # Сжатие финального видео. Упирается в жёсткий лимит: файл в GitHub
        # Releases не может быть больше 2 ГБ, а получасовой ролик в 1080p/30
        # подходит к нему вплотную. Зерно плёнки — шум, и оно бьёт по сжатию
        # сильнее всего остального.
        self.crf = 20
        self.preset = "veryfast"

        # ── фактура: из вектора, а не отдельным жребием ───────────────
        self.grain = int(round(v["grain"]))
        self.vignette = round(v["vignette"], 2)
        self.haze_enabled = False

        # Искры — подпись канала, но не в каждом ролике. Ноль на оси sparks
        # означает «в этом ролике искр нет вовсе»: приём, стоящий на всех
        # загрузках подряд, перестаёт быть подписью и становится шаблоном.
        spark_pool = [s for s in (1, 2, 3) if s not in recent_sparks] or [1, 2, 3]
        self.sparks_enabled = v["sparks"] != 0
        self.sparks_variant = (v["sparks"] if v["sparks"] in spark_pool
                               else r.choice(spark_pool))
        self.spark_opacity = round(v["spark_opacity"], 3)

        # Скорость задаётся В ПИКСЕЛЯХ В СЕКУНДУ и печётся прямо в петлю.
        # Так её видно и можно проверить линейкой, а не через множитель
        # setpts, у которого физического смысла нет.
        self.spark_speed_px_sec = (80.0, 120.0)
        self.spark_flicker = (4.0, 8.0)
        self.spark_size = (1.0, 3.0)
        self.spark_flip = r.random() < 0.5

        # ── ритм: из вектора ──────────────────────────────────────────
        self.arc = v["arc"]
        self.base_dur = round(v["base_duration"], 3)
        self.shot_spread = round(v["shot_spread"], 3)
        self.breath_rate = round(v["breath_rate"], 3)
        self.motion_amp = round(v["motion_amp"], 3)
        self.motion_bias = v["motion_bias"]
        self.framing_bias = v["framing_bias"]
        # jitter и decel остаются как поля: на них ссылается старая
        # спецификация через REDRAW в build.py и старый путь st.clip()
        self.jitter = self.shot_spread
        self.decel = 1.0 + (self.base_dur - 4.2) / 8.0

        # ── склейка ───────────────────────────────────────────────────
        self.transitions = list(TRANSITIONS)
        tr_pool = [t for t in self.transitions
                   if t not in recent_transitions] or self.transitions
        self.main_tr = (v["main_transition"] if v["main_transition"] in tr_pool
                        else r.choice(tr_pool))
        # Насколько основной переход доминирует. Раньше было зашито 0.72 у
        # ВСЕХ роликов — то есть почерк склейки повторялся с точностью до
        # процента, даже когда сам переход менялся.
        self.transition_focus = round(v["transition_focus"], 3)
        self.tr_dur_range = tuple(v["transition_dur"])
        self.tr_dur = round(r.uniform(*self.tr_dur_range), 2)
        self.hard_cut_probability = 0.0

        # ── вступление ────────────────────────────────────────────────
        self.opening = OPENING_ALIASES.get(v["opening"], v["opening"])
        if self.opening in recent_openings:
            pool = [o for o in OPENINGS if o not in recent_openings] or list(OPENINGS)
            self.opening = r.choice(pool)
        self.intro_footage_seconds = round(v["opening_seconds"], 1)
        self.intro_clip_duration_range = (2.0, 4.0)
        self.intro_photo_duration_range = (3.0, 7.0)
        self.intro_clip_share = 0.6
        self.intro_transition_duration_range = (0.35, 0.7)
        self.body_clip_every_n_shots = max(2, int(round(v["clip_rhythm"])))

        # ── эффекты ───────────────────────────────────────────────────
        self.effects_enabled = True
        self.effects = list(EFFECTS)
        self.effect_probability = round(v["effect_p"], 3)

        # ── типографика и звук ────────────────────────────────────────
        self.text_style = v["text_style"]
        self.text_density = round(v["text_density"], 3)
        self.duck_depth = round(v["duck_depth"], 3)
        self.duck_style = v["duck_style"]

        # Раскладка превью. YouTube показывает превью соседних загрузок в
        # одном ряду, поэтому одинаковая вёрстка подписи опознаётся как
        # серия быстрее, чем любой признак внутри самого ролика.
        self.thumb_style = r.choice(["lower_left", "lower_band", "upper_left"])

        self._last_move = None
        self._last_family = None
        self._family_run = 0
        self._used_frames = {}

    # ---------- движение камеры ----------

    def pick_move(self, motion_scale: float = 1.0, allow_hold=True,
                  only=None):
        """
        Движение и скорость для очередного кадра.

        Два правила поверх жребия. Первое: одно и то же движение не идёт
        два раза подряд — было и раньше. Второе: не идут подряд три
        движения ОДНОЙ СЕМЬИ. push_in и push_left это оба наезда, зритель
        видит их как одно, и «разные движения» из трёх наездов подряд —
        ровно тот случай, когда разнообразие есть в логе и нет на экране.

        only — ограничить набор именами движений. Нужно вступлению, где
        уместны лишь скольжение и наезд. ВАЖНО, что это параметр, а не
        подмена результата снаружи: раньше вступление брало движение
        отсюда и, если оно не подходило, перевыбирало своим жребием — мимо
        счётчика семей. Проверка плана нашла на этом девять наездов подряд
        в первых трёх минутах, то есть ровно тот узор, который счётчик и
        обязан ловить.
        """
        weights = MOTION_BIAS.get(self.motion_bias, MOTION_BIAS["mixed"])
        allowed = set(only) if only else None
        fams = [f for f in MOVE_FAMILY
                if (allow_hold or f != "hold")
                and (allowed is None or any(m in allowed for m in MOVE_FAMILY[f]))]
        if not fams:
            fams = [f for f in MOVE_FAMILY if allow_hold or f != "hold"]
            allowed = None
        # семья не третий раз подряд
        if self._family_run >= 2 and self._last_family in fams and len(fams) > 1:
            fams = [f for f in fams if f != self._last_family]

        fam = self.rng.choices(fams, weights=[weights.get(f, 1.0) for f in fams])[0]
        family_moves = [m for m in MOVE_FAMILY[fam]
                        if allowed is None or m in allowed] or MOVE_FAMILY[fam]
        pool = [m for m in family_moves if m != self._last_move] or family_moves
        move = self.rng.choice(pool)

        self._family_run = self._family_run + 1 if fam == self._last_family else 1
        self._last_family = fam
        self._last_move = move

        # Амплитуда: базовый жребий × ось ролика × замысел доли. Зажата,
        # чтобы наезд не выехал за рабочий холст — см. SPEED_CLAMP.
        speed = self.rng.uniform(0.82, 1.30) * self.motion_amp * motion_scale
        speed = max(SPEED_CLAMP[0], min(SPEED_CLAMP[1], speed))
        return move, round(speed, 3)

    # ---------- переходы ----------

    def pick_transition(self, short=False):
        """Переход и его длительность. short — для быстрой перебивки."""
        if self.rng.random() < self.hard_cut_probability:
            return "cut", 0.0
        tr = (self.main_tr if self.rng.random() < self.transition_focus
              else self.rng.choice(self.transitions))
        if short:
            d = round(self.rng.uniform(*self.intro_transition_duration_range), 2)
        else:
            d = round(self.rng.uniform(*self.tr_dur_range), 2)
        return tr, d

    # ---------- параметры одного кадра ----------

    def shot_for(self, beat, pacing, beat_index: int, t: float):
        """
        Настройки кадра, начинающегося в секунду t внутри доли beat.

        Это основной путь. Старый st.clip() оставлен ниже для совместимости
        со спецификациями и тестами, но план ролика строится через этот
        метод: он единственный знает про замысел доли.
        """
        want, why = pacing.want(beat_index, beat, t, spread=self.shot_spread)
        want = round(max(2.2, min(want, 24.0)), 2)
        move, speed = self.pick_move(pacing.motion_scale(beat))
        tr, trd = self.pick_transition()
        return dict(duration=want, move=move, speed=speed,
                    transition=tr, transition_dur=trd,
                    effect=self.effect(), why=why, beat_kind=beat.kind)

    def clip(self, index: int, total: int, is_anchor: bool = False):
        """
        СТАРЫЙ путь: настройки кадра по позиции на таймлайне.

        Оставлен работающим намеренно. Во-первых, на него опираются
        спецификации с base_duration_range и deceleration_range. Во-вторых,
        если разбор сценария почему-либо не дал ни одной доли (пустые
        тайм-коды, странный сценарий), план обязан собраться хоть как-то —
        и собирается по этой формуле.
        """
        r = self.rng
        pos = min(1.0, index / max(total - 1, 1))
        dur = self.base_dur * (1 + (self.decel - 1) * pos)
        dur *= 1 + r.uniform(-self.jitter, self.jitter)
        if is_anchor:
            dur = r.choice([dur * 2.4, dur * 0.42])
        dur = round(max(2.6, min(dur, 22.0)), 2)
        move, speed = self.pick_move()
        tr, trd = self.pick_transition()
        return dict(duration=dur, move=move, speed=speed, transition=tr,
                    transition_dur=trd, effect=self.effect(),
                    why="запасной путь: кривая по таймлайну", beat_kind=None)

    def effect(self):
        """Имя эффекта на этот кадр или None."""
        if not self.effects_enabled or not self.effects:
            return None
        if self.rng.random() >= self.effect_probability:
            return None
        return self.rng.choice(self.effects)

    def framing(self, image_id: str):
        """
        Кадрирование при повторном показе одной и той же картинки.

        Первый показ тянется из наклона ролика (общими планами или
        крупными), последующие — из неиспользованных, чтобы одна картинка
        не выходила дважды одним и тем же кадром.
        """
        used = self._used_frames.setdefault(image_id, [])
        weights = FRAMING_BIAS.get(self.framing_bias, FRAMING_BIAS["balanced"])
        pool = [f for f in FRAMINGS if f not in used] or list(FRAMINGS)
        pick = self.rng.choices(pool, weights=[weights.get(f, 1.0) for f in pool])[0]
        used.append(pick)
        return pick, FRAMINGS[pick]

    def anchor_positions(self, total: int):
        """
        1-2 позиции, где ритм сознательно ломается.

        При разборе по долям почти не нужны: выдохи из pacing.py делают ту
        же работу осмысленнее. Метод остаётся ради запасного пути.
        """
        n = self.rng.choice([1, 2])
        lo, hi = int(total * 0.25), int(total * 0.85)
        if hi <= lo:
            return []
        return sorted(self.rng.sample(range(lo, hi), min(n, hi - lo)))

    def summary(self):
        return {
            "video_id": self.video_id,
            "lut": self.lut,
            "archive_lut": self.archive_lut,
            "generated_share": self.generated_share,
            "grain": self.grain,
            "vignette": self.vignette,
            "sparks": self.sparks_variant if self.sparks_enabled else None,
            "spark_px_sec": list(self.spark_speed_px_sec),
            "spark_flicker": list(self.spark_flicker),
            "spark_opacity": self.spark_opacity,
            "haze": self.haze_enabled,
            "transitions": len(self.transitions),
            "effects": len(self.effects) if self.effects_enabled else 0,
            "effect_p": self.effect_probability,
            "transition_dur": list(self.tr_dur_range),
            "transition_focus": self.transition_focus,
            "hard_cut_p": self.hard_cut_probability,
            "intro_footage_s": self.intro_footage_seconds,
            "intro_clip_s": list(self.intro_clip_duration_range),
            "intro_photo_s": list(self.intro_photo_duration_range),
            "body_clip_every": self.body_clip_every_n_shots,
            "base_duration": round(self.base_dur, 2),
            "deceleration": round(self.decel, 2),
            "arc": self.arc,
            "breath_rate": self.breath_rate,
            "shot_spread": self.shot_spread,
            "motion_amp": self.motion_amp,
            "motion_bias": self.motion_bias,
            "framing_bias": self.framing_bias,
            "main_transition": self.main_tr,
            "transition_duration": self.tr_dur,
            "opening": self.opening,
            "thumb_style": self.thumb_style,
            "text_style": self.text_style,
            "text_density": self.text_density,
            "duck_style": self.duck_style,
            "duck_depth": self.duck_depth,
        }


if __name__ == "__main__":
    import json
    for vid in ["video-01-lhc", "video-02-titanic", "video-03-pyramids"]:
        s = StyleEngine(vid, diverge=False)
        print(json.dumps(s.summary(), ensure_ascii=False, indent=2))
