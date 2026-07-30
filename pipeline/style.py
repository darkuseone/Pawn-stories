"""
style.py — движок антишаблонности.

Каждый ролик получает свой набор визуальных параметров, выпадающий
случайно, но воспроизводимо: seed берётся из id ролика. Один и тот же
id всегда даст один и тот же стиль (можно перезапустить рендер и получить
то же самое), а разные id — гарантированно разные ролики.
"""

import random
import hashlib

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


def seed_from(video_id: str) -> int:
    return int(hashlib.sha256(video_id.encode()).hexdigest()[:12], 16)


class StyleEngine:
    """Выдаёт визуальный стиль ролика и параметры каждого кадра."""

    def __init__(self, video_id: str, recent_luts=None, recent_openings=None,
                 recent_transitions=None, recent_sparks=None):
        """
        recent_* — что использовалось в последних роликах канала. Эти
        варианты исключаются жёстко. Без этого случайность регулярно выдаёт
        три похожих ролика подряд, а YouTube показывает соседние загрузки
        канала рядом и зритель видит их именно рядом.

        Списки приходят из channel.py — журнала канала. Раньше их надо было
        вписывать в спецификацию руками, и они, разумеется, оставались
        пустыми: защита от повторов существовала только на бумаге.
        """
        self.video_id = video_id
        self.rng = random.Random(seed_from(video_id))
        r = self.rng

        recent_luts = list(recent_luts or [])[-3:]
        recent_openings = list(recent_openings or [])[-2:]
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
        #
        # Доля считается по времени и выдерживается на ходу: см. MaterialMix
        # в build.py. Проверяется замером — build.py печатает, что вышло.
        self.generated_share = 0.30

        # Сжатие финального видео. Вынесено сюда, потому что упирается в
        # жёсткий лимит: файл в GitHub Releases не может быть больше 2 ГБ, а
        # получасовой ролик в 1080p/30 подходит к нему вплотную. Зерно
        # плёнки — шум, и оно бьёт по сжатию сильнее всего остального:
        # замер на тестовой сборке дал 9.5 Мбит/с, то есть 2.8 ГБ на
        # тридцать девять минут. crf 22-23 возвращает файл в лимит,
        # preset влияет на сжатие сильнее, чем на картинку.
        #
        # Больше crf — меньше файл и хуже картинка. Медленнее preset —
        # меньше файл при том же качестве, но дольше рендер.
        self.crf = 20
        self.preset = "veryfast"

        # Зерно плёнки. Раньше у каждого пятого ролика его не было вовсе —
        # это было сознательной защитой от шаблона. Теперь зерно просят
        # всегда, как часть киношной фактуры, поэтому ноль убран, а
        # разброс оставлен: 5 — почти незаметно, 11 — заметная плёнка.
        self.grain = r.choices([5, 8, 11], weights=[34, 40, 26])[0]

        # Виньетка: иногда выключена полностью.
        self.vignette = r.choices([0.0, 3.6, 4.6, 5.6], weights=[22, 26, 30, 22])[0]

        # Дымка снята с канала совсем. Флаг оставлен, чтобы её можно было
        # вернуть из спецификации, но по умолчанию слоя нет: атмосферность
        # уже сидит в LUT через подъём чёрного, а второй слой только мылит.
        self.haze_enabled = False

        # Искры — подпись канала, но не в каждом ролике. Приём, который стоит
        # на всех загрузках подряд, перестаёт быть подписью и становится
        # шаблоном: именно по таким постоянным признакам канал и опознаётся
        # как поточный. Раз в пять-шесть роликов искр нет вовсе.
        self.sparks_enabled = r.random() > 0.17
        spark_pool = [v for v in (1, 2, 3) if v not in recent_sparks] or [1, 2, 3]
        self.sparks_variant = r.choice(spark_pool)

        # Скорость задаётся В ПИКСЕЛЯХ В СЕКУНДУ и печётся прямо в петлю.
        # Так её видно и можно проверить линейкой, а не через множитель
        # setpts, у которого физического смысла нет.
        self.spark_speed_px_sec = (80.0, 120.0)
        # Мерцание в радианах в секунду: 4-8 это примерно 0.6-1.3 вспышки
        # в секунду на искру. Раньше стояло 0.6-2.1, то есть одна вспышка
        # за пять секунд — глаз такое не читает как мерцание вовсе.
        self.spark_flicker = (4.0, 8.0)
        self.spark_size = (1.0, 3.0)
        # искры иногда сносит вправо, иногда влево
        self.spark_flip = r.random() < 0.5

        # Прозрачность готового слоя — одна-единственная ручка. В генераторе
        # искры печатаются на полную яркость, вся сила наложения здесь.
        self.spark_opacity = 0.42

        # Базовая длительность кадра и разброс — задают темп ролика.
        # реальная длительность выходит выше заданной: кадр всегда
        # тянется до конца предложения. Поправка учтена в этих числах.
        self.base_dur = r.uniform(5.4, 7.2)
        self.jitter = r.uniform(0.14, 0.26)

        # Плотность: к финалу кадры длиннее. Ролик «успокаивается».
        self.decel = r.uniform(1.15, 1.55)

        # Основной переход ролика + запасные для разнообразия. Основной
        # звучит в 72% склеек, поэтому именно он задаёт «почерк» монтажа и
        # именно его нельзя повторять от ролика к ролику.
        self.transitions = list(TRANSITIONS)
        tr_pool = [t for t in self.transitions
                   if t not in recent_transitions] or self.transitions
        self.main_tr = r.choice(tr_pool)
        # Длительность перехода: диапазон, а не три фиксированных значения —
        # так её можно двигать из спецификации ролика одной строкой.
        self.tr_dur_range = (1.0, 1.8)
        self.tr_dur = round(r.uniform(*self.tr_dur_range), 2)
        # Жёсткая склейка выключена: 0 вместо прежних 0.09.
        self.hard_cut_probability = 0.0

        # Вступление. Куски короткие: длинный стоковый клип в начале ролика
        # выключают так же охотно, как статичную картинку.
        # Около трёх минут — столько держится быстрая перебивка на этом
        # канале. Именно ОКОЛО: длина вступления выпадает из диапазона, а не
        # стоит ровно на 180 у всех роликов. Одинаковая до секунды точка,
        # где ролик переключает темп, — признак поточной сборки, заметный
        # даже без сравнения роликов между собой.
        self.intro_footage_seconds = round(r.uniform(150.0, 215.0), 1)
        self.intro_clip_duration_range = (2.0, 4.0)     # видео
        self.intro_photo_duration_range = (3.0, 7.0)    # фотография
        self.intro_clip_share = 0.6                     # доля видео в перебивке
        self.intro_transition_duration_range = (0.35, 0.7)
        self.body_clip_every_n_shots = 4

        # Эффекты кадра. Треть кадров — не каждый: эффект, стоящий на всём
        # подряд, перестаёт читаться как приём и становится браком плёнки.
        self.effects_enabled = True
        self.effects = list(EFFECTS)
        self.effect_probability = 0.32

        # Тип открытия: как выглядят первые секунды. Самая заметная зрителю
        # ось разнообразия — первые пять секунд решают, останется он или нет,
        # и одинаковое начало у всех роликов канала бросается в глаза быстрее
        # всего остального.
        #
        # РАНЬШЕ ЭТО ПОЛЕ НИ НА ЧТО НЕ ВЛИЯЛО. Оно вычислялось, защищалось от
        # повторов, печаталось в сводку — и нигде не использовалось: все
        # ролики открывались одинаково. Теперь варианты разведены в build.py:
        #
        #   quick_cuts    сразу очередь самых коротких кусков, без разгона
        #   long_footage  один длинный establishing-кадр, потом перебивка
        #   black_card    открытие из чёрного, кадр проявляется
        OPENINGS = ("long_footage", "quick_cuts", "black_card")
        open_pool = [o for o in OPENINGS if o not in recent_openings] or list(OPENINGS)
        self.opening = r.choice(open_pool)

        # Раскладка превью. YouTube показывает превью соседних загрузок в
        # одном ряду, поэтому одинаковая вёрстка подписи опознаётся как
        # серия быстрее, чем любой признак внутри самого ролика.
        self.thumb_style = r.choice(["lower_left", "lower_band", "upper_left"])

        self._last_move = None
        self._used_frames = {}

    # ---------- параметры одного кадра ----------

    def clip(self, index: int, total: int, is_anchor: bool = False):
        """Настройки одного кадра. is_anchor — намеренно выбивающийся кадр."""
        r = self.rng
        # pos ОБРЕЗАЕТСЯ единицей. Без обрезки: total — это оценка числа
        # кадров, посчитанная до раскладки, и она регулярно занижена вдвое.
        # Тогда pos у последних кадров доходит до 2 и больше, а
        # dur = base*(1+(decel-1)*pos) разгоняется далеко за заказанное
        # замедление — кадры в конце упираются в потолок 22 секунды, и
        # финал ролика превращается в набор долгих статик.
        pos = min(1.0, index / max(total - 1, 1))

        # Длительность растёт к концу ролика
        dur = self.base_dur * (1 + (self.decel - 1) * pos)
        dur *= 1 + r.uniform(-self.jitter, self.jitter)

        if is_anchor:
            # Намеренная асимметрия: либо очень длинный кадр под важной
            # мыслью, либо резко короткий в быстрой серии.
            dur = r.choice([dur * 2.4, dur * 0.42])

        dur = round(max(2.6, min(dur, 22.0)), 2)

        # Движение не повторяется два раза подряд
        choices = [m for m in MOVES if m != self._last_move]
        move = r.choice(choices)
        self._last_move = move

        speed = r.uniform(0.85, 1.35)
        # срез TRANSITIONS[:-1] убран: раньше последним в списке лежала
        # жёсткая склейка и её приходилось отрезать, теперь списка резких нет
        tr = "cut" if r.random() < self.hard_cut_probability else (
            self.main_tr if r.random() < 0.72 else r.choice(self.transitions)
        )

        return dict(
            duration=dur,
            move=move,
            speed=round(speed, 3),
            transition=tr,
            transition_dur=0.0 if tr == "cut" else round(
                r.uniform(*self.tr_dur_range), 2),
            effect=self.effect(),
        )

    def effect(self):
        """Имя эффекта на этот кадр или None. Выпадает примерно на треть."""
        if not self.effects_enabled or not self.effects:
            return None
        if self.rng.random() >= self.effect_probability:
            return None
        return self.rng.choice(self.effects)

    def framing(self, image_id: str):
        """Кадрирование при повторном показе одной и той же картинки."""
        used = self._used_frames.setdefault(image_id, [])
        pool = [f for f in FRAMINGS if f not in used] or list(FRAMINGS)
        if not used:
            pick = "wide" if self.rng.random() < 0.7 else "medium"
        else:
            pick = self.rng.choice([f for f in pool if f != "wide"] or pool)
        used.append(pick)
        return pick, FRAMINGS[pick]

    def anchor_positions(self, total: int):
        """1-2 позиции, где ритм сознательно ломается."""
        n = self.rng.choice([1, 2])
        lo, hi = int(total * 0.25), int(total * 0.85)
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
            "hard_cut_p": self.hard_cut_probability,
            "intro_footage_s": self.intro_footage_seconds,
            "intro_clip_s": list(self.intro_clip_duration_range),
            "intro_photo_s": list(self.intro_photo_duration_range),
            "body_clip_every": self.body_clip_every_n_shots,
            "base_duration": round(self.base_dur, 2),
            "deceleration": round(self.decel, 2),
            "main_transition": self.main_tr,
            "transition_duration": self.tr_dur,
            "opening": self.opening,
            "thumb_style": self.thumb_style,
        }


if __name__ == "__main__":
    import json
    for vid in ["video-01-lhc", "video-02-titanic", "video-03-pyramids"]:
        s = StyleEngine(vid)
        print(json.dumps(s.summary(), ensure_ascii=False, indent=2))
