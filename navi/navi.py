"""
NAVI desktop voice assistant — main entrypoint.

Orchestrates: PyQt overlay, push-to-talk hotkey, wake-word thread, Whisper STT,
Ollama chat (with optional LangChain tool routing), Coqui XTTS playback, and
Python-side integrations (Google Calendar, local Outlook COM, DuckDuckGo, JSON memory).

Threads: `keyboard` waits on Shift+L in a daemon thread; wake-word listener runs
in another daemon thread. Both call `process_input()` which must not touch Qt
widgets directly — overlay state changes go through Qt signals inside NAVIOverlay.
"""

import sys
import threading
import sounddevice as sd
import scipy.io.wavfile as wav
import numpy as np
import keyboard
import queue
import os
from pprint import pformat
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from faster_whisper import WhisperModel
from TTS.api import TTS
from overlay import NAVIOverlay, OverlayState
from wake_word import WakeWordListener, verify_speaker
from runtime_bootstrap import run_startup_checks
from device_manager import DeviceManager, DeviceSelectionError
from memory import remember, recall, forget, list_memories
from calendar_tool import (get_events_today, get_events_tomorrow,
                           get_events_this_week, create_event, delete_event,
                           move_event, check_freebusy)
from search_tool import search_web

LANGCHAIN_AVAILABLE = False
LANGCHAIN_IMPORT_ERROR = None
try:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.tools import StructuredTool
    try:
        from langchain_ollama import ChatOllama
    except Exception:
        from langchain_community.chat_models import ChatOllama
    LANGCHAIN_AVAILABLE = True
except Exception as exc:
    LANGCHAIN_IMPORT_ERROR = str(exc)
    LANGCHAIN_AVAILABLE = False

try:
    from outlook_tool import (get_unread_emails, get_emails_from_sender,
                              get_recent_emails, get_latest_email,
                              read_email, read_last_email,
                              send_email, create_draft,
                              flag_email, move_email, mark_all_read)
    OUTLOOK_AVAILABLE = True
except Exception:
    OUTLOOK_AVAILABLE = False

# ── CONFIG ──────────────────────────────────────────────────────
MODEL_NAME = os.getenv("OLLAMA_MODEL", "dolphin3")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_URL = f"{OLLAMA_BASE_URL.rstrip('/')}/api/chat"
SAMPLE_RATE = 16000
HOTKEY = "shift+l"
HOLD_KEY = "l"
PROJECT_ROOT = Path(__file__).resolve().parent
# True when a Resemblyzer voice profile exists (same path wake_word.py loads).
SPEAKER_VERIFY = (PROJECT_ROOT / "assets" / "voice_profile.npy").exists()
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "system_prompt.txt"

BASE_SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

# ── STARTUP BOOTSTRAP ────────────────────────────────────────────
bootstrap_report = run_startup_checks(
    model_name=MODEL_NAME,
    ollama_base_url=OLLAMA_BASE_URL,
    require_cuda=True,
)
print("Startup diagnostics:")
print(bootstrap_report.format_for_console())
if not bootstrap_report.ok:
    print("\nBlocking startup due to critical bootstrap failures.")
    print(pformat(bootstrap_report.to_dict(), sort_dicts=False))
    sys.exit(1)

# ── DEVICE SELECTION ─────────────────────────────────────────────
device_manager = DeviceManager()
try:
    whisper_device = device_manager.select_for_component(
        component="whisper",
        cuda_compute_type="float16",
        cpu_compute_type="int8",
        fallback_policy="allow_cpu_fallback",
    )
    xtts_device = device_manager.select_for_component(
        component="xtts",
        cuda_compute_type=None,
        cpu_compute_type=None,
        fallback_policy="require_cuda",
    )
except DeviceSelectionError as exc:
    print(f"Device selection failed: {exc}")
    print(pformat(device_manager.build_report().to_dict(), sort_dicts=False))
    sys.exit(1)

device_runtime_report = device_manager.build_report()
print("Device runtime report:")
for selection in device_runtime_report.selections.values():
    level = "OK" if selection.status == "pass" else "WARN"
    print(
        f"[{level}] {selection.component}: device={selection.device}, "
        f"compute_type={selection.compute_type}, policy={selection.policy}, "
        f"fallback={selection.used_fallback}"
    )
