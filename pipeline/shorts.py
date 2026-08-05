"""
shorts.py — два вертикальных ролика из уже собранного длинного.

    python pipeline/shorts.py jobs/<id>.json

Запускается ПОСЛЕ build.py, на готовом final.mp4. Ничего не рендерит заново
и ничего не тратит: длинный ролик уже собран, здесь из него вырезаются два
куска, поворачиваются в 9:16 и получают шапку с вопросом и пословные
субтитры.

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

Почему ffmpeg, а не Remotion или HyperFrames
--------------------------------------------
Оба движка рисуют кадр браузером и снимают его покадрово. На двух шортсах
это 2×55 с × 30 к/с ≈ 3300 кадров через headless Chromium, плюс Node и npm
в репозитории, где сейчас только Python и ffmpeg. Проект уже один раз
упирался в скорость рендера (zoompan 31 с на секунду видео против 2.6 у
scale+crop — см. шапку build.py), и лимит Actions в 6 часов никуда не
делся.

При этом всё, что здесь нужно, ffmpeg делает нативно: crop+scale для 9:16,
drawbox для рамки, libass для пословных субтитров. Пословная анимация — это
одно событие ASS на слово, а не анимация в браузере.

Remotion имело бы смысл, если бы понадобилась настоящая моушн-графика:
3D-текст, сложные морфы, частицы, связанные с содержанием. Тогда это
отдельный шаг и отдельный разговор про время сборки.

Откуда берутся тайм-коды слов
-----------------------------
Из marks.json — границы предложений, посчитанные из посимвольного
выравнивания ElevenLabs. Внутри предложения слова раскладываются
пропорционально длине: точных границ слов в marks.json нет, а скорость речи
внутри одного предложения почти постоянна, и ошибка получается в пределах
десятых долей секунды. Для субтитра, который висит треть секунды, этого
достаточно; строить ради этого отдельный разбор block_NN.json не стоит —
он привязал бы шортсы к внутренностям озвучки.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job

from editorial import beats as beats_mod

ROOT = Path(__file__).parent.parent

W, H = 1080, 1920          # вертикальный кадр
FPS = 30

# Длина шортса. YouTube считает шортсом всё до 60 секунд включительно, но
# впритык к границе упираться незачем: кусок режется по границам
# предложений, и последнее предложение может оказаться длинным.
SHORT_MIN = 24.0
SHORT_MAX = 52.0

# Шапка с вопросом: 20% высоты кадра, как заказано.
BOX_SHARE = 0.20
BOX_MARGIN = 36            # отступ рамки от краёв кадра

# Субтитры: одно слово на экране. Слово короче этого висит дольше — иначе
# предлоги мелькают быстрее, чем глаз их берёт.
WORD_MIN_SEC = 0.18

FONT = "DejaVu Sans"


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


def window_for(beat, marks, total, min_s=SHORT_MIN, max_s=SHORT_MAX):
    """
    Границы куска вокруг доли, ПО ГРАНИЦАМ ПРЕДЛОЖЕНИЙ.

    Резать по секундомеру нельзя: шортс, начинающийся с середины слова,
    выглядит обрезком, а не роликом. Поэтому начало и конец всегда
    совпадают с границами предложений из marks.json.

    Кусок расширяется НАЗАД от развязки, а не вперёд: развязка должна
    прозвучать в шортсе целиком и ближе к концу, а перед ней нужен разгон,
    иначе сумма падает на зрителя без контекста и ничего не значит.
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

    # сначала добираем разгон назад
    while span() < min_s and lo > 0:
        lo -= 1
    # если всё равно коротко — тянем вперёд
    while span() < min_s and hi < len(marks) - 1:
        hi += 1
    # если переросло — режем спереди, сохраняя развязку в конце
    while span() > max_s and lo < hi:
        lo += 1
    while span() > max_s and hi > lo:
        hi -= 1

    t0 = max(0.0, marks[lo]["start"] - 0.15)
    t1 = min(total, marks[hi]["end"] + 0.35)
    if t1 - t0 < 8.0:
        return None
    return t0, t1, lo, hi


