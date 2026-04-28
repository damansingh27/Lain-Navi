# NAVI — System overview

NAVI is a **Windows desktop voice assistant** that listens for a wake word or a **Shift+L** push-to-talk hotkey, transcribes speech with **Whisper**, reasons with a local **Ollama** LLM (default model `dolphin3`), speaks replies with **Coqui XTTS**, and integrates **Google Calendar**, **local Outlook (COM)**, **DuckDuckGo search**, and a small **JSON memory** store. A **PyQt6** overlay shows `navivis.gif` during listening, processing, and speaking.

This document describes how those pieces connect, which threads run where, and where to change behavior.

---

## 1. Process architecture

### 1.1 Entry points

| Entry | Role |
|--------|------|
| `navi.py` | Main assistant: loads models, runs bootstrap, starts overlay + hotkey + wake threads, runs Qt event loop. |
| `launcher.py` | Optional GUI: can start Ollama/Outlook, run preflight (`runtime_bootstrap`) in the venv, then spawn `navi.py`. |
| `tests/smoke_post_migration.py` | Automated sanity checks for CI or local verification. |

### 1.2 Qt event loop vs worker threads

- **`QApplication.exec()`** runs on the **main thread** in `navi.py`. All **widget and overlay** updates must happen on this thread.
- **`NAVIOverlay`** uses **signals** (`_sig_set_state`, `_sig_force_idle`) so that `set_state()` / `force_idle()` can be called safely from **background threads**; Qt delivers slots on the GUI thread.
- **`hotkey_loop`** (daemon thread): blocks on `keyboard.wait("shift+l")`, records while **L** is held, then calls `process_input()`.
- **`WakeWordListener.listen`** (daemon thread): reads microphone chunks, runs OpenWakeWord; on detection calls `on_wake_word()` which records with VAD then `process_input()`.

`process_input()` therefore often runs on a **worker thread**. It must not touch Qt widgets directly; it only calls `overlay.set_state(...)`, which emits to the main thread.

---

## 2. Startup sequence (`navi.py`)

1. **Load `system_prompt.txt`** from `PROJECT_ROOT` (directory of `navi.py`), not from CWD.
2. **`run_startup_checks()`** (`runtime_bootstrap.py`): import checks for core modules, required/optional assets, CUDA per policy, Ollama HTTP `/api/tags`, and configured model name. On any **critical** failure, NAVI prints diagnostics and **`sys.exit(1)`**.
3. **`DeviceManager`** (`device_manager.py`): selects `cuda` or `cpu` for Whisper and XTTS according to `fallback_policy` (`allow_cpu_fallback` vs `require_cuda` for XTTS).
4. **Model load**: `faster_whisper.WhisperModel("large-v3", ...)` and `TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)`.
5. **Outlook**: if `win32com` import succeeds, mail tools are registered in `TOOLS`; otherwise Outlook tools are omitted.
6. **LangChain** (optional): if `langchain` + Ollama bindings import, an `AgentExecutor` is built with **StructuredTool** wrappers over the same `TOOLS` dict. If not installed, a printed message explains that **keyword fallback** is used instead.

---

## 3. Voice pipeline (one user utterance)

Typical flow after audio is captured to `temp_input.wav`:

1. **Overlay**: `PROCESSING` + reason `transcribing_audio`.
2. **Speaker verification** (optional): if `assets/voice_profile.npy` exists, `verify_speaker()` in `wake_word.py` compares Resemblyzer embeddings; on mismatch the pipeline returns without LLM/TTS.
3. **STT**: `transcribe()` runs Whisper with VAD.
4. **Overlay**: `PROCESSING` + `llm_processing`.
5. **LLM / tools**:
   - **LangChain path**: `AgentExecutor.invoke()` with system prompt + dynamic context + tools.
   - **Fallback**: `_keyword_fallback()` in `navi.py` matches a small set of phrases (calendar today/tomorrow/week, unread mail, latest vs recent email nuance, generic search) and runs the corresponding Python function, returning its **string** result as the “reply.”
   - If no match and LangChain is off, user sees a “tool routing disabled” style message.
6. **Overlay**: `SPEAKING` + `tts_playback`.
7. **TTS**: `speak()` runs XTTS then `sounddevice.play` / `wait` at `TTS_SAMPLE_RATE` (24 kHz).
8. **Overlay**: `finally` in `process_input` sets **IDLE** (`pipeline_complete` or after errors `force_idle`).

---

## 4. Tool registry and integrations

### 4.1 `TOOLS` dict (`navi.py`)

Each entry has `fn`, `desc`, and `args` (for prompt text). LangChain infers schemas from the Python callables when enabled. The same names are documented in `system_prompt.txt` with `[TOOL: name]` style instructions for the model.

### 4.2 Google Calendar (`calendar_tool.py`)