print(
    "CUDA summary: "
    f"available={device_runtime_report.cuda_available}, "
    f"device_count={device_runtime_report.device_count}, "
    f"torch_cuda={device_runtime_report.torch_cuda_version}"
)

# ── LOAD MODELS ─────────────────────────────────────────────────
print("Loading Whisper...")
whisper = WhisperModel(
    "large-v3",
    device=whisper_device.device,
    compute_type=whisper_device.compute_type or "float16",
)

print("Loading TTS...")
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(xtts_device.device)

if SPEAKER_VERIFY:
    print("Speaker verification enabled.")
else:
    print("No voice profile found — speaker verification disabled.")

print("NAVI ready.")

# ── TOOL REGISTRY ────────────────────────────────────────────────
TOOLS = {
    "get_events_today": {
        "fn": get_events_today,
        "desc": "Get today's calendar events",
        "args": [],
    },
    "get_events_tomorrow": {
        "fn": get_events_tomorrow,
        "desc": "Get tomorrow's calendar events",
        "args": [],
    },
    "get_events_this_week": {
        "fn": get_events_this_week,
        "desc": "Get this week's calendar events",
        "args": [],
    },
    "create_event": {
        "fn": create_event,
        "desc": "Create a calendar event",
        "args": ["summary", "start_datetime (ISO 8601)", "end_datetime (ISO 8601)"],
    },
    "delete_event": {
        "fn": delete_event,
        "desc": "Delete a calendar event by name",
        "args": ["event_summary"],
    },
    "move_event": {
        "fn": move_event,
        "desc": "Move an upcoming calendar event by summary",
        "args": [
            "event_summary",
            "new_start_datetime (ISO 8601, local tz if omitted)",
            "new_end_datetime (ISO 8601, local tz if omitted)",
        ],
    },
    "check_freebusy": {
        "fn": check_freebusy,
        "desc": "Check busy windows in a date-time range",
        "args": [
            "start_datetime (ISO 8601, local tz if omitted)",
            "end_datetime (ISO 8601, local tz if omitted)",
        ],
    },
    "remember": {
        "fn": remember,
        "desc": "Store a fact or preference in long-term memory",
        "args": ["information"],
    },
    "recall": {
        "fn": recall,
        "desc": "Retrieve stored memories about a topic",
        "args": ["query"],
    },
    "forget": {
        "fn": forget,
        "desc": "Remove memories containing a topic",
        "args": ["topic"],
    },
    "search_web": {
        "fn": search_web,
        "desc": "Search the web for current information, news, or anything not in memory/calendar/email",
        "args": ["query"],
    },
}

if OUTLOOK_AVAILABLE:
    TOOLS.update({
        "get_latest_email": {
            "fn": get_latest_email,
            "desc": (
                "The single most recent inbox message only — use for "
                "'most recent / latest / top email' (one message, not a list)"
            ),
            "args": [],
        },
        "get_recent_emails": {
            "fn": get_recent_emails,
            "desc": "List several recent emails with previews — only if user asks for a list / multiple",
            "args": [],
        },
        "get_unread_emails": {
            "fn": get_unread_emails,
            "desc": "List unread emails from Outlook inbox",
            "args": [],
        },
        "read_email": {
            "fn": read_email,
            "desc": "Read the full contents of a specific email by subject keyword",
            "args": ["subject_query"],
        },
        "read_last_email": {
            "fn": read_last_email,
            "desc": "Re-read the last accessed email (use when user says 'read it' or 'read that')",
            "args": [],
        },
        "get_emails_from_sender": {
            "fn": get_emails_from_sender,
            "desc": "Get recent emails from a specific sender",
            "args": ["sender_name"],
        },
        "send_email": {
            "fn": send_email,
            "desc": "Send an email via Outlook",
            "args": ["to_address", "subject", "body"],
        },
        "create_draft": {
            "fn": create_draft,
            "desc": "Create an Outlook draft email",
            "args": ["to_address", "subject", "body"],
        },
        "flag_email": {
            "fn": flag_email,
            "desc": "Flag an Outlook message by its EntryID",
            "args": ["message_id", "flag_status"],
        },
        "move_email": {
            "fn": move_email,
            "desc": "Move an Outlook message by EntryID to a mailbox folder",
            "args": ["message_id", "destination_folder"],
        },
        "mark_all_read": {
            "fn": mark_all_read,
            "desc": "Mark all inbox emails as read",
            "args": [],
        },
    })

