#!/usr/bin/env python3
import json
import os
import sys
import time

from PIL import Image


def parse(argv):
    rest = argv[argv.index("--") + 1:]
    options = {}
    i = 0
    while i < len(rest):
        key = rest[i]
        if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            options[key] = rest[i + 1]
            i += 2
        else:
            options[key] = True
            i += 1
    return options


def write_json(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def main():
    argv = sys.argv[1:]
    blend = argv[3]
    options = parse(argv)
    with open(blend, "rb") as handle:
        content = handle.read()
    version = os.path.basename(os.path.dirname(os.path.abspath(sys.argv[0]))).split("-")[1]

    if "--preflight" in options:
        missing = []
        if b"MISSING" in content:
            missing.append({"type": "image", "name": "tex.png", "path": "//tex.png"})
        write_json(options["--preflight"], {
            "blender": version,
            "scene": "Scene",
            "engine": "CYCLES",
            "resolution": [64, 48],
            "resolution_percentage": 100,
            "frame_start": 1,
            "frame_end": 1 if b"STILL" in content else 5,
            "frame_step": 1,
            "frame_current": 1,
            "fps": 24,
            "format": "PNG",
            "output": "/tmp/",
            "samples": 4,
            "device": "GPU",
            "missing_files": missing,
        })
        return 0

    out = options["--out"]
    progress_path = options["--progress"]
    mode = options.get("--mode", "animation")
    if mode == "still":
        frames = [int(options.get("--frame", 1))]
    elif "--frames" in options:
        start, end = (int(part) for part in options["--frames"].split(":"))
        frames = list(range(start, end + 1))
    else:
        frames = list(range(1, 6))
    delay = 0.4 if b"SLOW" in content else 0.03
    os.makedirs(out, exist_ok=True)

    state = {
        "status": "starting",
        "mode": mode,
        "total_frames": len(frames),
        "frames_done": 0,
        "current_frame": frames[0],
        "frame_times": [],
        "updated": time.time(),
    }
    write_json(progress_path, state)

    for frame in frames:
        target = os.path.join(out, f"img_{frame:04d}.png")
        if mode != "still" and os.path.exists(target):
            continue
        began = time.time()
        state.update(status="rendering", current_frame=frame, updated=time.time())
        write_json(progress_path, state)
        for sample in range(1, 5):
            print(
                f"Fra:{frame} Mem:1.00M (Peak 1.00M) | Time:00:0{sample}.00 | Remaining:00:0{4 - sample}.00 "
                f"| Mem:0.00M, Peak:0.00M | Scene, ViewLayer | Sample {sample}/4",
                flush=True,
            )
            time.sleep(delay)
        if b"OOM" in content:
            print("CUDA error: Out of memory", flush=True)
            return 1
        if b"FAIL" in content:
            print("Error: boom", flush=True)
            return 1
        Image.new("RGB", (64, 48), ((frame * 20) % 256, 80, 160)).save(target)
        print(f"Saved: '{target}'", flush=True)
        state["frames_done"] += 1
        state["frame_times"].append(time.time() - began)
        state["updated"] = time.time()
        write_json(progress_path, state)

    state.update(status="done", updated=time.time())
    write_json(progress_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