- OAuth **installed application** flow; token refresh with **`RefreshError`** handling to force re-auth when refresh tokens die.
- Functions return **voice-oriented** strings (grouped by day for “this week”) plus CRUD helpers (`create_event`, `delete_event`, `move_event`, `check_freebusy`).

### 4.3 Outlook (`outlook_tool.py`)

- **COM automation** of the desktop Outlook profile (not Microsoft Graph in the current design).
- Inbox items sorted by `ReceivedTime`. **`get_latest_email`** returns one full message; list functions return short previews.

### 4.4 Web (`search_tool.py`)

- **`ddgs`** text search, capped result count, concatenated title/body snippets.

### 4.5 Memory (`memory.py`)

- JSON list in `assets/memory.json` (path resolved from module location).

---

## 5. Overlay state machine (`overlay.py`)

States: **IDLE**, **LISTENING**, **PROCESSING**, **SPEAKING**.

- **IDLE**: window hidden, movie stopped.
- **Non-IDLE**: GIF path resolved from `overlay.py` parent dir, `QMovie` attached to `QLabel`, `show()` + `raise_()`.
- **Watchdog `QTimer`**: per-state timeouts (long for `SPEAKING` to allow full-email TTS); on timeout, overlay resets to IDLE and logs the reason.

---

## 6. Wake word (`wake_word.py`)

- **OpenWakeWord** ONNX model id `WAKE_WORD_MODEL` (default `hey_jarvis`).
- PyAudio **blocking** `read(CHUNK_SIZE)` in a loop; score compared to `DETECTION_THRESHOLD`.
- After a hit: `on_wake` callback, `model.reset()`, drain stream buffer, **sleep** to debounce re-triggers.

---

## 7. Runtime bootstrap (`runtime_bootstrap.py`)

Structured **`BootstrapReport`** / **`Diagnostic`** objects: name, status (`pass`/`warn`/`fail`), critical flag, message, optional remediation. Used at NAVI import time and from **`launcher.run_preflight_checks()`** via `python -c` subprocess with `cwd=WORKSPACE_ROOT`.

---

## 8. Configuration knobs

| Variable / file | Meaning |
|-----------------|--------|
| `OLLAMA_MODEL` | Model tag for Ollama (default `dolphin3`). |
| `OLLAMA_BASE_URL` | Ollama API base (default `http://localhost:11434`). |
| `system_prompt.txt` | Persona + tool routing instructions. |
| `assets/google_credentials.json` | Google OAuth client secret JSON (gitignored). |
| `assets/google_token.json` | OAuth token store (gitignored). |
| `HOTKEY` / `HOLD_KEY` in `navi.py` | Push-to-talk: wait `shift+l`, record while `l` held. |

---

## 9. Dependencies (conceptual)

- **PyQt6**: overlay + launcher UI.
- **faster-whisper**, **torch**: STT.
- **TTS** (Coqui), **torch**: XTTS.
- **sounddevice**, **scipy**, **numpy**: capture + WAV I/O + playback.
- **keyboard**: global hotkey (may need elevated permissions on some Windows setups).
- **google-api-python-client** stack: Calendar.
- **pywin32**: Outlook COM.
- **openwakeword**, **pyaudio**, **resemblyzer**: wake + optional speaker ID.
- **requests**: bootstrap Ollama check.
- **ddgs**: search tool.
- **langchain** (+ `langchain-ollama` or `langchain-community`): optional agent routing.

---

## 10. Repository map (main modules)

| File | Purpose |
|------|---------|
| `navi.py` | Main loop, `TOOLS`, LangChain wiring, keyword fallback, audio + TTS pipeline. |
| `overlay.py` | GIF overlay + state machine + watchdogs. |
| `wake_word.py` | Wake listener + `verify_speaker`. |
| `runtime_bootstrap.py` | Preflight diagnostics. |
| `device_manager.py` | Per-component CUDA/CPU selection + report. |
| `calendar_tool.py` | Google Calendar API. |
| `outlook_tool.py` | Outlook COM mail helpers. |
| `search_tool.py` | DuckDuckGo search. |
| `memory.py` | JSON memory CRUD. |
| `launcher.py` | Start/stop helper UI + preflight subprocess. |
| `start_navi.bat` | Windows launcher for `launcher.py` via venv `pythonw`. |

---

## 11. Security and privacy notes

- **Never commit** `google_credentials.json`, `google_token.json`, voice profiles, or `memory.json` (see `.gitignore`).
- Debug NDJSON logging to the workspace was **removed**; avoid reintroducing file logs that capture transcripts or email bodies without explicit opt-in.
- Outlook and Calendar calls run **as the logged-in Windows / Google user**; treat the machine as trusted.

---

## 12. Operational tips

- Ensure **Ollama** is running and the configured model is pulled before starting NAVI (bootstrap enforces this).
- Run **`python tests/smoke_post_migration.py`** after environment or dependency changes.
- If the overlay GIF does not appear, confirm `assets/navivis.gif` exists and that state transitions are reaching the overlay (console prints `Overlay state: ...`).
