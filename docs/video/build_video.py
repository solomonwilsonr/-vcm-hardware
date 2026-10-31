#!/usr/bin/env python3
"""Build the 60-second demo video: real demo output + Kokoro voice-over.

Needs (not project deps): kokoro-onnx, soundfile, Pillow, ffmpeg, and the Kokoro model files.
Usage: build_video.py MODEL.onnx VOICES.bin OUT.mp4 [WORKDIR]
Narration text comes from NARRATION below and mirrors docs/demo-script.md.
"""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
MODEL, VOICES, OUT = sys.argv[1], sys.argv[2], Path(sys.argv[3])
WORK = Path(sys.argv[4]) if len(sys.argv) > 4 else OUT.parent / "build"
WORK.mkdir(parents=True, exist_ok=True)

W, H, FPS = 1280, 720, 12
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 22)
BOLD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 22)
CAP = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
BIG = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 54)
MID = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
CW, LH, COLS = 13, 28, 93
TOP, BOTTOM = 50, 128
ROWS = (H - TOP - BOTTOM) // LH
BG, FG = (13, 17, 23), (201, 209, 217)
COL = {"crit": (255, 123, 114), "warn": (227, 179, 65), "ok": (63, 185, 80),
       "head": (121, 192, 255), "dim": (125, 133, 144)}

# (title, command shown, narration)
BEATS = [
    ("Two committed configurations", "vcm log",
     "Hardware changes live in spreadsheets. A GPU swap looks like one line, "
     "but it touches power, cooling and firmware."),
    ("What changed?", "vcm diff v1.0 v1.1",
     "Version Control for Hardware diffs the node inventory, not bytes. "
     "Eight GPUs, one grouped finding."),
    ("What else is affected?", "vcm impact v1.0 v1.1",
     "Then it asks what else breaks. Every finding is labelled: verified from data, "
     "rule based from engineering rules, or speculative. This drop-in upgrade is short "
     "by one hundred ninety watts, and the driver is too old."),
    ("Which physical units?", 'vcm device affected --component GPU0 --value "NVIDIA A100 PCIe 40GB"',
     "Releases are hash pinned, and every physical node is registered against one. "
     "So the question becomes: which units are affected?"),
    ("Tamper-evident releases", "vcm release verify R1.1",
     "Corrupt one byte, and verification fails. Local first. No network. Apache two."),
]


