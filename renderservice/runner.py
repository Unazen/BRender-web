import os
import re
import shutil

SAMPLE_RE = re.compile(r"Sample (\d+)/(\d+)")
REMAINING_RE = re.compile(r"Remaining:([\d:.]+)")


def parse_clock(text):
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def parse_line(line):
    sample = None
    remaining = None
    match = SAMPLE_RE.search(line)
    if match:
        sample = (int(match.group(1)), int(match.group(2)))
        found = REMAINING_RE.search(line)
        if found:
            try:
                remaining = parse_clock(found.group(1))
            except ValueError:
                remaining = None
    return sample, remaining


def use_xvfb(settings, engine):
    if settings.xvfb == "true":
        return True
    if settings.xvfb == "false":
        return False
    return engine != "CYCLES" and not os.environ.get("DISPLAY") and shutil.which("xvfb-run") is not None


def build_command(blender, blend, wrapper, wrapper_args, xvfb):
    command = ["xvfb-run", "-a"] if xvfb else []
    command += [str(blender), "-b", "--disable-autoexec", "-noaudio", str(blend), "-P", str(wrapper), "--"]
    return command + list(wrapper_args)


def classify_failure(code, tail):
    text = "\n".join(tail).lower()
    if "out of memory" in text:
        return "Не хватило памяти (видеопамяти или оперативной)"
    if code == -9:
        return "Процесс убит системой, вероятно из-за нехватки оперативной памяти"
    if code in (-11, -6):
        return "Blender аварийно завершился"
    if "traceback" in text:
        return "Ошибка в скрипте-обёртке, подробности в логе"
    return f"Blender завершился с кодом {code}"
