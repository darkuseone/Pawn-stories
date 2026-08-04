"""
channel.py — память канала. Следит, чтобы соседние ролики не были похожи.

    python pipeline/channel.py list                 что уже выходило
    python pipeline/channel.py check jobs/x.json    не повтор ли это
    python pipeline/channel.py record jobs/x.json   записать после выкладки

Зачем это нужно
---------------
Стиль ролика выпадает случайно, но воспроизводимо — seed из id. Случайность
без памяти регулярно выдаёт три похожих ролика подряд: тот же цветокор, тот
же основной переход, то же открытие. YouTube показывает соседние загрузки
канала рядом, и зритель видит их именно рядом. Канал, у которого все ролики
на одно лицо, читается как поток с конвейера — и зрителем, и площадкой.

Поэтому здесь ведётся журнал: что уже выходило и с какими настройками.
Движок стиля получает из него списки «этого не брать» и обходит недавнее
жёстко, а не по счастливой случайности.

Что именно разводится
---------------------
  тема            главное. Два ролика про монеты подряд — это одна тема,
                  как бы по-разному они ни назывались
  цветокор        3 последних не повторяются
  тип открытия    2 последних не повторяются
  основной переход 2 последних
  вариант искр    2 последних
  темп, зерно, виньетка, длина вступления — выпадают из диапазонов, то есть
                  разные у каждого ролика сами по себе

Журнал лежит в channel/log.json и правится руками без опаски: это просто
список. Чего в нём нет, того движок и не избегает.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
LOG = ROOT / "channel" / "log.json"

# Сколько последних роликов учитывать по каждой оси. Больше — разнообразнее,
# но и пул быстрее кончается: цветокоров всего пять, при глубине 5 выбирать
# станет не из чего и защита выключится сама.
# Подложка: глубина 2. Больше нельзя — подложек всего пять, при глубине 4-5
# выбирать станет не из чего и защита выключится сама (ровно то же
# соображение, что и по цветокорам выше).
DEPTH = {"lut": 3, "opening": 2, "main_transition": 2, "sparks": 2,
         "music": 2}

# Насколько темы должны различаться. 0.34 значит: если треть значимых слов
# совпала, это уже та же тема. Подобрано на глаз и намеренно строго —
# ложная тревога стоит минуты на переименование, пропущенный повтор стоит
# ролика, который зритель уже видел.
TOPIC_OVERLAP_LIMIT = 0.34
TOPIC_DEPTH = 8

# Слова, которые есть в каждой теме канала и потому ничего не различают.
STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
        "with", "from", "that", "this", "it", "is", "was", "were", "be",
        "story", "stories", "found", "find", "finds", "worth", "value",
        "auction", "antique", "antiques", "old", "rare", "sold", "sale"}


def log(*a):
    print(*a, flush=True)


def load():
    if not LOG.exists():
        return {"channel": "pawn-stories", "aired": []}
    return json.loads(LOG.read_text(encoding="utf-8"))


def save(data):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")


def aired(data=None):
    """Только выложенное. Тестовые сборки в счёт не идут."""
    d = data or load()
    return [e for e in d.get("aired", []) if not e.get("test")]


def recent(field: str, n: int = None, data=None):
    """
    Последние n значений поля. Пустые пропускаются: ролик, записанный без
    какого-то поля, не должен запрещать это поле навсегда.
    """
    n = n or DEPTH.get(field, 3)
    vals = [e.get(field) for e in aired(data) if e.get(field) is not None]
    return vals[-n:]


def avoid(data=None):
    """Всё, чего движку стиля брать не следует. Одним словарём."""
    d = data or load()
    return {f: recent(f, DEPTH[f], d) for f in DEPTH}


# ─────────────────────────── ТЕМЫ ───────────────────────────

def words(text: str):
    """Значимые слова темы: без пунктуации, без общих для канала слов."""
    w = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", (text or "").lower())
    return {x for x in w if len(x) > 2 and x not in STOP}


def topic_words(job):
    """
    Слова темы ролика. Берутся из блока topic, а если его нет — из
    заголовка и названий глав: они всегда есть и всегда про суть.
    """
    t = job.get("topic") or {}
    src = " ".join([t.get("slug", ""), " ".join(t.get("keywords", []))])
    if not src.strip():
        y = job.get("youtube") or {}
        src = " ".join([y.get("title", "")] + list(y.get("chapters", [])))
    return words(src)


def overlap(a: set, b: set) -> float:
    """Жаккар: сколько общего в двух наборах слов."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check(job, data=None):
    """
    Похож ли ролик на что-то недавнее. Возвращает список претензий.

    НЕ роняет сборку сам по себе. Проверка, которая останавливает работающий
    пайплайн, хуже отсутствующей: тема может совпадать словами и расходиться
    по сути, и решать это человеку, а не сравнению множеств.
    """
    d = data or load()
    mine = topic_words(job)
    problems = []

    for e in aired(d)[-TOPIC_DEPTH:]:
        o = overlap(mine, words(" ".join(
            [e.get("topic", "")] + list(e.get("keywords", [])))))
        if o >= TOPIC_OVERLAP_LIMIT:
            problems.append(
                f"тема пересекается с «{e.get('topic') or e.get('id')}» "
                f"на {o*100:.0f}% (порог {TOPIC_OVERLAP_LIMIT*100:.0f}%)")

    ov = (job.get("style_override") or {})
    av = avoid(d)
    for field, key in (("lut", "lut"), ("opening", None)):
        val = job.get(key) or ov.get(key) if key else None
        if val and val in av.get(field, []):
            problems.append(f"{field} «{val}» уже был в последних "
                            f"{DEPTH[field]} роликах")
    return problems


