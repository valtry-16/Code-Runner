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

# =========================
# REQUEST
# =========================
class CodeRequest(BaseModel):
    code: str
    language: str
    input: str = ""   # 🔥 NEW


# =========================
# EXTENSIONS
# =========================
EXT_MAP = {
    "python": ".py",
    "javascript": ".js",
    "cpp": ".cpp",
    "c": ".c",
    "java": ".java",
    "rust": ".rs",
    "go": ".go",
    "bash": ".sh"
}


# =========================
# COMMAND BUILDER
# =========================
def get_run_command(filename, language):

    if language == "python":
        return f"python {filename}"

    elif language == "javascript":
        return f"node {filename}"

    elif language == "cpp":
        return f"g++ {filename} -o out && ./out"

    elif language == "c":
        return f"gcc {filename} -o out && ./out"

    elif language == "java":
        classname = filename.replace(".java", "")
        return f"javac {filename} && java {classname}"

    elif language == "rust":
        return f"rustc {filename} -o out && ./out"

    elif language == "go":
        return f"go run {filename}"

    elif language == "bash":
        return f"bash {filename}"

    return None


# =========================
# STREAM EXECUTION
# =========================
@app.post("/run/stream")
def run_code_stream(req: CodeRequest):

    def generate():

        file_id = str(uuid.uuid4())
        ext = EXT_MAP.get(req.language)

        if not ext:
            yield f"data: {json.dumps({'error': 'Unsupported language'})}\n\n"
            return

        filename = file_id + ext

        with open(filename, "w") as f:
            f.write(req.code)

        run_cmd = get_run_command(filename, req.language)

        if not run_cmd:
            yield f"data: {json.dumps({'error': 'Invalid command'})}\n\n"
            return

        # 🔥 DOCKER COMMAND
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "-i",   # 🔥 allow stdin
            "-v", f"{os.getcwd()}:/app",
            "-w", "/app",
            "ubuntu:22.04",
            "bash", "-c",
            f"""
            apt update >/dev/null 2>&1 &&
            apt install -y python3 nodejs gcc g++ openjdk-17-jdk golang rustc >/dev/null 2>&1 &&
            {run_cmd}
            """
        ]

        try:
            process = subprocess.Popen(
                docker_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            # 🔥 SEND INPUT
            if req.input:
                process.stdin.write(req.input)
                process.stdin.close()

            # 🔥 STREAM OUTPUT
            while True:
                output = process.stdout.readline()
                if output:
                    yield f"data: {json.dumps({'type':'stdout','content':output})}\n\n"

                error = process.stderr.readline()
                if error:
                    yield f"data: {json.dumps({'type':'stderr','content':error})}\n\n"

                if output == "" and error == "" and process.poll() is not None:
                    break

            yield f"event: done\ndata: {json.dumps({'status':'finished'})}\n\n"

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


@app.get("/")
def root():
    return {"status": "Docker Code Runner 🚀"}
