import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import blendver

WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_wrapper.py")
SAMPLE_RE = re.compile(r"Sample (\d+)/(\d+)")
REMAINING_RE = re.compile(r"Remaining:([\d:.]+)")


def parse_clock(text):
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def format_clock(seconds):
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def load_progress(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def estimate(progress, sample, remaining_current):
    total = progress.get("total_frames", 1)
    done = progress.get("frames_done", 0)
    times = progress.get("frame_times", [])
    frames_left = max(total - done - 1, 0)
    if times:
        average = sum(times) / len(times)
    elif sample and sample[0] and remaining_current is not None:
        fraction = sample[0] / sample[1]
        average = remaining_current * fraction / max(1 - fraction, 1e-6) + remaining_current
    else:
        average = None
    current_left = remaining_current if remaining_current is not None else average
    if average is None or current_left is None:
        return None
    return current_left + frames_left * average


def status_line(progress, sample, remaining, started):
    total = progress.get("total_frames", 1)
    index = min(progress.get("frames_done", 0) + 1, total)
    parts = [f"кадр {index}/{total}"]
    if sample:
        parts.append(f"сэмпл {sample[0]}/{sample[1]}")
    eta = estimate(progress, sample, remaining)
    parts.append("осталось ~" + format_clock(eta) if eta is not None else "осталось ?")
    parts.append("прошло " + format_clock(time.time() - started))
    return " | ".join(parts)


def run(cmd, log_path, progress_path):
    started = time.time()
    last_print = 0.0
    sample = None
    remaining = None
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                match = SAMPLE_RE.search(line)
                if match:
                    sample = (int(match.group(1)), int(match.group(2)))
                    rem = REMAINING_RE.search(line)
                    try:
                        remaining = parse_clock(rem.group(1)) if rem else None
                    except ValueError:
                        remaining = None
                now = time.time()
                if now - last_print >= 1.0:
                    last_print = now
                    progress = load_progress(progress_path)
                    if progress:
                        print("\r" + status_line(progress, sample, remaining, started) + " " * 8, end="", flush=True)
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            print("\nпрервано")
            return 130
    code = proc.wait()
    print()
    return code


def build_command(args, blend, wrapper_args):
    cmd = []
    if args.xvfb:
        cmd += ["xvfb-run", "-a"]
    cmd += [args.blender, "-b", "--disable-autoexec", "-noaudio", blend, "-P", WRAPPER, "--"]
    cmd += wrapper_args
    return cmd


def list_outputs(out_dir):
    if not os.path.isdir(out_dir):
        return []
    result = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            result.append((name, os.path.getsize(path)))
    return result


def do_preflight(args, blend):
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "preflight.json")
        cmd = build_command(args, blend, ["--preflight", target, "--addons", args.addons])
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        if not os.path.exists(target):
            print(result.stdout[-4000:])
            print("preflight не удался, код", result.returncode)
            return 1
        with open(target, "r", encoding="utf-8") as f:
            info = json.load(f)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("missing_files"):
        print(f"\nНе найдено файлов: {len(info['missing_files'])}")
        return 2
    return 0


def do_render(args, blend):
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(os.path.dirname(blend), "renders")
    work_dir = os.path.dirname(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    progress_path = os.path.join(work_dir, "progress.json")
    log_path = os.path.join(work_dir, "render.log")
    if os.path.exists(progress_path):
        os.remove(progress_path)
    wrapper_args = [
        "--mode", args.mode,
        "--device", args.device,
        "--out", out_dir,
        "--progress", progress_path,
        "--addons", args.addons,
    ]
    if args.frame is not None:
        wrapper_args += ["--frame", str(args.frame)]
    if args.frames:
        wrapper_args += ["--frames", args.frames]
    cmd = build_command(args, blend, wrapper_args)
    print("запуск:", " ".join(cmd))
    started = time.time()
    code = run(cmd, log_path, progress_path)
    print(f"код выхода: {code}, время: {format_clock(time.time() - started)}")
    print(f"лог: {log_path}")
    outputs = list_outputs(out_dir)
    print(f"файлов в {out_dir}: {len(outputs)}")
    for name, size in outputs[:20]:
        print(f"  {name}  {size / 1024 / 1024:.2f} МБ")
    if len(outputs) > 20:
        print(f"  ... и ещё {len(outputs) - 20}")
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("blend")
    parser.add_argument("--blender", required=True)
    parser.add_argument("--mode", choices=["still", "animation"], default="animation")
    parser.add_argument("--device", choices=["OPTIX", "CUDA", "CPU"], default="OPTIX")
    parser.add_argument("--out")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--xvfb", action="store_true")
    parser.add_argument("--addons", default="")
    parser.add_argument("--frame", type=int)
    parser.add_argument("--frames")
    args = parser.parse_args()
    if args.frames and not re.fullmatch(r"\d+:\d+", args.frames):
        parser.error("--frames ожидает формат НАЧАЛО:КОНЕЦ, например 1:3")

    blend = os.path.abspath(args.blend)
    if not os.path.isfile(blend):
        print("файл не найден:", blend)
        return 1
    if not os.path.isfile(args.blender):
        print("исполняемый файл Blender не найден:", args.blender)
        return 1

    version = blendver.read_version(blend)
    print("версия, в которой сохранён проект:", blendver.series_label(version))

    if args.preflight:
        return do_preflight(args, blend)
    return do_render(args, blend)


if __name__ == "__main__":
    sys.exit(main())