def run_demo() -> list[list[str]]:
    """Run examples/demo-60s.sh and split its output on the beat markers."""
    ws = WORK / "ws"
    env = {"VCM_DEMO_DIR": str(ws), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    out = subprocess.run(["bash", str(ROOT / "examples/demo-60s.sh")], capture_output=True,
                         text=True, env=env, check=True).stdout
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    parts = re.split(r"\n=== .*? ===\n", "\n" + out)[1:]
    assert len(parts) == len(BEATS), len(parts)
    return [p.strip("\n").splitlines() for p in parts]


def trim(i: int, lines: list[str]) -> list[str]:
    if i == 2:  # keep what the narration talks about
        keep = [ln for ln in lines if ln.startswith("## ") or "R001" in ln or "R006" in ln
                or "Shortfall" in ln or "requires at least" in ln]
        return keep[:14]
    if i == 4:
        return lines
    return lines[:22]


def wrap(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        ln = ln.replace("**", "")
        out += textwrap.wrap(ln, COLS, subsequent_indent="    ", replace_whitespace=False) or [""]
    return out


def color(ln: str):
    if "[critical]" in ln or "FAIL" in ln:
        return COL["crit"]
    if "[warning]" in ln:
        return COL["warn"]
    if ln.startswith("## ") or "release R1.1: OK" in ln:
        return COL["ok"] if "OK" in ln else COL["head"]
    if "evidence:" in ln or "derived from" in ln:
        return COL["dim"]
    return FG


def synth(kokoro) -> list[tuple[Path, float]]:
    res = []
    for i, (_, _, text) in enumerate(BEATS):
        samples, sr = kokoro.create(text, voice="af_heart", speed=1.02, lang="en-us")
        p = WORK / f"vo{i}.wav"
        sf.write(p, samples, sr)
        res.append((p, len(samples) / sr))
    return res


def frame(title: str, cmd: str, shown: list[str], typed: float, idx: int, caption: str):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 34], fill=(22, 27, 34))
    for k, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([14 + k * 22, 11, 26 + k * 22, 23], fill=c)
    d.text((W // 2 - 150, 8), "vcm  -  gpu-node-01 upgrade", font=FONT, fill=COL["dim"])
    y = TOP
    prompt = "$ " + cmd[: int(len(cmd) * typed)]
    body = wrap([prompt] + shown)
    for ln in body[-ROWS:]:
        f = BOLD if ln.startswith("$ ") else FONT
        d.text((16, y), ln, font=f, fill=COL["ok"] if ln.startswith("$ ") else color(ln))
        y += LH
    d.rectangle([0, H - BOTTOM, W, H], fill=(22, 27, 34))
    d.text((20, H - BOTTOM + 8), f"{idx + 1}/5  {title}", font=BOLD, fill=COL["head"])
    for k, ln in enumerate(textwrap.wrap(caption, 105)[:3]):
        d.text((20, H - BOTTOM + 40 + k * 26), ln, font=CAP, fill=FG)
    return im


def title_card(sub: str) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((W // 2, 280), "VCM", font=BIG, fill=COL["head"], anchor="mm")
    d.text((W // 2, 350), "Version Control for Hardware", font=MID, fill=FG, anchor="mm")
    d.text((W // 2, 410), sub, font=FONT, fill=COL["dim"], anchor="mm")
    return im


def main() -> None:
    from kokoro_onnx import Kokoro
    outputs = run_demo()
    vo = synth(Kokoro(MODEL, VOICES))
    frames_dir = WORK / "frames"
    frames_dir.mkdir(exist_ok=True)
    for f in frames_dir.glob("*.png"):
        f.unlink()
    n = 0
    audio, timeline = [], 0.0

    def emit(im, secs):
        nonlocal n
        for _ in range(max(1, round(secs * FPS))):
            im.save(frames_dir / f"f{n:05d}.png")
            n += 1

    emit(title_card("one GPU swap. what breaks? which racks?"), 1.5)
    audio.append(np.zeros(int(24000 * 1.5)))
    for i, (title, cmd, text) in enumerate(BEATS):
        lines = wrap(trim(i, outputs[i]))
        samples, _ = sf.read(vo[i][0])
        dur = max(vo[i][1] + 0.5, 4.0)
        t_type = 1.2
        total = int(dur * FPS)
        for k in range(total):
            t = k / FPS
            typed = min(1.0, t / t_type)
            reveal = 0 if t < t_type else (t - t_type) / max(0.1, dur - t_type - 1.2)
            shown = lines[: int(len(lines) * min(1.0, reveal)) + (1 if reveal > 0 else 0)]
            frame(title, cmd, shown if t >= t_type else [], typed, i, text).save(
                frames_dir / f"f{n:05d}.png")
            n += 1
        pad = np.zeros(int(24000 * dur) - len(samples))
        audio.append(np.concatenate([samples, pad]))
        timeline += dur
    emit(title_card("github.com/solomonwilsonr/-vcm-hardware"), 2.0)
    audio.append(np.zeros(int(24000 * 2.0)))
    sf.write(WORK / "voice.wav", np.concatenate(audio), 24000)
    total_s = n / FPS
    # quiet ambient pad under the voice
    music = (f"sine=f=110:d={total_s},volume=0.5[a];sine=f=165:d={total_s},volume=0.35[b];"
             f"sine=f=220:d={total_s},volume=0.2[c];[a][b][c]amix=inputs=3,lowpass=f=600,"
             f"tremolo=f=0.2:d=0.4,volume=0.12,afade=t=in:d=2,afade=t=out:st={total_s-2}:d=2[m]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i",
           str(frames_dir / "f%05d.png"), "-i", str(WORK / "voice.wav"),
           "-filter_complex", music + ";[1:a]volume=1.0[v];[v][m]amix=inputs=2:duration=first:"
           "normalize=0[a]", "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt",
           "yuv420p", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-shortest", str(OUT)]
    subprocess.run(cmd, check=True)
    print(f"wrote {OUT}  ({total_s:.1f}s)")


if __name__ == "__main__":
    main()