def _tool_arg_names(info):
    return [a.split("(")[0].strip() for a in info["args"]]


def _tool_call_fallback(**kwargs):
    return "No handler available."


def _create_langchain_tools():
    """
    Converts the existing TOOL registry into LangChain StructuredTool instances.
    """
    lc_tools = []
    for name, info in TOOLS.items():
        fn = info.get("fn") or _tool_call_fallback
        desc = info.get("desc", name)
        try:
            lc_tools.append(
                StructuredTool.from_function(
                    func=fn,
                    name=name,
                    description=desc,
                    infer_schema=True,
                )
            )
        except Exception as e:
            print(f"Skipping LangChain tool '{name}': {e}")
    return lc_tools


def _build_agent_executor():
    """
    Creates the LangChain agent executor and returns (executor, disabled_reason).
    """
    if not LANGCHAIN_AVAILABLE:
        reason = LANGCHAIN_IMPORT_ERROR or "LangChain packages are not installed."
        return None, reason

    try:
        llm = ChatOllama(
            model=MODEL_NAME,
            base_url=OLLAMA_BASE_URL,
            temperature=0,
        )
        lc_tools = _create_langchain_tools()
        if not lc_tools:
            return None, "No tools available to register with LangChain."

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "{system_prompt}\n\n{dynamic_context}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
                ("human", "{input}"),
            ]
        )
        agent = create_tool_calling_agent(llm=llm, tools=lc_tools, prompt=prompt)
        executor = AgentExecutor(agent=agent, tools=lc_tools, verbose=False, handle_parsing_errors=True)
        return executor, None
    except Exception as e:
        return None, f"Failed to build LangChain agent executor: {e}"


def _execute_tool(name, args):
    """Runs a registered tool and returns its result as a string."""
    if name not in TOOLS:
        return f"Unknown tool: {name}"
    try:
        return str(TOOLS[name]["fn"](**args))
    except Exception as e:
        return f"Tool error: {e}"


def _keyword_fallback(user_message):
    """
    Deterministic fallback when LangChain is unavailable or a tool-call fails.
    Routes only high-confidence intents that require no extra arguments.
    """
    msg = (user_message or "").lower()
    if "calendar" in msg or "schedule" in msg:
        if "tomorrow" in msg:
            return _execute_tool("get_events_tomorrow", {})
        if "week" in msg:
            return _execute_tool("get_events_this_week", {})
        if "today" in msg:
            return _execute_tool("get_events_today", {})
    if "unread" in msg and "email" in msg and "get_unread_emails" in TOOLS:
        return _execute_tool("get_unread_emails", {})
    # "most recent email" contains "recent email" as a substring — check single-inbox
    # intents first so we never return a 5-mail list for a singular question.
    if "get_recent_emails" in TOOLS and (
        "most recent emails" in msg
        or "most recent e-mails" in msg
    ):
        return _execute_tool("get_recent_emails", {})
    if "get_latest_email" in TOOLS and "email" in msg:
        if "most recent email" in msg or "most recent e-mail" in msg:
            return _execute_tool("get_latest_email", {})
        if (
            "latest email" in msg
            or "newest email" in msg
            or "top email" in msg
        ):
            return _execute_tool("get_latest_email", {})
    if "get_recent_emails" in TOOLS and (
        "recent emails" in msg
        or "recent e-mails" in msg
    ):
        return _execute_tool("get_recent_emails", {})
    if "get_recent_emails" in TOOLS and "recent email" in msg and "most recent" not in msg:
        return _execute_tool("get_recent_emails", {})
    if ("search" in msg or "look up" in msg or "what is" in msg) and "search_web" in TOOLS:
        return _execute_tool("search_web", {"query": user_message})
    return None


AGENT_EXECUTOR, AGENT_DISABLED_REASON = _build_agent_executor()
if AGENT_EXECUTOR is None:
    print(f"LangChain routing disabled: {AGENT_DISABLED_REASON}")
else:
    print("LangChain routing enabled.")


