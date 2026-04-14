from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import uuid
import os
import json
import re
import threading
import queue
import time
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# REQUEST
# =========================
class CodeRequest(BaseModel):
    code: str
    language: str
    input: str = ""


# =========================
# CONFIG
# =========================
EXT_MAP = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "c": ".c",
    "cpp": ".cpp",
    "java": ".java",
    "rust": ".rs",
    "go": ".go",
    "php": ".php",
    "ruby": ".rb",
    "bash": ".sh",
}

LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "rs": "rust",
    "golang": "go",
    "rb": "ruby",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
}

EXEC_TIMEOUT_SECONDS = 25


# =========================
# SAFETY (basic)
# =========================
def is_safe(code: str) -> bool:
    blocked = [
        "import os",
        "import sys",
        "subprocess",
        "open(",
        "__",
        "eval(",
        "exec(",
    ]
    lowered = code.lower()
    return not any(token in lowered for token in blocked)


def normalize_language(language: str) -> str:
    value = (language or "").strip().lower()
    return LANGUAGE_ALIASES.get(value, value)


def java_public_class_name(code: str) -> Optional[str]:
    match = re.search(r"public\s+class\s+([A-Za-z_]\w*)", code)
    return match.group(1) if match else None


# =========================
# COMMAND BUILDER
# =========================
def get_commands(filename: str, language: str, code: str):
    # returns (compile_cmd, run_cmd)
    if language == "python":
        return None, ["python", filename]

    if language == "javascript":
        return None, ["node", filename]

    if language == "typescript":
        # Requires: npm i -g ts-node typescript
        return None, ["npx", "ts-node", filename]

    if language == "c":
        return ["gcc", filename, "-O2", "-o", "out"], ["./out"]

    if language == "cpp":
        return ["g++", filename, "-O2", "-std=c++17", "-o", "out"], ["./out"]

    if language == "java":
        # File name must match public class name if present
        class_name = java_public_class_name(code)
        if class_name:
            expected = f"{class_name}.java"
            if os.path.basename(filename) != expected:
                return None, None
            return ["javac", filename], ["java", class_name]
        # fallback
        base = os.path.splitext(os.path.basename(filename))[0]
        return ["javac", filename], ["java", base]

    if language == "rust":
        return ["rustc", filename, "-O", "-o", "out"], ["./out"]

    if language == "go":
        return None, ["go", "run", filename]

    if language == "php":
        return None, ["php", filename]

    if language == "ruby":
        return None, ["ruby", filename]

    if language == "bash":
        return None, ["bash", filename]

    return None, None


def sse_data(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# =========================
# PROCESS STREAMER
# =========================
def stream_process(cmd, stdin_text: str = ""):
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if stdin_text and process.stdin:
        process.stdin.write(stdin_text)
    if process.stdin:
        process.stdin.close()

    q = queue.Queue()

    def pump(stream, stream_type: str):
        try:
            for line in iter(stream.readline, ""):
                q.put((stream_type, line))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    start = time.time()

    while True:
        if (time.time() - start) > EXEC_TIMEOUT_SECONDS:
            process.kill()
            yield {"type": "stderr", "content": f"Execution timed out after {EXEC_TIMEOUT_SECONDS}s\n"}
            break

        try:
            stream_type, line = q.get(timeout=0.1)
            yield {"type": stream_type, "content": line}
        except queue.Empty:
            if process.poll() is not None and q.empty():
                break

    rc = process.poll()
    yield {"type": "info", "content": f"Process exited with code {rc}\n"}


# =========================
# STREAM EXECUTION
# =========================
@app.post("/run/stream")
def run_code_stream(req: CodeRequest):
    def generate():
        code = req.code or ""
        language = normalize_language(req.language)

        if not code.strip():
            yield sse_data({"error": "Code is empty"})
            return

        if not is_safe(code):
            yield sse_data({"error": "Unsafe code detected"})
            return

        ext = EXT_MAP.get(language)
        if not ext:
            yield sse_data({"error": f"Unsupported language: {language}"})
            return

        file_id = str(uuid.uuid4())
        filename = f"{file_id}{ext}"

        # Java file-name rule for public class
        if language == "java":
            class_name = java_public_class_name(code)
            if class_name:
                filename = f"{class_name}.java"

        with open(filename, "w", encoding="utf-8") as f:
            f.write(code)

        compile_cmd, run_cmd = get_commands(filename, language, code)
        if run_cmd is None:
            yield sse_data({"error": "Invalid command / Java filename-public-class mismatch"})
            cleanup_temp_files(filename)
            return

        try:
            # Compile step
            if compile_cmd:
                compile_proc = subprocess.run(
                    compile_cmd,
                    capture_output=True,
                    text=True,
                    timeout=EXEC_TIMEOUT_SECONDS,
                )
                if compile_proc.stdout:
                    yield sse_data({"type": "stdout", "content": compile_proc.stdout})
                if compile_proc.stderr:
                    yield sse_data({"type": "stderr", "content": compile_proc.stderr})
                if compile_proc.returncode != 0:
                    yield "event: done\ndata: {\"status\":\"finished\"}\n\n"
                    return

            # Runtime step (stdin supported)
            user_input = req.input or ""
            if user_input and not user_input.endswith("\n"):
                user_input += "\n"

            for event in stream_process(run_cmd, user_input):
                yield sse_data(event)

            yield "event: done\ndata: {\"status\":\"finished\"}\n\n"

        except subprocess.TimeoutExpired:
            yield sse_data({"error": f"Execution timed out after {EXEC_TIMEOUT_SECONDS}s"})
        except Exception as e:
            yield sse_data({"error": str(e)})
        finally:
            cleanup_temp_files(filename)

    return StreamingResponse(generate(), media_type="text/event-stream")


def cleanup_temp_files(filename: str):
    try:
        if os.path.exists(filename):
            os.remove(filename)
        if os.path.exists("out"):
            os.remove("out")
    except Exception:
        pass


@app.get("/")
def root():
    return {"status": "Code Runner Streaming API 🚀"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=10000)
