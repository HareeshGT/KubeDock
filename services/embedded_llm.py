"""Secure lazy model-cache and inference primitives for embedded AI."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
import sys
import urllib.request

MAX_MODEL_BYTES = 500 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30

MODEL_DIRECTORY_NAME = "models"


def bundled_model_directory() -> Path:
    """Return the model directory inside the installed KubeDock application."""
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin" and executable_dir.parent.name == "Contents":
            return executable_dir.parent / "Resources" / MODEL_DIRECTORY_NAME
        return executable_dir / MODEL_DIRECTORY_NAME
    return Path(__file__).resolve().parent.parent / MODEL_DIRECTORY_NAME



class EmbeddedModelError(RuntimeError):
    """Safe error suitable for display in KubeDock."""


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    filename: str
    url: str
    size_bytes: int
    sha256: str

    def validate(self):
        if not self.model_id or not self.filename or Path(self.filename).name != self.filename:
            raise EmbeddedModelError("Invalid embedded model catalogue entry.")
        if not self.url.startswith("https://"):
            raise EmbeddedModelError("Embedded model downloads must use HTTPS.")
        if not isinstance(self.size_bytes, int) or not 0 < self.size_bytes <= MAX_MODEL_BYTES:
            raise EmbeddedModelError("The selected model must be no larger than 500 MB.")
        digest = self.sha256.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise EmbeddedModelError("The selected model has no valid pinned SHA-256 digest.")


def verify_model(path: Path, spec: ModelSpec) -> bool:
    spec.validate()
    try:
        if path.stat().st_size != spec.size_bytes:
            return False
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest() == spec.sha256.lower()
    except OSError:
        return False


def download_model(spec: ModelSpec, cache_dir: Path | None = None, *, progress=None, cancel=None) -> Path:
    """Download, enforce byte bounds, verify digest, then atomically publish inside KubeDock."""
    spec.validate()
    cache_dir = Path(cache_dir) if cache_dir is not None else bundled_model_directory()
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / spec.filename
    if verify_model(destination, spec):
        return destination
    destination.unlink(missing_ok=True)
    fd, name = tempfile.mkstemp(prefix=spec.filename + ".", suffix=".part", dir=str(cache_dir))
    temporary = Path(name)
    digest, received = hashlib.sha256(), 0
    try:
        request = urllib.request.Request(spec.url, headers={"User-Agent": "KubeDock"})
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) != spec.size_bytes:
                raise EmbeddedModelError("Model server returned an unexpected file size.")
            while True:
                if cancel and cancel():
                    raise EmbeddedModelError("Model download cancelled.")
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                received += len(chunk)
                if received > spec.size_bytes or received > MAX_MODEL_BYTES:
                    raise EmbeddedModelError("Model download exceeded its permitted size.")
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(received, spec.size_bytes)
            output.flush()
            os.fsync(output.fileno())
        if received != spec.size_bytes:
            raise EmbeddedModelError("Model download was incomplete; retry the download.")
        if digest.hexdigest() != spec.sha256.lower():
            raise EmbeddedModelError("Model integrity check failed; file was not installed.")
        os.replace(temporary, destination)
        return destination
    except EmbeddedModelError:
        raise
    except Exception as exc:
        raise EmbeddedModelError(f"Unable to download the embedded model: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def generate(model_path: Path, prompt: str, *, max_tokens: int = 1024) -> str:
    """Use the llama_cpp module bundled by the KubeDock application build."""
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise EmbeddedModelError(
            "This KubeDock build does not include the embedded inference runtime yet. "
            "Select an API provider or Ollama."
        ) from exc
    try:
        engine = Llama(model_path=str(model_path), n_ctx=4096,
                       n_threads=max(1, os.cpu_count() or 1), verbose=False)
        result = engine.create_chat_completion(
            messages=[{"role": "user", "content": str(prompt)}],
            max_tokens=max(1, min(int(max_tokens), 2048)), temperature=0.2,
        )
        answer = result["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise EmbeddedModelError("The embedded model returned an empty response.")
        return answer.strip()
    except EmbeddedModelError:
        raise
    except Exception as exc:
        raise EmbeddedModelError(f"Embedded model inference failed: {exc}") from exc
