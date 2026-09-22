"""
jobspec.py — чтение спецификации ролика с подмешиванием умолчаний канала.

    from jobspec import load_job
    job = load_job("jobs/pawn-01.json")

Зачем
-----
Спецификация ролика описывает ОДИН ролик, но половина её объёма — это
настройки КАНАЛА, одинаковые во всех семи файлах: те же переходы, те же
эффекты, тот же crf, тот же диапазон длительностей. Скопированные семь раз,
они расходятся: правка «на будущее» попадает в тот файл, который был открыт,
и следующий ролик собирается по старым числам, потому что в его копии
правки нет. Это не гипотеза — ровно так в спецификациях разошлись наборы
эффектов.

Теперь общее лежит в channel/defaults.json, а в спецификации остаётся то,
что делает ролик ЭТИМ роликом: сценарий, промпты, запросы, тема, петля.
Правила CTR для фонов обложек — отдельно в channel/covers.json: их читает
covers.art_prompts(), а не это слияние. Это не style_override: блок
нужно дописать к каждому промпту, даже если в спецификации уже есть
свои два сюжета, иначе правка «120 px / правая половина» снова разъедется
по файлам.

Правила слияния — намеренно скучные, чтобы результат можно было предсказать,
не заглядывая сюда:

  - ключ есть в спецификации  -> берётся из спецификации, целиком
  - ключа нет                 -> берётся из умолчаний
  - style_override            -> сливается ПОКЛЮЧЕВО (единственное
                                 исключение: иначе ролик, которому нужен
                                 один свой crf, потерял бы весь остальной
                                 блок)
  - списки                    -> НЕ сливаются: список в спецификации
                                 заменяет список умолчаний целиком

Списки не сливаются сознательно. «Добавить один переход к умолчаниям» и
«взять ровно эти переходы» — разные намерения, и склейка списков молча
превращает второе в первое. Роликов, которым нужен именно свой короткий
набор, у нас большинство.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULTS = ROOT / "channel" / "defaults.json"

# Ключи-комментарии. В спецификациях канала принято подписывать поля
# соседним ключом с подчёркиванием — они не данные и в слиянии не участвуют.
def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def load_defaults() -> dict:
    if not DEFAULTS.exists():
        return {}
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def merge(defaults: dict, job: dict) -> dict:
    """Умолчания канала под спецификацией ролика. Спецификация выигрывает."""
    out = dict(defaults)
    for k, v in job.items():
        if (k == "style_override" and isinstance(v, dict)
                and isinstance(out.get(k), dict)):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def load_job(path) -> dict:
    """
    Спецификация ролика, готовая к работе.

    Единственная точка чтения: если завтра появится ещё один слой умолчаний
    (например, на рубрику), он появится здесь, а не в девяти модулях.
    """
    job = json.loads(Path(path).read_text(encoding="utf-8"))
    d = _clean(load_defaults())
    if not d:
        return job
    return merge(d, job)


# ─────────────────────── ПРОВЕРКА ПОЛЕЙ ───────────────────────
#
# ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА, КОГДА ЕСТЬ style_override
# ---------------------------------------------------
# Неизвестное поле ВНУТРИ style_override роняет прогон (build.py,
# apply_style_override) — и это правильно. Но верхний уровень спецификации
# не проверялся вовсе, и опечатка там ничего не роняет: поле просто никто
# не читает, а ролик собирается на умолчании. Ровно так на канале уже
# живёт тихая ошибка: ЧЕТЫРЕ спецификации задают
#
#     "voice_settings": {"similarity_boost": 0.75}
#
# — имя из документации ElevenLabs, — а build_voice читает vs["similarity"]
# и на отсутствие ключа честно берёт 0.78. Голос озвучен НЕ ТЕМИ
# настройками, деньги за него уже списаны, и в логе об этом ни строки.
# Тот же класс, что и знаменитый инцидент с невалидными полями: проверять
# надо ДО того, как потрачено, а не после.
#
# Поэтому список ключей закрытый. Новое поле в спецификации — строка сюда;
# это дешевле одного прогона, потраченного на молча проигнорированную
# настройку.

# ФОРМАТ КАНАЛА — 15-25 минут. Не догма для конкретного ролика (длину
# иногда задаёт человек вместе с темой), а рамка, за которой стоит
# переспросить: на 10 минутах жанр не успевает выдать путь к сумме, на 35
# монтаж упирается в лимит раннера и в 2 ГБ Releases.
CHANNEL_MINUTES = (15, 25)

KNOWN_TOP_LEVEL = {
    # что делает ролик ЭТИМ роликом
    "id", "script_blocks", "topic", "youtube", "open_loop",
    # материал
    "image_prompts", "image_model", "footage_queries", "archive_queries",
    "graphic_queries", "fill_prompts", "fill_limit", "reject",
    "trusted_sources", "top_up_budget", "material_overshoot",
    "photo_sources", "video_sources",
    # материал, закреплённый ЗАРАНЕЕ прямыми ссылками (см. PINNED_KEYS)
    "pinned_footage", "pinned_archive",
    # сцены для поиска материала глазами (pipeline/scout.py, SCENE_KEYS)
    "scenes",
    # расчёт хронометража: [минимум, максимум] минут, под который написан
    # сценарий. Ничего не задаёт монтажу — это запись решения, чтобы на
    # пересборке было видно, на какую длину рассчитывали.
    "target_minutes",
    # голос
    "voice_settings", "voice_id",
    # отбраковка
    "vet_vision", "vet_model",
    # картинка и звук
    "style_override", "lut", "archive_lut", "music", "bed_gain_db",
    "recent_luts", "recent_openings",
    # надписи длинного ролика
    "burn_subs", "outro_cta",
    # генерация
    "batch", "magnific_enabled", "magnific_image_share",
    "magnific_image_models",
    "magnific_video_share", "magnific_video_gen_enabled",
    "cover_text_by_model",
}

# Настройки голоса. similarity_boost — имя ИЗ API ElevenLabs, и в
# спецификациях оно встречается чаще «правильного»: его подставляет любой
# пример из документации. Читаются оба (assets.build_voice), но сказать об
# этом надо — иначе два имени в семи файлах разъедутся молча.
VOICE_KEYS = {"stability", "similarity", "similarity_boost", "style",
              "use_speaker_boost", "speed"}

LIST_FIELDS = ("script_blocks", "image_prompts", "footage_queries",
               "archive_queries", "graphic_queries", "fill_prompts",
               "photo_sources", "video_sources", "trusted_sources",
               "recent_luts", "recent_openings",
               "pinned_footage", "pinned_archive", "scenes")

# ЗАКРЕПЛЁННЫЙ МАТЕРИАЛ. Ключ записи: url обязателен, остальное — по
# желанию.
#
#   url   — прямая ссылка на файл (CDN стока, файл архива). Не страница!
#   q     — слова, под которые этот файл закреплён. Попадают в манифест, и
#           build.kw_of() подбирает по ним кадр под то, что звучит в эту
#           секунду. Без q файл ляжет под текст случайно — так что это
#           «по желанию» только формально.
#   src   — метка источника в имени файла и в манифесте. Умолчание
#           "pinned", и оно же стоит в TRUSTED_SOURCES: файл, который
#           человек (или чат) уже посмотрел глазами, не надо показывать
#           зрению за деньги второй раз.
#   kind  — "video" | "image". Умолчание берётся по папке: для
#           pinned_footage это video, для pinned_archive — image.
#   note  — свободная заметка, зачем этот файл. Никуда не читается, но
#           через месяц объясняет выбор лучше, чем сам url.
#   at    — ЦИТАТА из сценария (кусок фразы, дословно), под которую этот
#           файл выбран. Строка или список строк. Монтаж ставит файл ровно
#           под эту фразу (build.plan_shots) и бережёт его до неё. Без at
#           файл ложится по словам q — то есть примерно, а не точно.
#   t0    — секунда исходного клипа, с которой начинается годный кусок
#           (смотрели кадры — знаем, где заставка, где дрожит камера).
#           Клип режется от неё, а не из середины.
PINNED_KEYS = {"url", "q", "src", "kind", "note", "at", "t0"}

# СЦЕНЫ — задание на поиск материала (pipeline/scout.py). Одна сцена —
# один кусок сценария, под который нужен конкретный кадр.
#
#   id       — короткое имя (s01, s02…), из него номера кандидатов s01-03
#   at       — цитата из сценария, как у закреплённого файла
#   want     — ЧТО должно быть в кадре, английскими словами предмета. Уходит
#              в q закреплённого файла.
#   queries  — поисковые запросы к стокам (2-4 штуки, от узкого к широкому)
#   kind     — "video" (умолчание), "image" или "any"
#   n        — сколько кандидатов на запрос с источника (умолчание 4)
SCENE_KEYS = {"id", "at", "want", "queries", "kind", "n", "note"}


def norm_text(s: str) -> str:
    """Текст для сверки цитаты со сценарием: без регистра, пунктуации и
    лишних пробелов. Апостроф выбрасывается, иначе it's против its."""
    import re
    s = (s or "").lower().replace("\u2019", "").replace("'", "")
    return " ".join(re.findall(r"[\w$£€]+", s))


