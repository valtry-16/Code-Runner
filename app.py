from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import subprocess
import uuid
import os
import json
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CodeRequest(BaseModel):
    code: str
    language: str


# =========================
# 🔥 SAFE CHECK (basic)
# =========================
def is_safe(code: str):
    blocked = ["import os", "import sys", "subprocess", "open(", "__"]
    return not any(b in code for b in blocked)


# =========================
# 🔥 COMMAND BUILDER
# =========================
def get_command(filename, language):
    if language == "python":
        return ["python", filename]

    elif language == "javascript":
        return ["node", filename]

    elif language == "cpp":
        return ["bash", "-c", f"g++ {filename} -o out && ./out"]

    elif language == "rust":
        return ["bash", "-c", f"rustc {filename} -o out && ./out"]

    else:
        return None


# =========================
# 🔥 STREAM EXECUTION
# =========================
@app.post("/run/stream")
def run_code_stream(req: CodeRequest):

    def generate():

        if not is_safe(req.code):
            yield f"data: {json.dumps({'error': 'Unsafe code detected'})}\n\n"
            return

        file_id = str(uuid.uuid4())

        ext_map = {
            "python": ".py",
            "javascript": ".js",
            "cpp": ".cpp",
            "rust": ".rs"
        }

        ext = ext_map.get(req.language)

        if not ext:
            yield f"data: {json.dumps({'error': 'Unsupported language'})}\n\n"
            return

        filename = file_id + ext

        with open(filename, "w") as f:
            f.write(req.code)

        cmd = get_command(filename, req.language)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            # 🔥 STREAM STDOUT
            for line in process.stdout:
                yield f"data: {json.dumps({'type': 'stdout', 'content': line})}\n\n"

            # 🔥 STREAM STDERR
            for line in process.stderr:
                yield f"data: {json.dumps({'type': 'stderr', 'content': line})}\n\n"

            process.wait()

            yield f"event: done\ndata: {json.dumps({'status': 'finished'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        finally:
            try:
                os.remove(filename)
                if os.path.exists("out"):
                    os.remove("out")
            except:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream")


# =========================
# HEALTH CHECK
# =========================
@app.get("/")
def root():
    return {"status": "Code Runner Streaming API 🚀"}


# =========================
# RUN
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=10000)