def _build_tool_block():
    """Formats the available-tools section for the system prompt."""
    lines = [
        "You have access to tools. To use one, respond with ONLY a tool call in this exact format:",
        '[TOOL: tool_name {"arg1": "value1"}]',
        "If the tool takes no arguments use: [TOOL: tool_name]",
        "Only call ONE tool per response. Do NOT add extra text around a tool call.",
        "If no tool is needed, respond normally.",
        "",
        "Available tools:",
    ]
    for name, info in TOOLS.items():
        if info["args"]:
            args_str = ", ".join(info["args"])
            lines.append(f"  {name}({args_str}) — {info['desc']}")
        else:
            lines.append(f"  {name}() — {info['desc']}")
    return "\n".join(lines)

_TOOL_BLOCK = _build_tool_block()


def _build_context():
    """Returns dynamic context (time, memories) injected each turn."""
    parts = [f"Current date/time: {datetime.now().strftime('%A, %B %d, %Y %I:%M %p')}"]
    memories = recall("")
    if memories:
        parts.append(f"Dom's stored memories:\n{memories}")
    return "\n\n".join(parts)


# ── AUDIO CAPTURE ───────────────────────────────────────────────
def record_while_held(hold_key, out_path="temp_input.wav"):
    """
    Push-to-talk capture: after global HOTKEY fires, user holds `hold_key` (e.g. L);
    samples are queued from sounddevice until the key is released, then written as WAV.
    """
    print("Listening...")
    q = queue.Queue()
    chunks = []

    def callback(indata, frames, time_info, status):
        if status:
            print(status)
        q.put(indata.copy())

    blocksize = int(0.05 * SAMPLE_RATE)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=blocksize, callback=callback):
        while keyboard.is_pressed(hold_key):
            try:
                chunks.append(q.get(timeout=0.2))
            except queue.Empty:
                pass
        # drain any remaining buffers
        while not q.empty():
            chunks.append(q.get_nowait())

    if not chunks:
        print("No audio captured.")
        return None

    audio = np.concatenate(chunks, axis=0)
    duration = len(audio) / SAMPLE_RATE
    print(f"Captured {duration:.1f}s of audio.")

    if duration < 0.3:
        print("Recording too short, skipping.")
        return None

    wav.write(out_path, SAMPLE_RATE, audio)
    return out_path

def record_until_silence(out_path="temp_input.wav", silence_thresh=400,
                         silence_duration=1.5, max_duration=30):
    """
    Wake-word path: record after trigger until RMS stays below `silence_thresh`
    for `silence_duration` seconds (post speech), or until `max_duration`.
    """
    print("Listening (speak now)...")
    q = queue.Queue()
    chunks = []

    def callback(indata, frames, time_info, status):
        if status:
            print(status)
        q.put(indata.copy())

    blocksize = int(0.05 * SAMPLE_RATE)
    silent_chunks = 0
    silent_chunks_needed = int(silence_duration / 0.05)
    heard_speech = False
    total_chunks = 0
    max_chunks = int(max_duration / 0.05)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=blocksize, callback=callback):
        while total_chunks < max_chunks:
            try:
                chunk = q.get(timeout=0.2)
            except queue.Empty:
                continue
            chunks.append(chunk)
            total_chunks += 1
            rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))
            if rms > silence_thresh:
                heard_speech = True
                silent_chunks = 0
            else:
                silent_chunks += 1
            if heard_speech and silent_chunks >= silent_chunks_needed:
                break

    if not chunks:
        print("No audio captured.")
        return None

    audio = np.concatenate(chunks, axis=0)
    duration = len(audio) / SAMPLE_RATE
    print(f"Captured {duration:.1f}s of audio.")

    if duration < 0.3 or not heard_speech:
        print("No speech detected, skipping.")
        return None

    wav.write(out_path, SAMPLE_RATE, audio)
    return out_path

def transcribe(audio_path):
    """Run faster-whisper on a mono 16 kHz WAV path; VAD trims leading/trailing silence."""
    segments, _ = whisper.transcribe(audio_path, vad_filter=True, beam_size=1)
    return " ".join([s.text for s in segments]).strip()

