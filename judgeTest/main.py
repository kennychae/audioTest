from fastapi import FastAPI, UploadFile, Form, Response
from fastapi.responses import JSONResponse
from pathlib import Path
from typing import Dict
import shutil

app = FastAPI()

BASE = Path(__file__).parent
SESS_BASE = BASE / "sessions_b"
SESS_BASE.mkdir(exist_ok=True)
INBOX = BASE / "inbox"        # (선택) 조각 저장용
INBOX.mkdir(exist_ok=True)

# 세션별 조각 카운트 (데모/메모리)
session_counts: Dict[str, int] = {}

@app.post("/ingest-chunk")
async def ingest_chunk(
    sessionId: str = Form(...),
    seq: str = Form(...),
    container: str = Form(...),  # "ogg" or "webm"
    chunk: UploadFile = Form(...)
):
    try:
        # (선택) 디버깅용 원본 저장
        sess_inbox = INBOX / sessionId
        sess_inbox.mkdir(parents=True, exist_ok=True)
        out_path = sess_inbox / f"{seq}.{container}"
        with out_path.open("wb") as f:
            shutil.copyfileobj(chunk.file, f)

        # 데모 : 2번째는 start, 8번째는 end
        n = session_counts.get(sessionId, 0) + 1
        session_counts[sessionId] = n

        if n == 2:
            return {"decision": "start"}
        if n == 8:
            return {"decision": "end"}

        # 결정 없음
        return Response(status_code=204)

    except Exception:
        return Response(status_code=204)

@app.post("/ingest-final")
async def ingest_final(
    sessionId: str = Form(...),
    final: UploadFile = Form(...)
):
    """
    Server A가 end를 받은 즉시 병합한 최종 WAV를 업로드.
    여기서 세션 폴더에 저장.
    """
    try:
        d = SESS_BASE
        out_path = d / "final.wav"
        with out_path.open("wb") as f:
            shutil.copyfileobj(final.file, f)

        # 저장 완료
        return JSONResponse({"saved": True, "path": str(out_path)}, status_code=201)
    except Exception as e:
        return JSONResponse({"saved": False, "detail": str(e)}, status_code=500)