# ─────────────────────────── ЗАПИСЬ ───────────────────────────

def entry_from(job, style_card: dict, test=False):
    t = job.get("topic") or {}
    y = job.get("youtube") or {}
    return {
        "id": job["id"],
        "date": date.today().isoformat(),
        "topic": t.get("slug") or y.get("title", ""),
        "keywords": t.get("keywords", []),
        "title": y.get("title", ""),
        "lut": style_card.get("lut"),
        "archive_lut": style_card.get("archive_lut"),
        "music": style_card.get("music"),
        "opening": style_card.get("opening"),
        "main_transition": style_card.get("main_transition"),
        "sparks": style_card.get("sparks"),
        "intro_seconds": style_card.get("intro_footage_s"),
        "base_duration": style_card.get("base_duration"),
        "generated_share": style_card.get("generated_share"),
        "thumb_style": style_card.get("thumb_style"),
        "arc": style_card.get("arc"),
        # ВЕКТОР СТИЛЯ ЦЕЛИКОМ. Главное, что здесь появилось: без него
        # разведение роликов невозможно в принципе. Поосевые поля выше
        # умеют сказать «этот LUT уже был», но не умеют сказать «этот ролик
        # похож на прошлый», а похожесть — свойство сочетания, а не поля.
        # Читает editorial/memory.py.
        "style_vector": style_card.get("style_vector") or {},
        # Метрики готового плана. Нужны не движку, а человеку: по ряду из
        # десяти роликов видно дрейф — если у всех подряд одинаковый
        # разброс длительностей, генератор где-то заклинило, и ни одна
        # поосевая проверка этого не покажет.
        "plan_metrics": style_card.get("plan_metrics") or {},
        # Что из общего пула стока и архива уже показано. Следующий ролик
        # начнёт подбор с гандикапом на эти файлы, см. ShotPicker.prior.
        "assets_used": style_card.get("assets_used") or {},
        **({"test": True} if test else {}),
    }


def record(job_path, test=False):
    """
    Дописывает ролик в журнал. Запускается ПОСЛЕ выкладки, а не после сборки:
    пересобранный десять раз ролик не должен десять раз занимать место в
    списке недавних и выталкивать оттуда настоящие.
    """
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    card_path = ROOT / "work" / job["id"] / "out" / "style.json"
    if not card_path.exists():
        raise SystemExit(f"нет {card_path} — сначала собери ролик")
    card = json.loads(card_path.read_text(encoding="utf-8"))

    data = load()
    data.setdefault("aired", [])
    data["aired"] = [e for e in data["aired"] if e["id"] != job["id"]]
    data["aired"].append(entry_from(job, card, test=test))
    save(data)
    log(f"записан {job['id']}: {card.get('lut')}, {card.get('opening')}, "
        f"переход {card.get('main_transition')}")
    return data


def main(argv):
    if len(argv) < 2 or argv[1] == "list":
        d = load()
        rows = d.get("aired", [])
        if not rows:
            log("журнал пуст — это будет первый ролик канала")
            return
        log(f"{'id':<18}{'дата':<12}{'цветокор':<14}{'открытие':<14}тема")
        for e in rows:
            mark = " (тест)" if e.get("test") else ""
            log(f"{e['id']:<18}{e.get('date',''):<12}"
                f"{str(e.get('lut')):<14}{str(e.get('opening')):<14}"
                f"{e.get('topic','')}{mark}")
        log("\nчего избегать в следующем ролике:")
        for f, v in avoid(d).items():
            log(f"  {f:<16} {v or '—'}")
        return

    cmd = argv[1]
    if cmd == "check":
        job = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        problems = check(job)
        if not problems:
            log("не повтор: тема и настройки расходятся с недавними")
            return
        log("ПОХОЖЕ НА НЕДАВНЕЕ:")
        for p in problems:
            log(f"  ! {p}")
        sys.exit(1)
    elif cmd == "record":
        record(argv[2], test="--test" in argv)
    else:
        raise SystemExit(f"не знаю команды {cmd!r}; есть: list, check, record")


if __name__ == "__main__":
    main(sys.argv)