def ask_ollama(user_message):
    """
    Prefer LangChain `AgentExecutor` when installed; otherwise `_keyword_fallback`
    for a small set of intents; if neither applies, return a disabled message.
    """
    system = f"{BASE_SYSTEM_PROMPT}\n\n{_TOOL_BLOCK}\n\n{_build_context()}"
    if AGENT_EXECUTOR is not None:
        try:
            result = AGENT_EXECUTOR.invoke(
                {
                    "input": user_message,
                    "system_prompt": BASE_SYSTEM_PROMPT,
                    "dynamic_context": _build_context(),
                }
            )
            output = result.get("output", "")
            if output:
                return output
        except Exception as e:
            print(f"LangChain routing error: {e}")
            fallback_result = _keyword_fallback(user_message)
            if fallback_result is not None:
                return str(fallback_result)
            # deterministic final fallback, no tools
            return "Tool routing is temporarily unavailable."

    fallback_result = _keyword_fallback(user_message)
    if fallback_result is not None:
        return str(fallback_result)
    return f"Tool routing disabled: {AGENT_DISABLED_REASON or 'unknown reason'}"

TTS_SAMPLE_RATE = 24000

def speak(text):
    """Synthesize `text` with XTTS and block until sounddevice playback finishes."""
    print(f"NAVI: {text}")
    audio = tts.tts(text=text, speaker='Ana Florence', language="en")
    audio_np = np.array(audio, dtype=np.float32)
    sd.play(audio_np, TTS_SAMPLE_RATE)
    sd.wait()

def process_input(audio_path, overlay):
    """
    End-to-end utterance handler: optional speaker gate, Whisper, LLM/tools, TTS.
    Always returns overlay to IDLE in `finally` (after speak completes or on skip).
    """
    try:
        if SPEAKER_VERIFY and not verify_speaker(audio_path):
            print("Speaker not recognized — ignoring.")
            return

        overlay.set_state(OverlayState.PROCESSING, "transcribing_audio")
        user_text = transcribe(audio_path)
        if user_text:
            print(f"You: {user_text}")
            overlay.set_state(OverlayState.PROCESSING, "llm_processing")
            response = ask_ollama(user_text)
            overlay.set_state(OverlayState.SPEAKING, "tts_playback")
            speak(response)
        else:
            print("No command heard — returning to standby.")
    except Exception as e:
        print(f"Pipeline error: {e}")
        overlay.force_idle("pipeline_error")
    finally:
        overlay.set_state(OverlayState.IDLE, "pipeline_complete")


def hotkey_loop(overlay):
    """Push-to-talk: hold hotkey to record, release to process."""
    print(f"Hold {HOTKEY} to speak to NAVI.")
    while True:
        keyboard.wait(HOTKEY)
        overlay.set_state(OverlayState.LISTENING, "push_to_talk_pressed")
        audio_path = None
        try:
            audio_path = record_while_held(HOLD_KEY)
        except Exception as e:
            print(f"Recording error: {e}")
            overlay.force_idle("record_error")
            continue
        if audio_path is None:
            overlay.set_state(OverlayState.IDLE, "no_audio_captured")
            continue
        process_input(audio_path, overlay)


def on_wake_word(overlay):
    """Callback when wake word is detected."""
    print("Wake word detected — recording command...")
    overlay.set_state(OverlayState.LISTENING, "wake_word_detected")
    try:
        audio_path = record_until_silence()
    except Exception as e:
        print(f"Wake-word recording error: {e}")
        overlay.force_idle("wake_record_error")
        return
    if audio_path is None:
        overlay.set_state(OverlayState.IDLE, "wake_no_audio")
        return
    process_input(audio_path, overlay)


if __name__ == "__main__":
    import signal
    from PyQt6.QtCore import QTimer

    app = QApplication(sys.argv)
    overlay = NAVIOverlay()

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    # Periodic no-op timer so the Qt event loop processes Windows signals (Ctrl+C).
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(200)

    hotkey_thread = threading.Thread(
        target=hotkey_loop, args=(overlay,), daemon=True)
    hotkey_thread.start()

    wake_listener = WakeWordListener(
        on_wake=lambda: on_wake_word(overlay))
    wake_thread = threading.Thread(
        target=wake_listener.listen, daemon=True)
    wake_thread.start()

    print("Activation: hold Shift+L (push-to-talk) or say the wake word.")
    sys.exit(app.exec())