def at_list(item: dict) -> list[str]:
    at = item.get("at")
    if not at:
        return []
    return [a for a in ([at] if isinstance(at, str) else at) if str(a).strip()]


def find_at(texts: list[str], quote: str):
    """
    Индексы подряд идущих текстов (предложений), в которых стоит цитата.
    Сначала ищется в одном предложении, потом на стыке двух-трёх — цитата
    иногда захватывает конец одной фразы и начало следующей. None — нет.
    """
    q = norm_text(quote)
    if not q:
        return None
    normed = [norm_text(t) for t in texts]
    for span in (1, 2, 3):
        for i in range(len(normed) - span + 1):
            if q in " ".join(normed[i:i + span]):
                return list(range(i, i + span))
    return None


def check(job: dict) -> tuple[list[str], list[str]]:
    """
    Проверка спецификации ДО первых денег. Возвращает (стоп, заметки).

    «Стоп» — то, из-за чего прогон нельзя начинать: опечатка в имени поля
    означает молча неработающую настройку, а этап 1 стоит озвучки и
    генерации целиком. «Заметки» печатаются и ничего не роняют.

    Цветокоры проверяются здесь же по файлам в assets/luts: check_luts()
    в build.py делает то же самое, но на два этапа позже — когда голос уже
    оплачен.
    """
    stop, note = [], []
    # Цитаты at сверяются с блоками целиком: цитата живёт внутри одного
    # блока, а границы предложений здесь не нужны.
    script = [str(b) for b in (job.get("script_blocks") or [])
              if isinstance(b, str)]

    unknown = sorted(k for k in job
                     if not str(k).startswith("_") and k not in KNOWN_TOP_LEVEL)
    if unknown:
        stop.append(
            "неизвестные поля верхнего уровня: " + ", ".join(unknown) +
            ". Опечатка здесь ничего не роняет — поле просто никто не "
            "прочитает, и ролик соберётся на умолчании. Известные поля: " +
            ", ".join(sorted(KNOWN_TOP_LEVEL)))

    for name in LIST_FIELDS:
        val = job.get(name)
        if val is not None and not isinstance(val, list):
            stop.append(f"{name}: {type(val).__name__} вместо списка — "
                        f"строка разберётся по буквам, а не по запятым")

    vs = job.get("voice_settings")
    if vs is not None:
        if not isinstance(vs, dict):
            stop.append("voice_settings должно быть объектом")
        else:
            bad = sorted(k for k in vs if k not in VOICE_KEYS)
            if bad:
                stop.append(
                    "voice_settings: неизвестные ключи " + ", ".join(bad) +
                    ". Их никто не прочитает, а озвучка уже платная. "
                    "Допустимо: " + ", ".join(sorted(VOICE_KEYS)))
            if "similarity_boost" in vs and "similarity" not in vs:
                note.append(
                    "voice_settings.similarity_boost — имя из API ElevenLabs; "
                    "канал зовёт это поле similarity. Значение подхватывается, "
                    "но лучше свести к одному имени")
            for k in ("stability", "similarity", "similarity_boost", "style"):
                v = vs.get(k)
                if v is not None and not (0.0 <= float(v) <= 1.0):
                    stop.append(f"voice_settings.{k}={v} вне 0..1")

    luts = ROOT / "assets" / "luts"
    ov = job.get("style_override") or {}
    named = [(k, job.get(k)) for k in ("lut", "archive_lut") if job.get(k)]
    named += [(f"style_override.{k}", ov[k])
              for k in ("lut", "archive_lut") if ov.get(k)]
    have = sorted(p.stem for p in luts.glob("*.cube")) if luts.exists() else []
    for where, name in named:
        if have and name not in have:
            stop.append(f"{where}: нет цветокора {name!r}. Есть: " +
                        ", ".join(have))

    # ЗАКРЕПЛЁННЫЙ МАТЕРИАЛ. Ошибка здесь стоит дыры в ролике: файл не
    # скачается, слот закроется чем попало, и увидно это будет на готовом
    # монтаже. Поэтому опечатка в ключе — «стоп», а не заметка.
    for name in ("pinned_footage", "pinned_archive"):
        items = job.get(name)
        if not isinstance(items, list):
            continue                      # не список — уже поймано выше
        for i, it in enumerate(items):
            where = f"{name}[{i}]"
            if not isinstance(it, dict):
                stop.append(f"{where}: нужен объект {{\"url\": …}}, а не "
                            f"{type(it).__name__}")
                continue
            bad = sorted(k for k in it if k not in PINNED_KEYS)
            if bad:
                stop.append(f"{where}: неизвестные ключи " + ", ".join(bad) +
                            ". Допустимо: " + ", ".join(sorted(PINNED_KEYS)))
            url = str(it.get("url") or "")
            if not url.startswith(("http://", "https://")):
                stop.append(f"{where}: url {url!r} — нужна прямая ссылка на "
                            f"файл по http(s)")
            kind = it.get("kind")
            if kind is not None and kind not in ("video", "image"):
                stop.append(f"{where}: kind={kind!r} — только \"video\" "
                            f"или \"image\"")
            if not str(it.get("q") or "").strip():
                note.append(f"{where}: нет q — build.kw_of() не сможет "
                            f"подобрать этот файл по смыслу, и он ляжет под "
                            f"текст случайно")
            t0 = it.get("t0")
            if t0 is not None and not (isinstance(t0, (int, float)) and t0 >= 0):
                stop.append(f"{where}: t0={t0!r} — секунда исходника, число >= 0")
            # ЦИТАТА ОБЯЗАНА НАЙТИСЬ В СЦЕНАРИИ. Не нашлась — привязка
            # молча пропала бы, и файл лёг бы по словам куда придётся: ровно
            # та неточность, ради которой at и заведён. Стоп, а не заметка:
            # правка одной строки дешевле ролика с кадром не под своей фразой.
            for a in at_list(it):
                if find_at(script, a) is None:
                    stop.append(f"{where}: at «{a[:60]}» нет в script_blocks "
                                f"дословно — привязка к фразе не сработает")

    # СЦЕНЫ ПОИСКА. Те же требования к цитате, что у закреплённого файла:
    # scout.py pin переносит at сцены в запись как есть.
    scenes = job.get("scenes")
    if isinstance(scenes, list):
        ids = set()
        for i, sc in enumerate(scenes):
            where = f"scenes[{i}]"
            if not isinstance(sc, dict):
                stop.append(f"{where}: нужен объект")
                continue
            bad = sorted(k for k in sc if k not in SCENE_KEYS)
            if bad:
                stop.append(f"{where}: неизвестные ключи " + ", ".join(bad) +
                            ". Допустимо: " + ", ".join(sorted(SCENE_KEYS)))
            sid = str(sc.get("id") or "")
            if not sid or "-" in sid or "@" in sid:
                stop.append(f"{where}: id {sid!r} — нужен короткий id без "
                            f"«-» и «@» (s01)")
            elif sid in ids:
                stop.append(f"{where}: id {sid} повторяется")
            ids.add(sid)
            if not isinstance(sc.get("queries"), list) or not sc.get("queries"):
                stop.append(f"{where}: queries — непустой список запросов")
            if sc.get("kind", "video") not in ("video", "image", "any"):
                stop.append(f"{where}: kind — video, image или any")
            for a in at_list(sc):
                if find_at(script, a) is None:
                    stop.append(f"{where}: at «{a[:60]}» нет в script_blocks "
                                f"дословно")
            if not at_list(sc):
                note.append(f"{where}: нет at — закреплённый из этой сцены "
                            f"файл не будет привязан к фразе")

    tm = job.get("target_minutes")
    if tm is not None:
        ok = (isinstance(tm, (list, tuple)) and len(tm) == 2
              and all(isinstance(v, (int, float)) for v in tm)
              and tm[0] <= tm[1])
        if not ok:
            stop.append("target_minutes: нужен [минимум, максимум] минут")
        elif not (CHANNEL_MINUTES[0] <= tm[0] and tm[1] <= CHANNEL_MINUTES[1]):
            note.append(f"target_minutes={list(tm)} вне формата канала "
                        f"{list(CHANNEL_MINUTES)} минут")

    loop = job.get("open_loop") or {}
    per_block = loop.get("questions")
    if per_block is not None:
        if not isinstance(per_block, dict):
            stop.append("open_loop.questions: нужна карта "
                        "{\"номер_блока\": \"вопрос\"}, ключ строкой")
        else:
            bad = [k for k in per_block if not str(k).lstrip("-").isdigit()]
            if bad:
                stop.append(f"open_loop.questions: ключи {bad} — не номера "
                            f"блоков")

    # Сумма находки на обложке. Это не придирка к оформлению: то же поле
    # уходит в карточку-итог в конце ролика (type.outro_lines), и без
    # цифры концовка остаётся без рекапа. Заметка, а не стоп: ролик без
    # суммы бывает (обзор приёма, а не история находки).
    y = job.get("youtube") or {}
    sub = str(y.get("cover_sub") or "")
    if not y.get("cover_kicker"):
        note.append("нет youtube.cover_kicker — крючок обложки собирается "
                    "из заголовка механически и почти всегда выходит длиннее "
                    "5 слов")
    if not any(ch.isdigit() for ch in sub) and not any(c in sub for c in "$£€"):
        note.append("в youtube.cover_sub нет числа — на обложке не будет "
                    "суммы, и карточка-итог в конце ролика останется без "
                    "рекапа")

    music = job.get("music")
    if music:
        p = Path(music)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            note.append(f"music: файла {music} нет — подложку выберет жребий")

    return stop, note


def require_ok(job: dict, log=print) -> None:
    """Проверка как ворота: заметки в лог, «стоп» роняет прогон."""
    stop, note = check(job)
    for n in note:
        log(f"  ! {n}")
    if stop:
        raise SystemExit("спецификация не проходит проверку:\n  - "
                         + "\n  - ".join(stop))
