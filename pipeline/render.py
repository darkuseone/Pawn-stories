"""
render.py — сборка видео.

ВАЖНО ПРО СКОРОСТЬ. Классический способ делать наезды в FFmpeg — фильтр
zoompan. Он замерен и оказался непригоден: 31 секунда процессорного времени
на 1 секунду готового видео. Для ролика на 38 минут это больше 19 часов,
робот столько не живёт.

Используется другой приём: scale с eval=frame меняет размер холста покадрово,
а crop постоянного размера вырезает из него окно кадра. Тот же наезд и та же
панорама, но 2.6x реального времени вместо 31x. Разница в двенадцать раз.
Проверено замером, не теоретически.
"""

import json
import os
import subprocess
import shlex
from pathlib import Path

from style import MOVES, FRAMINGS, EFFECTS

W, H, FPS = 1920, 1080, 30
# 30 кадров вместо 25: панорамы и наезды заметно глаже. Рендер дороже
# примерно на пятую часть — принято сознательно.

# Рабочий холст, из него режем кадр. Держится в пропорции кадра, поэтому
# запас на движение растёт по обеим осям одинаково. 3000 против 1920 — это
# полуторакратный запас, из него движения в MOVES и берут свою амплитуду.
# Все движения ходят в диапазоне 2180-2720, то есть холст всегда УМЕНЬШАЕТСЯ
# до рабочего размера, а не растягивается: картинка остаётся резкой.
PREP_W = 3000
PREP_H = PREP_W * H // W          # 1687 -> ниже округляется до чётного


