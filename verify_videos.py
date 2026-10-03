#!/usr/bin/env python3
"""Check downloaded video metadata and extract three review frames per shot."""

import argparse
import html
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    args = parser.parse_args()
    config = json.loads((ROOT / "scenes.json").read_text(encoding="utf-8"))
    review = args.output / "review"
    review.mkdir(parents=True, exist_ok=True)
    items = []
    for scene in config["scenes"]:
        path = args.output / (scene["id"] + ".mp4")
        if not path.exists():
            continue
        cached = review / (scene["id"] + ".json")
        if cached.exists():
            item = json.loads(cached.read_text(encoding="utf-8"))
        else:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                check=True, capture_output=True, text=True,
            )
            metadata = json.loads(result.stdout)
            video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
            duration = float(video.get("duration") or metadata["format"]["duration"])
            valid = video["width"] == 1920 and video["height"] == 1080 and abs(duration - scene["duration"]) <= 0.15
            item = {
                "id": scene["id"], "title": scene["title"], "expected_duration": scene["duration"],
                "actual_duration": duration, "width": video["width"], "height": video["height"],
                "codec": video["codec_name"], "valid": valid,
                "frames": [],
            }
            for index, moment in enumerate((0.25, duration / 2, max(0.25, duration - 0.3)), start=1):
                frame = review / f"{scene['id']}_{index}.jpg"
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(moment),
                     "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "2", str(frame)],
                    check=True,
                )
                item["frames"].append(frame.name)
            cached.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        items.append(item)
        strip_path = review / (item["id"] + "_strip.jpg")
        if not strip_path.exists():
            canvas = Image.new("RGB", (1920, 396), "#222222")
            ImageDraw.Draw(canvas).text((12, 10), item["id"], fill="white")
            for index, filename in enumerate(item["frames"]):
                with Image.open(review / filename) as frame:
                    canvas.paste(frame, (index * 640, 36))
            canvas.save(strip_path, quality=92)
    report = {"checked": len(items), "expected": len(config["scenes"]), "videos": items}
    (review / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cards = []
    for item in items:
        frames = "".join(f'<img src="{html.escape(frame)}" alt="Klatka kontrolna">' for frame in item["frames"])
        cards.append(
            f'<section><h2>{html.escape(item["title"])}</h2>'
            f'<p>{item["width"]}×{item["height"]} · {item["actual_duration"]:.2f} s · '
            f'{"parametry OK" if item["valid"] else "sprawdź parametry"}</p><div class="frames">{frames}</div>'
            f'<video controls preload="metadata" src="../{item["id"]}.mp4"></video></section>'
        )
    page = (
        '<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Smart City — podgląd wygenerowanych ujęć</title><style>'
        'body{font:16px system-ui;background:#111;color:#eee;max-width:1200px;margin:40px auto;padding:0 20px}'
        'section{margin:32px 0;padding:24px;background:#202020;border-radius:12px}h2{font-size:20px}'
        '.frames{display:flex;gap:8px}.frames img{width:calc((100% - 16px)/3);height:auto;object-fit:contain}'
        'video{width:100%;max-height:600px;margin-top:16px}p{color:#aaa}</style>'
        f'<h1>Smart City — {len(items)}/{len(config["scenes"])} ujęć</h1>'
        '<p>16:9 · 1920×1080 · 3–5 sekund. Klatki pokazują początek, środek i koniec każdego ujęcia.</p>'
        + "".join(cards) + '</html>'
    )
    (review / "index.html").write_text(page, encoding="utf-8")
    for item in items:
        print(f"{item['id']}: {item['width']}×{item['height']}, {item['actual_duration']:.2f} s, "
              f"{'OK' if item['valid'] else 'PARAMETRY NIEZGODNE'}")
    print(f"Sprawdzono {len(items)}/{len(config['scenes'])}. Podgląd: {review / 'index.html'}")
    return 1 if any(not item["valid"] for item in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
