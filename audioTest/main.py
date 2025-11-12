from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from uuid import uuid4
from pathlib import Path
import json, shutil, subprocess, os
import httpx

app = FastAPI()

BASE_DIR = Path(__file__).parent
SESS_BASE = BASE_DIR / "sessions"
SESS_BASE.mkdir(exist_ok=True)

# 정적 파일 제공
app.mount("/static", StaticFiles(directory=str(BASE_DIR), html=True), name="static")

@app.get("/")
def root():
    return FileResponse(BASE_DIR / "index.html")

# ---------- 설정: 판단 서버 주소 ----------
JUDGE_BASE_URL = os.getenv("JUDGE_BASE_URL", "http://127.0.0.1:9000")
JUDGE_INGEST_CHUNK = f"{JUDGE_BASE_URL}/ingest-chunk"
JUDGE_INGEST_FINAL = f"{JUDGE_BASE_URL}/ingest-final"

# ---------- ffmpeg 탐색/호출 ----------
def resolve_ffmpeg():
    env_path = os.getenv("FFMPEG_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    return None

FFMPEG = resolve_ffmpeg()

def run_ffmpeg(args: list):
    if not FFMPEG:
        raise RuntimeError("ffmpeg not found. Install it or set FFMPEG_PATH.")
    proc = subprocess.run([FFMPEG] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n{proc.stderr}")
    return proc

# ---------- 모델 ----------
class FinalizeReq(BaseModel):
    sessionId: str

# ---------- 헬퍼 ----------
def sess_dir(sid: str) -> Path:
    d = SESS_BASE / sid
    d.mkdir(parents=True, exist_ok=True)
    return d

def meta_path(sid: str) -> Path:
    return sess_dir(sid) / "meta.json"

def load_meta(sid: str) -> dict:
    p = meta_path(sid)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"state": "waiting", "start_seq": None, "end_seq": None}

def save_meta(sid: str, meta: dict):
    meta_path(sid).write_text(json.dumps(meta), encoding="utf-8")

# 판단 서버 호출: 조각 전달 → start/end 또는 continue
def send_to_judge(session_id: str, seq: str, container: str, filepath: Path) -> str:
    try:
        with filepath.open("rb") as f:
            files = {"chunk": (filepath.name, f, f"audio/{container}")}
            data = {"sessionId": session_id, "seq": seq, "container": container}
            resp = httpx.post(JUDGE_INGEST_CHUNK, data=data, files=files, timeout=10.0)

        if resp.status_code == 204:
            return "continue"
        if resp.headers.get("content-type", "").startswith("application/json"):
            js = resp.json()
            dec = str(js.get("decision", "")).lower()
            if dec in ("start", "end"):
                return dec
        return "continue"
    except Exception:
        return "continue"

# 완성 파일을 판단 서버로 전달
def send_final_to_judge(session_id: str, final_path: Path) -> bool:
    try:
        with final_path.open("rb") as f:
            files = {"final": (final_path.name, f, "audio/wav")}
            data = {"sessionId": session_id}
            resp = httpx.post(JUDGE_INGEST_FINAL, data=data, files=files, timeout=20.0)
            return resp.status_code in (200, 201)
    except Exception:
        return False

# 병합: start~end 조각을 WAV로 합쳐 Path 반환
def build_final_wav(session_id: str) -> tuple[Path, list]:
    d = sess_dir(session_id)
    meta = load_meta(session_id)
    if meta["start_seq"] is None or meta["end_seq"] is None:
        raise RuntimeError("start/end not decided yet")

    start_seq, end_seq = meta["start_seq"], meta["end_seq"]

    parts = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".ogg", ".webm"):
            if start_seq <= p.stem <= end_seq:
                parts.append(p)
    if not parts:
        raise RuntimeError("no chunks in selected range")

    wav_dir = d / "wav"
    wav_dir.mkdir(exist_ok=True)
    skipped = []
    converted = []

    for p in parts:
        w = wav_dir / (p.stem + ".wav")
        if w.exists():
            converted.append(w)
            continue
        try:
            run_ffmpeg([
                "-hide_banner", "-loglevel", "error",
                "-fflags", "+genpts",
                "-y", "-i", str(p),
                "-ac", "1", "-ar", "16000",
                str(w)
            ])
            converted.append(w)
        except Exception as ee:
            skipped.append((p.name, str(ee)))

    if not converted:
        raise RuntimeError("no chunk could be decoded; prefer Ogg/Opus or ensure full WebM fragments.")

    concat_txt = d / "concat.txt"
    with concat_txt.open("w", encoding="utf-8") as f:
        for w in sorted(converted, key=lambda x: x.stem):
            if start_seq <= w.stem <= end_seq:
                f.write(f"file '{w.as_posix()}'\n")

    out_wav = d / "final.wav"
    run_ffmpeg([
        "-hide_banner", "-loglevel", "error",
        "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_txt),
        "-c", "copy",
        str(out_wav)
    ])

    return out_wav, skipped

# ---------- 라우트 ----------
@app.post("/start")
def start():
    sid = str(uuid4())
    save_meta(sid, {"state": "waiting", "start_seq": None, "end_seq": None})
    return {"sessionId": sid}

@app.post("/upload-chunk")
async def upload_chunk(
    sessionId: str = Form(...),
    seq: str = Form(...),
    chunk: UploadFile = Form(...),
    container: str = Form("ogg"),   # "ogg" or "webm"
):
    try:
        d = sess_dir(sessionId)
        ext = "ogg" if container == "ogg" else "webm"
        out_path = d / f"{seq}.{ext}"
        with out_path.open("wb") as f:
            shutil.copyfileobj(chunk.file, f)

        meta = load_meta(sessionId)
        if meta["state"] == "ended":
            return {"decision": "continue"}

        decision = send_to_judge(sessionId, seq, ext, out_path)

        if decision == "start" and meta["state"] == "waiting":
            meta["state"] = "recording"
            meta["start_seq"] = seq
            save_meta(sessionId, meta)

        elif decision == "end":
            meta["state"] = "ended"
            meta["end_seq"] = seq
            save_meta(sessionId, meta)

            # 병합 → 판단 서버에 최종 파일 전송
            final_path, skipped = build_final_wav(sessionId)
            ok = send_final_to_judge(sessionId, final_path)
            return {"decision": "end", "finalSent": ok, "skipped": skipped}

        return {"decision": decision}

    except Exception as e:
        return JSONResponse({"error": "upload_failed", "detail": str(e)}, status_code=500)

@app.post("/finalize")
def finalize(req: FinalizeReq):
    try:
        final_path, skipped = build_final_wav(req.sessionId)
        return {"fileUrl": f"/download/{req.sessionId}", "skipped": skipped}
    except Exception as e:
        return JSONResponse({"error": "ffmpeg_failed", "detail": str(e)}, status_code=500)

@app.get("/download/{session_id}")
def download(session_id: str):
    out_wav = sess_dir(session_id) / "final.wav"
    if not out_wav.exists():
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(str(out_wav), media_type="audio/wav", filename="final.wav")