def pick_windows(beats, marks, total, want=2):
    """
    Два куска, которые не пересекаются и не стоят вплотную.

    Два шортса из соседних абзацев — это один и тот же шортс дважды: то же
    место ролика, тот же материал в кадре, тот же смысл. Поэтому второй
    берётся только если он отстоит от первого.
    """
    out = []
    for _, b in rank_beats(beats):
        w = window_for(b, marks, total)
        if not w:
            continue
        t0, t1 = w[0], w[1]
        if any(not (t1 <= o0 or t0 >= o1) for o0, o1, *_ in out):
            continue        # пересекается с уже взятым
        if any(min(abs(t0 - o1), abs(o0 - t1)) < 12.0 for o0, o1, *_ in out):
            continue        # стоит вплотную
        out.append((t0, t1, w[2], w[3], b))
        if len(out) >= want:
            break
    return out


# ─────────────────────── СУБТИТРЫ ───────────────────────

def words_with_times(marks, lo, hi, t0):
    """
    Слова с тайм-кодами, относительно начала куска.

    Внутри предложения слова раскладываются пропорционально числу символов.
    Границ слов в marks.json нет, а скорость речи внутри одного предложения
    почти постоянна — ошибка выходит в пределах десятых долей секунды, что
    для субтитра длиной в треть секунды несущественно.
    """
    out = []
    for m in marks[lo:hi + 1]:
        text = (m.get("text") or "").strip()
        ws = [w for w in re.split(r"\s+", text) if w]
        if not ws:
            continue
        dur = max(m["end"] - m["start"], 0.05)
        total_chars = sum(len(w) for w in ws) or 1
        cur = m["start"] - t0
        for w in ws:
            share = len(w) / total_chars
            wd = max(WORD_MIN_SEC, dur * share)
            out.append((max(0.0, cur), max(0.0, cur) + wd, w))
            cur += dur * share
    # подрезаем нахлёсты, чтобы два слова не висели разом
    for i in range(len(out) - 1):
        s, e, w = out[i]
        out[i] = (s, min(e, out[i + 1][0]), w)
    return [(s, e, w) for s, e, w in out if e > s]


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def wrap(text: str, per_line: int):
    """Простой перенос по словам. drawtext и ASS сами не переносят."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > per_line:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def build_ass(words, question: str, out: Path):
    """
    Файл субтитров: вопрос в шапке на весь кусок + слова по одному.

    Оба слоя здесь, а не в drawtext, по одной причине: drawtext не умеет
    ни переносить строки, ни центрировать многострочный текст, и каждое
    слово в нём — отдельный фильтр в цепочке. На полусотне слов цепочка
    становится нечитаемой, а на полутора сотнях — ещё и медленной. libass
    берёт то же самое одним файлом.
    """
    box_h = int(H * BOX_SHARE)
    q_lines = wrap(question.strip(), 26) if question else []
    # шрифт вопроса подбирается под число строк, чтобы текст не вылезал
    q_size = 62 if len(q_lines) <= 2 else (52 if len(q_lines) == 3 else 44)
    q_y = BOX_MARGIN + box_h // 2

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Q,{FONT},{q_size},&H00202020,&H00202020,&H00FFFFFF,-1,0,0,0,100,100,0,0,1,0,0,5,40,40,40,1
Style: WORD,{FONT},108,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,6,3,5,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    if q_lines:
        rows.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(99999)},Q,,0,0,0,,"
            f"{{\\pos({W//2},{q_y})}}" + "\\N".join(ass_escape(l) for l in q_lines))
    for s, e, w in words:
        rows.append(
            f"Dialogue: 1,{ass_time(s)},{ass_time(e)},WORD,,0,0,0,,"
            f"{{\\pos({W//2},{int(H*0.72)})}}{ass_escape(w)}")
    out.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return out


# ─────────────────────── РЕНДЕР ───────────────────────

def render_short(src: Path, t0: float, t1: float, ass: Path, dst: Path,
                 crf: int = 20):
    """
    Вырезает кусок, разворачивает в 9:16 и накладывает шапку и субтитры.

    Кадрирование центральное: 1920x1080 -> 608x1080 -> 1080x1920. Апскейл
    в 1.78 раза заметен только на мелкой фактуре, а исходники готовятся на
    холсте 3000 пикселей (PREP_W в render.py) — то есть детали в кадре
    достаточно, теряется она уже при сборке длинного ролика, а не здесь.

    -ss ДО -i, а не после: так ffmpeg перематывает по ключевым кадрам и не
    декодирует всё от начала файла. На получасовом ролике разница — секунды
    против минут.
    """
    box_h = int(H * BOX_SHARE)
    # 1080 * 9/16 = 607.5; libx264 требует чётные размеры, берём 608
    crop_w = 608
    vf = (
        f"crop={crop_w}:1080:(iw-{crop_w})/2:0,"
        f"scale={W}:{H}:flags=lanczos,setsar=1,"
        # белая рамка-шапка: заливка белым, поверх неё вопрос из ASS
        f"drawbox=x={BOX_MARGIN}:y={BOX_MARGIN}:w={W-2*BOX_MARGIN}:h={box_h}:"
        f"color=white@0.92:t=fill,"
        f"drawbox=x={BOX_MARGIN}:y={BOX_MARGIN}:w={W-2*BOX_MARGIN}:h={box_h}:"
        f"color=white:t=4,"
        f"ass={ass.as_posix()}"
    )
    run(["ffmpeg", "-v", "error", "-y",
         "-ss", f"{t0:.3f}", "-t", f"{t1-t0:.3f}", "-i", str(src),
         "-vf", vf, "-r", str(FPS),
         "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         str(dst)])
    return dst


def duration_of(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ─────────────────────── ГЛАВНОЕ ───────────────────────

def main(job_path, want=2):
    job = load_job(job_path)
    work = ROOT / "work" / job["id"]
    assets, out = work / "assets", work / "out"
    final = out / "final.mp4"
    if not final.exists():
        raise SystemExit(f"нет {final} — сначала собери ролик (build.py)")

    marks = json.loads((assets / "marks.json").read_text(encoding="utf-8"))
    total = duration_of(final)
    if not marks or total <= 0:
        raise SystemExit("нет тайм-кодов или пустой ролик")

    question = ((job.get("open_loop") or {}).get("question") or "").strip()
    if not question:
        # Не падаем: шортс без шапки — рабочий шортс. Но молчать нельзя,
        # вопрос в шапке это и есть причина досмотреть.
        log("  ! в спецификации нет open_loop.question — шортсы будут без "
            "шапки с вопросом, а она и держит зрителя")

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

    made = []
    for n, (t0, t1, lo, hi, beat) in enumerate(windows, 1):
        words = words_with_times(marks, lo, hi, t0)
        ass = build_ass(words, question, out / f"short_{n}.ass")
        dst = out / f"short_{n}.mp4"
        log(f"── шортс {n}: {t0:.1f}–{t1:.1f} с ({t1-t0:.1f} с), "
            f"доля «{beat.kind}», слов {len(words)}")
        render_short(final, t0, t1, ass, dst,
                     crf=int((job.get("style_override") or {}).get("crf", 20)))
        got = duration_of(dst)
        if got > 60.5:
            log(f"  ! {got:.1f} с — длиннее 60, YouTube не примет как Shorts")
        log(f"  {dst.name}: {got:.1f} с, {dst.stat().st_size/1048576:.1f} МБ")
        made.append(dst)

    (out / "shorts.json").write_text(json.dumps([
        {"file": p.name, "start": round(w[0], 2), "end": round(w[1], 2),
         "beat": w[4].kind, "question": question}
        for p, w in zip(made, windows)], ensure_ascii=False, indent=1),
        encoding="utf-8")
    log(f"── готово: {len(made)} шортса")
    return made


if __name__ == "__main__":
    main(sys.argv[1], want=int(sys.argv[2]) if len(sys.argv) > 2 else 2)
