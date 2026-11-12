"""
Twitter/X media downloader - بسيط ومستقر.
يستخدم gallery-dl للتحميل (يتعامل مع إعادة المحاولة والـ rate limit)
وFlask لتقديم واجهة ويب وAPI صغير.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).parent.resolve()
WORK = BASE / "downloads"
WORK.mkdir(exist_ok=True)

COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", BASE / "cookies.txt"))
AUTH_TOKEN = os.environ.get("TWITTER_AUTH_TOKEN", "").strip()
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "200"))      # أقصى عدد ملفات للطلب
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))  # ثواني
JOB_TTL = int(os.environ.get("JOB_TTL", "3600"))          # مدة الاحتفاظ بالملفات
WORKERS = int(os.environ.get("WORKERS", "2"))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
URL_USER_RE = re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})", re.I)
FILTERS = {
    "all": None,
    "photos": "extension in ('jpg','jpeg','png','webp')",
    "videos": "extension in ('mp4','webm','m4v','mov')",
}

app = Flask(__name__, static_folder=None)
pool = ThreadPoolExecutor(max_workers=WORKERS)
jobs: dict[str, dict] = {}
lock = threading.Lock()


def has_auth() -> bool:
    return bool(AUTH_TOKEN) or COOKIES_FILE.is_file()


def parse_username(raw: str):
    raw = (raw or "").strip()
    m = URL_USER_RE.search(raw)
    name = m.group(1) if m else raw.lstrip("@")
    return name if USERNAME_RE.match(name) else None


def write_config(job_dir: Path, out_dir: Path) -> Path:
    tw = {
        "videos": True,
        "retweets": False,
        "quoted": False,
        "cards": False,
        "text-tweets": False,
        "filename": "{tweet_id}_{num}.{extension}",
        "directory": [],
        "sleep-request": [1.0, 2.5],
        "retries": 5,
    }
    if COOKIES_FILE.is_file():
        tw["cookies"] = str(COOKIES_FILE)
    elif AUTH_TOKEN:
        tw["cookies"] = {"auth_token": AUTH_TOKEN}
    cfg = {
        "extractor": {"base-directory": str(out_dir), "twitter": tw},
        "downloader": {"retries": 5, "timeout": 30, "part": True},
    }
    path = job_dir / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def explain_error(tail: str) -> str:
    t = tail.lower()
    if "suspended" in t:
        return "هذا الحساب موقوف."
    if "notfound" in t or "does not exist" in t or "404" in t:
        return "لم يتم العثور على الحساب. تأكد من كتابة اليوزر بشكل صحيح."
    if "authorizationerror" in t or "protected" in t or "nsfw" in t:
        return "الحساب خاص أو محتواه يحتاج صلاحية. تأكد من أن حساب الكوكيز يتابع هذا الحساب."
    if "authenticationerror" in t or "401" in t or "403" in t or "login" in t:
        return "فشل تسجيل الدخول لدى تويتر. حدّث ملف cookies.txt أو التوكن."
    if "429" in t or "rate" in t:
        return "تويتر حدّ من الطلبات مؤقتاً. حاول بعد بضع دقائق."
    if "no results" in t or "no media" in t:
        return "لا توجد وسائط في هذا الحساب."
    return "لم يتم تنزيل أي ملف. قد لا يحتوي الحساب على وسائط أو تويتر رفض الطلب."


def public(job: dict) -> dict:
    keys = ("id", "username", "kind", "status", "count", "message", "files", "zip_size")
    return {k: job.get(k) for k in keys}


def run_job(job: dict):
    job_dir = WORK / job["id"]
    out_dir = job_dir / "files"
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = write_config(job_dir, out_dir)

    cmd = [sys.executable, "-m", "gallery_dl", "-c", str(cfg),
           "--range", f"1-{job['limit']}"]
    if FILTERS[job["kind"]]:
        cmd += ["--filter", FILTERS[job["kind"]]]
    cmd.append(f"https://x.com/{job['username']}/media")

    with lock:
        job["status"] = "running"
    tail = deque(maxlen=40)
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)

        def kill():
            nonlocal timed_out
            timed_out = True
            proc.kill()

        timer = threading.Timer(JOB_TIMEOUT, kill)
        timer.start()
        try:
            for line in proc.stdout:
                line = line.rstrip()
                tail.append(line)
                if str(out_dir) in line:
                    with lock:
                        job["count"] += 1
            proc.wait()
        finally:
            timer.cancel()
    except Exception as e:  # noqa: BLE001
        with lock:
            job.update(status="error", message=f"خطأ داخلي: {e}")
        return

    files = sorted(p for p in out_dir.iterdir()
                   if p.is_file() and not p.name.endswith(".part"))
    if not files:
        with lock:
            job.update(status="error", message=explain_error("\n".join(tail)))
        return

    zip_path = job_dir / "media.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # الوسائط مضغوطة أصلاً
        for p in files:
            z.write(p, p.name)

    note = ""
    if timed_out:
        note = " (توقف التحميل بسبب انتهاء المهلة، هذه النتائج الجزئية.)"
    elif proc.returncode not in (0, None):
        note = " (بعض الملفات تعذر تنزيلها.)"
    with lock:
        job.update(
            status="done",
            count=len(files),
            files=[{"name": p.name, "size": p.stat().st_size} for p in files],
            zip_size=zip_path.stat().st_size,
            message=f"تم تنزيل {len(files)} ملف.{note}",
        )


def cleaner():
    while True:
        time.sleep(300)
        now = time.time()
        with lock:
            old = [j for j in jobs.values()
                   if j["status"] in ("done", "error") and now - j["created"] > JOB_TTL]
            for j in old:
                jobs.pop(j["id"], None)
        for j in old:
            shutil.rmtree(WORK / j["id"], ignore_errors=True)


threading.Thread(target=cleaner, daemon=True).start()


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/health")
def health():
    return jsonify(gallery_dl=importlib.util.find_spec("gallery_dl") is not None,
                   auth=has_auth(), max_items=MAX_ITEMS)


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    username = parse_username(data.get("username"))
    if not username:
        return jsonify(error="اليوزر غير صالح. استخدم حروفاً إنجليزية وأرقاماً و_ فقط."), 400
    kind = data.get("kind", "all")
    if kind not in FILTERS:
        kind = "all"
    try:
        limit = max(1, min(int(data.get("limit", 50)), MAX_ITEMS))
    except (TypeError, ValueError):
        limit = 50

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    with lock:
        if any(j["ip"] == ip and j["status"] in ("queued", "running") for j in jobs.values()):
            return jsonify(error="لديك عملية تنزيل جارية. انتظر حتى تنتهي."), 429
        job = {"id": uuid.uuid4().hex, "username": username, "kind": kind,
               "limit": limit, "status": "queued", "count": 0, "message": "",
               "files": [], "zip_size": 0, "ip": ip, "created": time.time()}
        jobs[job["id"]] = job
    pool.submit(run_job, job)
    return jsonify(public(job)), 202


def get_job(job_id):
    with lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    with lock:
        return jsonify(public(job))


@app.get("/api/jobs/<job_id>/zip")
def job_zip(job_id):
    job = get_job(job_id)
    path = WORK / job["id"] / "media.zip"
    if job["status"] != "done" or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{job['username']}_media.zip")


@app.get("/api/jobs/<job_id>/files/<name>")
def job_file(job_id, name):
    job = get_job(job_id)
    return send_from_directory(WORK / job["id"] / "files", name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
