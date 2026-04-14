from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import tempfile
import shutil
import os
import json
import re
import time

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
    input: str = ""

# Frontend logo language aliases -> canonical language
LANGUAGE_ALIASES = {
    # JS/TS
    "js": "javascript", "jsx": "javascript", "javascript": "javascript",
    "ts": "typescript", "tsx": "typescript", "typescript": "typescript",

    # Python
    "py": "python", "python": "python",

    # C-family
    "c": "c", "cpp": "cpp", "c++": "cpp", "cc": "cpp", "cxx": "cpp",
    "cs": "csharp", "csharp": "csharp",

    # JVM/native
    "java": "java", "kt": "kotlin", "kotlin": "kotlin",
    "go": "go", "golang": "go",
    "rs": "rust", "rust": "rust",
    "swift": "swift",

    # Script
    "sh": "bash", "zsh": "bash", "shell": "bash", "bash": "bash",
    "php": "php", "rb": "ruby", "ruby": "ruby",

    # Data/markup/config/logo-only languages from frontend
    "sql": "sql", "postgres": "sql", "postgresql": "sql", "plsql": "sql",
    "json": "json",
    "html": "html",
    "css": "css",
    "scss": "scss", "sass": "scss",
    "less": "less",
    "yaml": "yaml", "yml": "yaml",
    "markdown": "markdown", "md": "markdown",
}

# Executable languages on your current runner
EXT_MAP = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "c": ".c",
    "cpp": ".cpp",
    "java": ".java",
    "rust": ".rs",
    "go": ".go",
    "bash": ".sh",
    "php": ".php",
    "ruby": ".rb",
    # csharp/kotlin/swift can be added if toolchains are installed
}

NON_EXEC_LANGS = {"sql", "json", "html", "css", "scss", "less", "yaml", "markdown"}

TIMEOUT_SEC = 20

def canon_lang(lang: str) -> str:
    v = (lang or "").strip().lower()
    return LANGUAGE_ALIASES.get(v, v)

def detect_java_classname(code: str) -> str:
    m = re.search(r"public\s+class\s+([A-Za-z_]\w*)", code)
    return m.group(1) if m else "Main"

def build_commands(filename: str, language: str, code: str):
    # returns (compile_cmd or None, run_cmd or None)
    if language == "python":
        return None, ["python3", filename]

    if language == "javascript":
        return None, ["node", filename]

    if language == "typescript":
        # Needs ts-node installed in runtime
        return None, ["npx", "ts-node", filename]

    if language == "c":
        out = "out_c"
        return ["gcc", filename, "-O2", "-o", out], [f"./{out}"]

    if language == "cpp":
        out = "out_cpp"
        return ["g++", filename, "-O2", "-std=c++17", "-o", out], [f"./{out}"]

    if language == "java":
        cls = detect_java_classname(code)
        return ["javac", filename], ["java", cls]

    if language == "rust":
        out = "out_rust"
        return ["rustc", filename, "-O", "-o", out], [f"./{out}"]

    if language == "go":
        return None, ["go", "run", filename]

    if language == "bash":
        return None, ["bash", filename]

    if language == "php":
        return None, ["php", filename]

    if language == "ruby":
        return None, ["ruby", filename]

    return None, None

def sse_data(payload: dict):
    return f"data: {json.dumps(payload)}\n\n"

def run_stream(cmd, cwd, stdin_text=""):
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if stdin_text and proc.stdin:
        proc.stdin.write(stdin_text)
    if proc.stdin:
        proc.stdin.close()

    start = time.time()
    while True:
        if time.time() - start > TIMEOUT_SEC:
            proc.kill()
            yield {"type": "stderr", "content": f"Execution timed out after {TIMEOUT_SEC}s\n"}
            break

        out = proc.stdout.readline() if proc.stdout else ""
        err = proc.stderr.readline() if proc.stderr else ""

        if out:
            yield {"type": "stdout", "content": out}
        if err:
            yield {"type": "stderr", "content": err}

        if out == "" and err == "" and proc.poll() is not None:
            break

@app.post("/run/stream")
def run_code_stream(req: CodeRequest):
    def generate():
        language = canon_lang(req.language)

        if language in NON_EXEC_LANGS:
            yield sse_data({
                "type": "info",
                "content": f"'{language}' is not directly executable. Use this block as data/markup/config.\n"
            })
            yield "event: done\ndata: {\"status\":\"finished\"}\n\n"
            return

        ext = EXT_MAP.get(language)
        if not ext:
            yield sse_data({"error": f"Unsupported language: {language}"})
            return

        workdir = tempfile.mkdtemp(prefix="runner_")
        try:
            filename = f"main{ext}"
            filepath = os.path.join(workdir, filename)

            code = req.code or ""
            if language == "java":
                cls = detect_java_classname(code)
                filename = f"{cls}.java"
                filepath = os.path.join(workdir, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(code)

            compile_cmd, run_cmd = build_commands(filename, language, code)
            if not run_cmd:
                yield sse_data({"error": "Invalid command"})
                return

            # compile step
            if compile_cmd:
                for ev in run_stream(compile_cmd, workdir):
                    yield sse_data(ev)

                c = subprocess.run(compile_cmd, cwd=workdir, capture_output=True, text=True)
                if c.returncode != 0:
                    if c.stderr:
                        yield sse_data({"type": "stderr", "content": c.stderr})
                    yield "event: done\ndata: {\"status\":\"finished\"}\n\n"
                    return

            stdin_data = req.input or ""
            for ev in run_stream(run_cmd, workdir, stdin_data):
                yield sse_data(ev)

            yield "event: done\ndata: {\"status\":\"finished\"}\n\n"

        except Exception as e:
            yield sse_data({"error": str(e)})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/")
def root():
    return {"status": "Code Runner API 🚀"}
