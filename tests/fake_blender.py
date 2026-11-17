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


def user_dirs():
    return (
        os.environ.get("BLENDER_USER_CONFIG", ""),
        os.environ.get("BLENDER_USER_SCRIPTS", ""),
        os.environ.get("BLENDER_USER_EXTENSIONS", ""),
    )


def scan(target):
    config, scripts, extensions = user_dirs()
    enabled = []
    prefs = os.path.join(config, "userpref.blend")
    if os.path.isfile(prefs):
        with open(prefs, "rb") as handle:
            for line in handle.read().decode("utf-8", "replace").splitlines():
                if line.startswith("ADDON:"):
                    enabled.append(line[6:].strip())
    modules = [
        {"module": name, "name": name.replace("_", " ").title(), "version": "1.0.0", "path": f"/fake/blender/scripts/addons/{name}"}
        for name in ("cycles", "node_wrangler")
    ]
    addons = os.path.join(scripts, "addons")
    if os.path.isdir(addons):
        for entry in sorted(os.listdir(addons)):
            path = os.path.join(addons, entry)
            if entry.endswith(".py"):
                modules.append({"module": entry[:-3], "name": entry[:-3], "version": "", "path": path})
            elif os.path.isfile(os.path.join(path, "__init__.py")):
                modules.append({"module": entry, "name": entry, "version": "", "path": os.path.join(path, "__init__.py")})
    if os.path.isdir(extensions):
        for repo in sorted(os.listdir(extensions)):
            repo_path = os.path.join(extensions, repo)
            if not os.path.isdir(repo_path):
                continue
            for entry in sorted(os.listdir(repo_path)):
                path = os.path.join(repo_path, entry)
                if os.path.isdir(path):
                    modules.append({"module": f"bl_ext.{repo}.{entry}", "name": entry, "version": "2.0", "path": path})
    write_json(target, {"blender": "fake", "enabled": enabled, "modules": modules})
    print("[wrapper] scan done", flush=True)
    return 0


def command(argv):
    rest = argv[argv.index("extension") + 1:]
    if rest[0] == "sync":
        return 0
    ext_id = rest[-1]
    if "nonexistent" in ext_id:
        print("Error: extension not found: " + ext_id, flush=True)
        return 1
    _, _, extensions = user_dirs()
    folder = os.path.join(extensions, "blender_org", ext_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write("")
    return 0


def main():
    argv = sys.argv[1:]
    if "--command" in argv:
        return command(argv)
    options = parse(argv)
    if "--scan" in options:
        return scan(options["--scan"])
    blend = argv[3]
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
            "addons_failed": [name for name in str(options.get("--addons", "")).split(",") if "broken" in name],
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
    config, _, _ = user_dirs()
    print(f"[fake] config={config} addons={options.get('--addons', '')} disabled={options.get('--disable-addons', '')}", flush=True)
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