def run(cmd, quiet=True):
    return subprocess.run(cmd, shell=True, check=True,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def prepare_image(src: Path, dst: Path, framing: dict):
    """
    Готовит холст под кадр: окно по framing, приведение к рабочему размеру.

    Окно вырезается СРАЗУ в пропорции холста. Раньше вырезался кусок в
    пропорции исходника, а потом он насильно ресайзился в фиксированный
    размер — и вертикальное архивное фото 1024x1365 растягивалось в ширину
    больше чем вдвое. На генерации 16:9 это было незаметно, на реальном
    архиве из библиотек портило именно самый ценный материал.
    """
    from PIL import Image
    im = Image.open(src).convert("RGB")
    iw, ih = im.size
    ph = PREP_H - (PREP_H % 2)
    ar = PREP_W / ph

    # окно нужной пропорции, вписанное в исходник, ужатое на framing.scale
    s = framing["scale"]
    cw = iw / s
    ch = cw / ar
    if ch > ih / s:                       # не помещается по высоте — ведём от неё
        ch = ih / s
        cw = ch * ar
    cw, ch = max(1, int(cw)), max(1, int(ch))

    cx = int((iw - cw) * framing["cx"])
    cy = int((ih - ch) * framing["cy"])
    im = im.crop((cx, cy, cx + cw, cy + ch)).resize((PREP_W, ph), Image.LANCZOS)
    im.save(dst, quality=95)


def motion_filter(move: str, speed: float, dur: float) -> str:
    """Строит цепочку фильтров для одного движения камеры."""
    m = MOVES[move]
    w0, w1 = m["w0"], m["w1"]
    w1 = w0 + (w1 - w0) * speed          # скорость меняет амплитуду
    x0, x1 = m["x0"], m["x1"]
    y0, y1 = m["y0"], m["y1"]

    # p — прогресс 0..1, сглаженный (ease-in-out), чтобы движение
    # начиналось и заканчивалось плавно, а не рывком
    p = f"(3*pow(min(t/{dur:.3f},1),2)-2*pow(min(t/{dur:.3f},1),3))"

    wexpr = f"trunc(({w0:.1f}+({w1 - w0:.1f})*{p})/2)*2"
    xexpr = f"(iw-{W})*({x0:.3f}+({x1 - x0:.3f})*{p})"
    yexpr = f"(ih-{H})*({y0:.3f}+({y1 - y0:.3f})*{p})"

    return (f"scale=w='{wexpr}':h=-2:eval=frame,"
            f"crop={W}:{H}:x='{xexpr}':y='{yexpr}',setsar=1")


def effect_filter(name) -> str:
    """
    Цепочка эффекта по имени. Неизвестное имя — не молчаливый пропуск:
    опечатка в спецификации должна быть видна сразу, а не через сорок минут
    рендера «почему-то без эффектов».
    """
    if not name:
        return ""
    if name not in EFFECTS:
        raise SystemExit(f"неизвестный эффект {name!r}; есть: "
                         + ", ".join(sorted(EFFECTS)))
    return EFFECTS[name]


def with_effect(vf: str, effect) -> str:
    """Эффект встаёт ПОСЛЕ движения — он красит уже готовый кадр 1920x1080."""
    fx = effect_filter(effect)
    return f"{vf},{fx}" if fx else vf


def render_clip(image: Path, out: Path, move: str, speed: float, dur: float,
                effect=None):
    vf = with_effect(motion_filter(move, speed, dur), effect)
    cmd = (f"ffmpeg -y -loop 1 -t {dur:.3f} -r {FPS} -i {shlex.quote(str(image))} "
           f"-vf {shlex.quote(vf)} -c:v libx264 -crf 18 -preset veryfast "
           f"-pix_fmt yuv420p -an {shlex.quote(str(out))}")
    run(cmd)


def render_footage_clip(src: Path, out: Path, dur: float, start: float = 0.0,
                        effect=None):
    """
    Стоковый футаж: обрезка, приведение к 1920x1080/25fps, без звука.

    stream_loop обязателен. Клипы со стоков часто короче кадра (10-15 секунд
    против кадра до 22), и без петли на выходе получается файл короче
    заказанного. Дальше xfade просит кадры за его концом и обрывает всю
    группу склейки без единой ошибки в логе.
    """
    vf = with_effect(f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},fps={FPS},setsar=1", effect)
    cmd = (f"ffmpeg -y -stream_loop -1 -ss {start:.2f} -i {shlex.quote(str(src))} "
           f"-vf {shlex.quote(vf)} -t {dur:.3f} "
           f"-c:v libx264 -crf 18 -preset veryfast "
           f"-pix_fmt yuv420p -an {shlex.quote(str(out))}")
    run(cmd)


def concat_segments(segments, out: Path):
    """Финальная склейка без перекодирования — быстро и без потерь."""
    lst = out.parent / "concat.txt"
    lst.write_text("".join(f"file '{Path(s).resolve()}'\n" for s in segments))
    run(f"ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(lst))} "
        f"-c copy {shlex.quote(str(out))}")


def build_audio(voice: Path, bed, out: Path, total: float,
                bed_gain_db: float = -26.0):
    """
    Голос + фоновая подложка. bed=None — только голос.

    Подложка зацикливается на весь ролик, независимо от своей длины.
    Заход и уход сглажены, иначе на старте и в финале слышен обрыв.

    loudnorm приводит всё к -16 LUFS, alimiter срезает пики. Это не
    украшение: один скачок громкости на тридцатой минуте выбивает зрителя
    из ролика, и он уходит.

    Нормализация нужна и без подложки, поэтому голос в одиночку идёт по той
    же цепочке, а не копируется как есть.
    """
    norm = "loudnorm=I=-16:TP=-1.5:LRA=9,alimiter=limit=0.92"
    # Дорожка пишется СТЕРЕО и в 48 кГц. Замер готового ролика показал моно
    # на 96 кГц: начитка приходит от ElevenLabs моно, amix берёт раскладку по
    # первому входу — и подложка, записанная в стерео, схлопывалась в моно,
    # теряя всю ширину. Частота бралась максимальная из входов, отсюда и
    # ненужные 96 кГц. 48 кГц стерео — то, во что YouTube всё равно
    # перекодирует, и лишний раз портить исходник незачем.
    fmt = "-ar 48000 -ac 2 -c:a aac -b:a 192k"

    if bed is None:
        run(f"ffmpeg -y -i {shlex.quote(str(voice))} -af {shlex.quote(norm)} "
            f"{fmt} {shlex.quote(str(out))}")
        return

    # У короткого ролика хвост ухода не помещается: st не может быть
    # отрицательным, иначе afade молча не сработает вовсе.
    fade_out = max(0.5, min(6.0, total / 4))
    fade_in = max(0.3, min(3.0, total / 6))
    st_out = max(0.0, total - fade_out)

    tmp = out.parent / "bed_loop.m4a"
    run(f"ffmpeg -y -stream_loop -1 -i {shlex.quote(str(bed))} "
        f"-t {total:.2f} -af 'afade=t=in:d={fade_in:.2f},"
        f"afade=t=out:st={st_out:.2f}:d={fade_out:.2f}' "
        f"-c:a aac -b:a 160k {shlex.quote(str(tmp))}")

    # Оба входа приводятся к стерео ДО amix: иначе он берёт раскладку по
    # первому входу, а первый — моно-начитка, и подложка теряет ширину.
    filt = (f"[1:a]volume={bed_gain_db}dB,"
            f"aformat=channel_layouts=stereo:sample_rates=48000[bed];"
            f"[0:a]aformat=channel_layouts=stereo:sample_rates=48000[voc];"
            f"[voc][bed]amix=inputs=2:duration=first:dropout_transition=0,"
            f"{norm}[a]")
    run(f"ffmpeg -y -i {shlex.quote(str(voice))} -i {shlex.quote(str(tmp))} "
        f"-filter_complex {shlex.quote(filt)} -map [a] "
        f"{fmt} {shlex.quote(str(out))}")


def mux(video: Path, audio: Path, out: Path):
    run(f"ffmpeg -y -i {shlex.quote(str(video))} -i {shlex.quote(str(audio))} "
        f"-c:v copy -c:a copy -shortest {shlex.quote(str(out))}")


def duration_of(path: Path, stream: str = "v") -> float:
    """Длина дорожки в секундах. Нужна для проверки сведения замером."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream,
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip().rstrip(","))
    except ValueError:
        return 0.0


def write_srt(segments, out: Path):
    """Субтитры из тайм-кодов ElevenLabs. Распознавание речи не нужно."""
    def ts(sec):
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}\n{ts(seg['start'])} --> {ts(seg['end'])}\n{seg['text']}\n")
    out.write_text("\n".join(lines), encoding="utf-8")
