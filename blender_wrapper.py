import argparse
import json
import os
import sys
import time
import traceback

import bpy

VIDEO_FORMATS = {"FFMPEG", "AVI_JPEG", "AVI_RAW"}


def say(message):
    print(f"[wrapper] {message}", flush=True)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["still", "animation"], default="animation")
    parser.add_argument("--device", choices=["OPTIX", "CUDA", "CPU"], default="OPTIX")
    parser.add_argument("--out")
    parser.add_argument("--progress")
    parser.add_argument("--preflight")
    parser.add_argument("--addons", default="")
    return parser.parse_args(argv)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def enable_addons(names):
    if not names:
        return
    import addon_utils
    for name in names:
        try:
            module = addon_utils.enable(name, default_set=False, persistent=False)
        except Exception as exc:
            say(f"аддон не включился: {name}: {exc}")
            continue
        if module is None:
            say(f"аддон не включился: {name}")
        else:
            say(f"аддон включён: {name}")


def scene_info(scene):
    render = scene.render
    info = {
        "blender": bpy.app.version_string,
        "scene": scene.name,
        "engine": render.engine,
        "resolution": [render.resolution_x, render.resolution_y],
        "resolution_percentage": render.resolution_percentage,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_step": scene.frame_step,
        "frame_current": scene.frame_current,
        "fps": render.fps,
        "format": render.image_settings.file_format,
        "output": render.filepath,
    }
    if render.engine == "CYCLES" and hasattr(scene, "cycles"):
        info["samples"] = scene.cycles.samples
        info["device"] = scene.cycles.device
    elif hasattr(scene, "eevee"):
        info["samples"] = getattr(scene.eevee, "taa_render_samples", None)
    return info


def collect_missing():
    missing = []
    for image in bpy.data.images:
        if image.source not in {"FILE", "SEQUENCE", "MOVIE"}:
            continue
        if image.packed_file or not image.filepath:
            continue
        path = bpy.path.abspath(image.filepath, library=image.library)
        if not os.path.exists(path):
            missing.append({"type": "image", "name": image.name, "path": image.filepath})
    for library in bpy.data.libraries:
        if not os.path.exists(bpy.path.abspath(library.filepath)):
            missing.append({"type": "library", "name": library.name, "path": library.filepath})
    for sound in bpy.data.sounds:
        if sound.packed_file or not sound.filepath:
            continue
        if not os.path.exists(bpy.path.abspath(sound.filepath)):
            missing.append({"type": "sound", "name": sound.name, "path": sound.filepath})
    for clip in bpy.data.movieclips:
        if not os.path.exists(bpy.path.abspath(clip.filepath)):
            missing.append({"type": "movieclip", "name": clip.name, "path": clip.filepath})
    for cache in bpy.data.cache_files:
        if not os.path.exists(bpy.path.abspath(cache.filepath)):
            missing.append({"type": "cache", "name": cache.name, "path": cache.filepath})
    return missing


def refresh_devices(prefs):
    for name in ("refresh_devices", "get_devices"):
        function = getattr(prefs, name, None)
        if function is None:
            continue
        try:
            function()
        except Exception:
            pass


def setup_device(scene, requested):
    if scene.render.engine != "CYCLES":
        return "не Cycles, устройство не меняется"
    if requested == "CPU":
        scene.cycles.device = "CPU"
        return "CPU"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    order = [requested] + [kind for kind in ("OPTIX", "CUDA") if kind != requested]
    for kind in order:
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        refresh_devices(prefs)
        gpus = [device for device in prefs.devices if device.type == kind]
        if not gpus:
            continue
        for device in prefs.devices:
            device.use = device.type == kind
        scene.cycles.device = "GPU"
        return f"{kind}: " + ", ".join(device.name for device in gpus)
    scene.cycles.device = "CPU"
    return "GPU не найден, откат на CPU"


def configure_output(scene, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    original = scene.render.filepath.replace("\\", "/")
    prefix = original.rsplit("/", 1)[-1]
    scene.render.filepath = os.path.join(out_dir, prefix)


class Progress:
    def __init__(self, path, scene, mode):
        self.path = path
        self.frame_started = None
        if mode == "still":
            total = 1
        else:
            total = len(range(scene.frame_start, scene.frame_end + 1, max(scene.frame_step, 1)))
        self.state = {
            "status": "starting",
            "mode": mode,
            "total_frames": total,
            "frames_done": 0,
            "current_frame": scene.frame_current,
            "frame_times": [],
            "updated": time.time(),
        }
        self.write()

    def write(self):
        if not self.path:
            return
        self.state["updated"] = time.time()
        write_json(self.path, self.state)

    def on_pre(self, scene, *args):
        self.frame_started = time.time()
        self.state["status"] = "rendering"
        self.state["current_frame"] = scene.frame_current
        self.write()

    def on_post(self, scene, *args):
        if self.frame_started is not None:
            times = self.state["frame_times"]
            times.append(time.time() - self.frame_started)
            self.state["frame_times"] = times[-20:]
        self.state["frames_done"] += 1
        self.write()

    def on_complete(self, scene, *args):
        self.state["status"] = "done"
        self.write()

    def on_cancel(self, scene, *args):
        self.state["status"] = "cancelled"
        self.write()


def main():
    args = parse_args()
    scene = bpy.context.scene
    say(f"Blender {bpy.app.version_string}, сцена {scene.name}, движок {scene.render.engine}")
    enable_addons([name for name in args.addons.split(",") if name])

    if args.preflight:
        info = scene_info(scene)
        info["missing_files"] = collect_missing()
        write_json(args.preflight, info)
        say("preflight готов")
        return

    say(f"устройство: {setup_device(scene, args.device)}")
    if args.out:
        configure_output(scene, args.out)

    progress = Progress(args.progress, scene, args.mode)
    handlers = bpy.app.handlers
    handlers.render_pre.append(progress.on_pre)
    handlers.render_post.append(progress.on_post)
    handlers.render_complete.append(progress.on_complete)
    handlers.render_cancel.append(progress.on_cancel)

    if args.mode == "still":
        if scene.render.image_settings.file_format in VIDEO_FORMATS:
            scene.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(write_still=True)
    else:
        scene.render.use_overwrite = False
        bpy.ops.render.render(animation=True)


try:
    main()
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
    sys.exit(1)
