import json
import time

from . import storage

VIDEO_FORMATS = {"FFMPEG", "AVI_JPEG", "AVI_RAW"}


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def expected_total(info, options):
    if options.get("mode") == "still":
        return 1
    frames = options.get("frames")
    if frames:
        start, end = frames
    else:
        start, end = info.get("frame_start", 1), info.get("frame_end", 1)
    step = max(info.get("frame_step", 1), 1)
    return max(len(range(start, end + 1, step)), 1)


def estimate(total, done, frame_times, sample, remaining):
    if done >= total:
        return 0.0
    frames_left = max(total - done - 1, 0)
    average = sum(frame_times) / len(frame_times) if frame_times else None
    fraction = sample[0] / sample[1] if sample and sample[1] else None
    if average is None and remaining is not None and fraction is not None and fraction < 1:
        average = remaining / (1 - fraction)
    current_left = remaining
    if current_left is None and average is not None:
        current_left = average * (1 - (fraction or 0))
    if current_left is None:
        return None
    if frames_left == 0:
        return current_left
    if average is None:
        return None
    return current_left + frames_left * average


def build(job, paths, live, now=None):
    now = now or time.time()
    info = job.get("info") or {}
    options = job.get("options") or {}
    status = job["status"]
    raw = read_json(paths.progress) or {}
    total = raw.get("total_frames") or expected_total(info, options)
    done = raw.get("frames_done", 0)
    if options.get("mode") == "animation" and info.get("format") not in VIDEO_FORMATS:
        done = max(done, storage.count_images(paths.renders))
    done = min(done, total)

    sample = None
    remaining = None
    if live and status == "rendering" and live["time"] >= raw.get("updated", 0):
        sample = live["sample"]
        remaining = live["remaining"]

    eta = None
    if status == "done":
        percent = 1.0
        eta = 0.0
    elif status == "rendering":
        fraction = sample[0] / sample[1] if sample and sample[1] else 0
        percent = 1.0 if done >= total else min((done + fraction) / total, 1.0)
        eta = estimate(total, done, raw.get("frame_times", []), sample, remaining)
    else:
        percent = done / total if total else 0.0

    started = job.get("started")
    finished = job.get("finished")
    elapsed = None
    if started and status == "rendering":
        elapsed = now - started
    elif started and finished and status in ("done", "failed", "cancelled"):
        elapsed = finished - started

    times = raw.get("frame_times", [])
    return {
        "frames_done": done,
        "total_frames": total,
        "current_frame": raw.get("current_frame"),
        "sample": sample[0] if sample else None,
        "samples_total": sample[1] if sample else None,
        "percent": round(percent, 4),
        "eta_seconds": None if eta is None else round(eta, 1),
        "elapsed_seconds": None if elapsed is None else round(elapsed, 1),
        "avg_frame_seconds": round(sum(times) / len(times), 1) if times else None,
    }
