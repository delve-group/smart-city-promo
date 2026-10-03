#!/usr/bin/env python3
"""Generate the reference-matched film shots with Wan 3.0 through Runware."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import re
import shutil
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
API_URL = "https://api.runware.ai/v1"
PRINT_LOCK = threading.Lock()


class GenerationError(Exception):
    def __init__(self, message, definitive=False, code=""):
        super().__init__(message)
        self.definitive = definitive
        self.code = code


def log(message):
    with PRINT_LOCK:
        print(message, flush=True)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def describe_error(value, key):
    if isinstance(value, str):
        message, code = value, ""
    elif isinstance(value, dict):
        message = str(value.get("message", "Błąd Runware"))
        code = str(value.get("code", ""))
    else:
        message, code = "Błąd Runware", ""
    message = message.replace(key, "[UKRYTY KLUCZ]") if key else message
    message = re.sub(r"data:[^\s\"]+", "[DANE OBRAZU]", message)
    return (f"{code}: {message}" if code else message)[:800], code


def api_request(payload, key, context):
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    # Submission requests are never retried automatically: a lost acknowledgement
    # may still represent a billable task. The saved task UUID is polled instead.
    try:
        with urllib.request.urlopen(request, context=context, timeout=90) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read())
        except (ValueError, UnicodeDecodeError):
            raise GenerationError(f"Runware HTTP {error.code}", definitive=400 <= error.code < 500) from None
        errors = result.get("errors") or [result.get("error", {"message": f"HTTP {error.code}"})]
        if isinstance(errors, dict):
            errors = [errors]
        message, code = describe_error(errors[0], key)
        raise GenerationError(message, definitive=400 <= error.code < 500, code=code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        message = str(getattr(error, "reason", error)).replace(key, "[UKRYTY KLUCZ]")
        raise GenerationError(f"Błąd połączenia: {message[:300]}") from None
    except ValueError:
        raise GenerationError("Runware zwróciło niepoprawny JSON; zachowano UUID zadania.") from None
    if not isinstance(result, dict):
        raise GenerationError("Nieoczekiwany format odpowiedzi Runware.")
    errors = result.get("errors") or ([result["error"]] if result.get("error") else [])
    if isinstance(errors, dict):
        errors = [errors]
    if errors:
        message, code = describe_error(errors[0], key)
        raise GenerationError(message, definitive=True, code=code)
    return result.get("data", [])


def ask_key(dialog, estimate):
    # Never put the key in a command argument, file, log, or chat message.
    if dialog or (sys.platform == "darwin" and not sys.stdin.isatty()):
        if sys.platform != "darwin" or not Path("/usr/bin/osascript").exists():
            raise GenerationError("Okno klucza jest dostępne na macOS. Uruchom skrypt w swoim terminalu.")
        script = (
            'set answer to display dialog "Podaj Runware API KEY. Klucz pozostanie tylko w pamięci procesu. '
            f'Generacja wybranych scen: szacunkowo {estimate:.2f} USD, Wan 3.0, 16:9, 1080p." '
            'default answer "" with hidden answer buttons {"Anuluj", "Generuj"} '
            'default button "Generuj" cancel button "Anuluj" with title "Smart City — Runware"\n'
            'return text returned of answer'
        )
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False
        )
        if result.returncode:
            if "-128" in result.stderr:
                raise GenerationError("Anulowano podanie klucza; nie wysłano żadnej generacji.")
            raise GenerationError("Nie udało się otworzyć okna klucza. Uruchom skrypt w swoim terminalu.")
        key = result.stdout.strip()
    else:
        if not sys.stdin.isatty():
            raise GenerationError("Uruchom skrypt w interaktywnym terminalu, aby bezpiecznie podać klucz.")
        key = getpass.getpass("Runware API KEY (wpisywanie jest ukryte): ").strip()
    if not key:
        raise GenerationError("Nie podano klucza; nie wysłano żadnej generacji.")
    if "\n" in key or "\r" in key:
        raise GenerationError("Klucz musi być pojedynczą linią.")
    return key


def load_project(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if config["model"] != "alibaba:wan@3.0":
        raise GenerationError("Ten projekt wymaga modelu alibaba:wan@3.0.")
    if (config["width"], config["height"]) != (1920, 1080):
        raise GenerationError("Projekt ma być poziomy: ustaw width=1920, height=1080.")
    available = {}
    for image in (ROOT / "referenceimages").rglob("*"):
        if image.is_file():
            available.setdefault(image.name.casefold(), []).append(image)
    seen = set()
    for scene in config["scenes"]:
        if not re.fullmatch(r"[a-z0-9_]+", scene["id"]) or scene["id"] in seen:
            raise GenerationError("Identyfikatory scen muszą być unikatowe i zawierać tylko a-z, 0-9, _.")
        seen.add(scene["id"])
        if type(scene["duration"]) is not int or not 3 <= scene["duration"] <= 5:
            raise GenerationError(f"{scene['id']}: dozwolone są wyłącznie ujęcia 3–5 sekund.")
        if not 1 <= len(scene["references"]) <= 10:
            raise GenerationError(f"{scene['id']}: wymagane 1–10 zdjęć referencyjnych.")
        scene["reference_paths"] = []
        for filename in scene["references"]:
            matches = available.get(filename.casefold(), [])
            if len(matches) != 1:
                raise GenerationError(f"Nie można jednoznacznie znaleźć zdjęcia: {filename}")
            image_path = matches[0]
            if image_path.stat().st_size > 20 * 1024 * 1024:
                raise GenerationError(f"Zdjęcie jest większe niż 20 MB: {filename}")
            with image_path.open("rb") as image_file:
                header = image_file.read(24)
            if header[:8] != b"\x89PNG\r\n\x1a\n":
                raise GenerationError(f"Oczekiwano obrazu PNG: {filename}")
            width, height = struct.unpack(">II", header[16:24])
            if min(width, height) < 240 or not 1 / 8 <= width / height <= 8:
                raise GenerationError(f"Niedozwolone wymiary referencji: {filename}")
            scene["reference_paths"].append(image_path)
    return config


def full_prompt(config, scene):
    return scene["prompt"] + " " + config["style_prompt"]


def fingerprint(config, scene):
    content = {
        "model": config["model"], "width": config["width"], "height": config["height"],
        "audio": config["audio"], "prompt_extend": config["prompt_extend"],
        "duration": scene["duration"], "prompt": full_prompt(config, scene),
        "images": [hashlib.sha256(p.read_bytes()).hexdigest() for p in scene["reference_paths"]],
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def build_payload(config, scene, task_uuid):
    return {
        "taskType": "videoInference", "taskUUID": task_uuid,
        "model": config["model"], "positivePrompt": full_prompt(config, scene),
        "width": config["width"], "height": config["height"], "duration": scene["duration"],
        "numberResults": 1, "outputType": "URL", "outputFormat": "MP4",
        "deliveryMethod": "async", "includeCost": True,
        "inputs": {"referenceImages": [
            "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
            for p in scene["reference_paths"]
        ]},
        "settings": {"audio": config["audio"], "promptExtend": config["prompt_extend"]},
    }


def export_prompts(config, destination=None):
    lines = [
        "SMART CITY — PROMPTY WAN 3.0 / RUNWARE",
        "Format: poziomy 16:9, 1920×1080. Ujęcia: 3–5 sekund.",
        "Zdjęcia są przypisane po obejrzeniu zawartości. Image 1 / Image 2 odpowiadają kolejności poniżej.",
        "Powiadomienie: ulice X, Y, Z to placeholdery. Czytelność liter w generowanym wideo wymaga kontroli.",
        "Gdy tekst aplikacji musi być dokładny, nałóż docelowy interfejs podczas montażu.",
        "W scenach zgłoszenia 05 i 06 ekran telefonu pozostaje niewidoczny; widzimy wyłącznie tył obudowy.",
        "",
    ]
    for scene in config["scenes"]:
        lines.extend([
            f"{scene['id']} — {scene['title']} ({scene['duration']} s)",
            "Referencje: " + ", ".join(scene["references"]),
            "Dopasowanie: " + scene["reference_notes"],
            "Prompt:", full_prompt(config, scene), "",
        ])
    destination = destination or ROOT / "prompty.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def download_video(url, destination, context):
    if not url.startswith("https://"):
        raise GenerationError("Runware zwróciło adres wideo bez HTTPS.")
    temporary = destination.with_suffix(".mp4.part")
    try:
        with urllib.request.urlopen(url, context=context, timeout=90) as response:
            with temporary.open("wb") as video_file:
                shutil.copyfileobj(response, video_file)
        with temporary.open("rb") as video_file:
            header = video_file.read(32)
        if b"ftyp" not in header:
            raise GenerationError("Pobrany wynik nie ma prawidłowego nagłówka MP4.")
        temporary.replace(destination)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise GenerationError(f"Nie udało się pobrać MP4: {type(error).__name__}. Wynik pozostaje w Runware.") from None


def generate_scene(config, scene, key, output, context, timeout, retry_failed, stop):
    identifier = scene["id"]
    state_path = output / "tasks" / (identifier + ".json")
    destination = output / (identifier + ".mp4")
    signature = fingerprint(config, scene)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    if state and state.get("fingerprint") != signature:
        raise GenerationError(f"{identifier}: parametry się zmieniły. Użyj nowego --output, aby zachować stare wyniki.")
    if state and state.get("status") == "downloaded" and destination.exists():
        log(f"[{identifier}] Gotowy plik już istnieje — pomijam.")
        return state
    if state and state.get("status") == "failed":
        if not retry_failed:
            raise GenerationError(f"{identifier}: poprzednia generacja nie powiodła się. Szczegóły w {state_path}. Ponowna: --retry-failed.")
        state = None
    if state and state.get("videoURL"):
        download_video(state["videoURL"], destination, context)
        state["status"] = "downloaded"
        write_json(state_path, state)
        log(f"[{identifier}] Pobrano zachowany wynik.")
        return state
    if stop.is_set():
        raise GenerationError(f"{identifier}: wstrzymano nowe zadania po błędzie konta.")
    if state is None:
        task_uuid = str(uuid.uuid4())
        payload = build_payload(config, scene, task_uuid)
        state = {
            "id": identifier, "title": scene["title"], "fingerprint": signature,
            "taskUUID": task_uuid, "status": "submitting", "duration": scene["duration"],
            "references": scene["references"], "model": config["model"],
            "width": config["width"], "height": config["height"],
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        write_json(state_path, state)
        log(f"[{identifier}] Wysyłam {scene['duration']} s, 1920×1080, {len(scene['references'])} referencje.")
        try:
            data = api_request([payload], key, context)
        except GenerationError as error:
            state["status"] = "failed" if error.definitive else "submission_uncertain"
            state["error"] = str(error)
            write_json(state_path, state)
            if any(word in error.code.lower() for word in ("apikey", "auth", "credit", "balance", "fund")):
                stop.set()
            raise
        state["status"] = "processing"
        write_json(state_path, state)
    else:
        log(f"[{identifier}] Wznawiam sprawdzanie istniejącego zadania {state['taskUUID']}.")
        data = []
    started = time.monotonic()
    delay = 5
    last_log = started
    while time.monotonic() - started < timeout:
        matching = [item for item in data if item.get("taskUUID") == state["taskUUID"]]
        result = next((item for item in matching if item.get("videoURL")), None)
        if result:
            for field in ("videoURL", "videoUUID", "cost", "seed"):
                if field in result:
                    state[field] = result[field]
            state["status"] = "generated"
            write_json(state_path, state)
            download_video(state["videoURL"], destination, context)
            state["status"] = "downloaded"
            state.pop("error", None)
            write_json(state_path, state)
            log(f"[{identifier}] Gotowe: {destination.name}; koszt: {state.get('cost', 'brak danych')} USD.")
            return state
        failed = next((item for item in matching if item.get("status") in ("error", "failed")), None)
        if failed:
            message, code = describe_error(failed.get("error", failed), key)
            state.update(status="failed", error=message)
            write_json(state_path, state)
            raise GenerationError(message, definitive=True, code=code)
        time.sleep(delay)
        delay = min(20, delay + 3)
        try:
            data = api_request([{"taskType": "getResponse", "taskUUID": state["taskUUID"]}], key, context)
        except GenerationError as error:
            if error.definitive:
                # A lost acknowledgement plus task-not-found is left uncertain;
                # it never triggers another paid submission automatically.
                if state.get("status") in ("submitting", "submission_uncertain"):
                    state["error"] = str(error)
                    write_json(state_path, state)
                    raise
                state.update(status="failed", error=str(error))
                write_json(state_path, state)
                raise
            log(f"[{identifier}] Chwilowy błąd odczytu; ponowię tylko sprawdzanie statusu.")
            data = []
        if time.monotonic() - last_log >= 45:
            log(f"[{identifier}] Generacja trwa ({int(time.monotonic() - started)} s).")
            last_log = time.monotonic()
    raise GenerationError(f"{identifier}: upłynął limit oczekiwania; UUID zapisano. Uruchom ponownie, aby sprawdzić wynik.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Sprawdź zdjęcia i eksportuj prompty bez wywoływania API.")
    parser.add_argument("--key-dialog", action="store_true", help="Poproś o klucz w ukrytym oknie macOS.")
    parser.add_argument("--scenes", nargs="+", help="Generuj tylko wskazane identyfikatory scen.")
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument("--config", type=Path, default=ROOT / "scenes.json", help="Wersja promptów używana do generacji.")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1800, help="Limit oczekiwania na pojedynczą scenę, w sekundach.")
    parser.add_argument("--retry-failed", action="store_true", help="Wyślij ponownie zadania zakończone błędem; może naliczyć nowy koszt.")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4 or args.timeout < 30:
        parser.error("--concurrency musi być 1–4, a --timeout co najmniej 30.")
    config = load_project(args.config)
    scenes = config["scenes"]
    if args.scenes:
        requested = set(args.scenes)
        missing = requested - {scene["id"] for scene in scenes}
        if missing:
            parser.error("Nieznane sceny: " + ", ".join(sorted(missing)))
        scenes = [scene for scene in scenes if scene["id"] in requested]
    selected_config = {**config, "scenes": scenes}
    prompt_path = ROOT / "prompty.txt" if args.config.resolve() == (ROOT / "scenes.json").resolve() else args.output / "prompty.txt"
    export_prompts(selected_config, prompt_path)
    seconds = sum(scene["duration"] for scene in scenes)
    estimate = seconds * 0.20
    log(f"Wan 3.0 | 16:9 | 1920×1080 | {len(scenes)} ujęć | {seconds} s | szacunkowo {estimate:.2f} USD")
    for scene in scenes:
        log(f"{scene['id']}: {scene['duration']} s — {scene['title']}")
    if args.dry_run:
        log(f"Zdjęcia i parametry sprawdzone. Prompty zapisane w {prompt_path}. Nie wywołano API.")
        return 0
    key = ask_key(args.key_dialog, estimate)
    log("Odebrano klucz w pamięci procesu; rozpoczynam generację.")
    args.output.mkdir(parents=True, exist_ok=True)
    snapshot = {**selected_config, "scenes": [
        {field: value for field, value in scene.items() if field != "reference_paths"}
        for scene in scenes
    ]}
    write_json(args.output / "scenes.json", snapshot)
    certificate_file = Path("/etc/ssl/cert.pem")
    context = ssl.create_default_context(cafile=str(certificate_file) if certificate_file.exists() else None)
    stop = threading.Event()
    successes, failures = [], []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        jobs = {executor.submit(generate_scene, config, scene, key, args.output, context,
                                args.timeout, args.retry_failed, stop): scene for scene in scenes}
        for job in as_completed(jobs):
            scene = jobs[job]
            try:
                successes.append(job.result())
            except GenerationError as error:
                log(f"[{scene['id']}] {error}")
                failures.append({"id": scene["id"], "error": str(error)})
    report = {
        "model": config["model"], "width": config["width"], "height": config["height"],
        "completed": sorted(successes, key=lambda item: item["id"]),
        "failed": sorted(failures, key=lambda item: item["id"]),
        "reported_cost_usd": round(sum(item.get("cost", 0) or 0 for item in successes), 6),
    }
    write_json(args.output / "generation_report.json", report)
    log(f"Zakończono: {len(successes)}/{len(scenes)} MP4. Raport: {args.output / 'generation_report.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GenerationError as error:
        log(str(error))
        sys.exit(1)
    except KeyboardInterrupt:
        log("Przerwano oczekiwanie. Wysłane zadania mogą nadal być generowane; uruchom ponownie, aby pobrać wyniki.")
        sys.exit(130)
