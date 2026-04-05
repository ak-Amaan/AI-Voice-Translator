# optimized_main.py  (paste into your main.py)
import os
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk
import time

import speech_recognition as sr
from deep_translator import GoogleTranslator
from google.transliteration import transliterate_text

# =========================
# TUNABLE PARAMETERS
# =========================

# How many seconds max to record per audio chunk (None => rely on pause_threshold)
PHRASE_TIME_LIMIT = 3

# Silence threshold (seconds) before cutting the phrase when phrase_time_limit is None
PAUSE_THRESHOLD = 0.5

# Mic sensitivity
ENERGY_THRESHOLD = 300

# Size of audio buffer queue (how many audio chunks can wait while processing)
AUDIO_QUEUE_MAXSIZE = 6

# Whether to use macOS 'say' (fast, local) for TTS. True recommended for macOS.
USE_MAC_SAY = True

# Language map (name -> code)
language_codes = {
    "English": "en",
    "Hindi": "hi",
    "Bengali": "bn",
    "Spanish": "es",
    "Chinese (Simplified)": "zh-CN",
    "Russian": "ru",
    "Japanese": "ja",
    "Korean": "ko",
    "German": "de",
    "French": "fr",
    "Tamil": "ta",
    "Telugu": "te",
    "Kannada": "kn",
    "Gujarati": "gu",
    "Punjabi": "pa",
}

# =========================
# UI Setup (Tkinter)
# =========================

win = tk.Tk()
win.geometry("760x520")
win.title("Real-Time Voice Translator — Optimized")
icon = tk.PhotoImage(file="icon.png")
win.iconphoto(False, icon)

tk.Label(win, text="Recognized Text ⮯").pack()
input_text = tk.Text(win, height=8, width=85)
input_text.pack()

tk.Label(win, text="Translated Text ⮯").pack()
output_text = tk.Text(win, height=8, width=85)
output_text.pack()

frame = tk.Frame(win)
frame.pack(pady=8)

tk.Label(frame, text="Input Lang:").grid(row=0, column=0)
input_lang_cb = ttk.Combobox(frame, values=list(language_codes.keys()), width=20)
input_lang_cb.set("English")
input_lang_cb.grid(row=0, column=1, padx=6)

tk.Label(frame, text="Output Lang:").grid(row=0, column=2)
output_lang_cb = ttk.Combobox(frame, values=list(language_codes.keys()), width=20)
output_lang_cb.set("English")
output_lang_cb.grid(row=0, column=3, padx=6)

start_btn = tk.Button(win, text="Start Translation")
start_btn.pack(side="left", padx=14, pady=6)

kill_btn = tk.Button(win, text="Kill Translation")
kill_btn.pack(side="left", padx=14, pady=6)

status_label = tk.Label(win, text="Idle")
status_label.pack(side="right", padx=8)

# =========================
# Shared Queues & Flags
# =========================

audio_queue = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)  # holds sr.AudioData objects
ui_queue = queue.Queue()  # thread-safe queue for UI updates (tuples)
running_flag = threading.Event()  # when set, listener & processor should run
stop_now_flag = threading.Event()  # immediate stop request (Kill button)

# =========================
# Recognizers & Mic (one mic used by listener only)
# =========================

listener_recognizer = sr.Recognizer()
listener_recognizer.energy_threshold = ENERGY_THRESHOLD
listener_recognizer.pause_threshold = PAUSE_THRESHOLD

processor_recognizer = sr.Recognizer()  # separate recognizer for recognition step
processor_recognizer.energy_threshold = ENERGY_THRESHOLD
processor_recognizer.pause_threshold = PAUSE_THRESHOLD

microphone = sr.Microphone()  # the single microphone object (shared by listener thread)

# =========================
# Helper: safe UI update from worker threads
# =========================

def push_ui(kind: str, text: str):
    """kind: 'input' or 'output' or 'status'"""
    ui_queue.put((kind, text))


def process_ui_queue():
    """Called on main thread periodically to apply UI updates."""
    try:
        while True:
            kind, text = ui_queue.get_nowait()
            if kind == "input":
                input_text.insert(tk.END, text + "\n")
                input_text.see(tk.END)
            elif kind == "output":
                output_text.insert(tk.END, text + "\n")
                output_text.see(tk.END)
            elif kind == "status":
                status_label.config(text=text)
    except queue.Empty:
        pass
    # schedule next poll
    win.after(100, process_ui_queue)


# =========================
# Listener Thread: Capture audio chunks and enqueue them
# =========================

def listener_thread_fn():
    """Continuously listens and puts sr.AudioData into audio_queue."""
    # One-time ambient adjustment
    with microphone as source:
        try:
            listener_recognizer.adjust_for_ambient_noise(source, duration=0.8)
        except Exception:
            pass

    # Now loop capturing audio
    while running_flag.is_set() and not stop_now_flag.is_set():
        try:
            with microphone as source:
                # Print prompt once on start
                push_ui("status", "Listening... (Speak Now!)")
                # Listen: either limited by PHRASE_TIME_LIMIT or by pause_threshold
                if PHRASE_TIME_LIMIT:
                    audio = listener_recognizer.listen(source, phrase_time_limit=PHRASE_TIME_LIMIT)
                else:
                    audio = listener_recognizer.listen(source)

                # Put audio into queue (drop oldest if full)
                try:
                    audio_queue.put(audio, timeout=0.5)
                except queue.Full:
                    try:
                        _ = audio_queue.get_nowait()  # drop one oldest
                    except queue.Empty:
                        pass
                    try:
                        audio_queue.put_nowait(audio)
                    except queue.Full:
                        # give up on this chunk
                        pass
        except Exception as e:
            push_ui("output", f"Listener error: {e}")
            # on unexpected listener error, stop
            running_flag.clear()
            break

    push_ui("status", "Listener stopped")


# =========================
# Processor Thread: consume audio, perform STT, translate, TTS
# =========================

def processor_thread_fn():
    """Consume queued audio chunks and process them."""
    # single translator instance not required but we create local references
    translator = GoogleTranslator

    while running_flag.is_set() and not stop_now_flag.is_set():
        try:
            audio = audio_queue.get(timeout=1.0)
            # if we receive None as sentinel, treat it as a wakeup and break
            if audio is None:
                audio_queue.task_done()
                # if running_flag cleared, exit; otherwise continue waiting
                if not running_flag.is_set():
                    break
                else:
                    continue

        except queue.Empty:
            # if queue is empty and running_flag cleared, break
            if not running_flag.is_set():
                break
            else:
                continue

        # STT using processor_recognizer
        try:
            # For STT language, map selected input language name to code
            in_name = input_lang_cb.get()
            src_code = language_codes.get(in_name, "en")
            # Use 'auto' fallback when user chose a value not in map
            if src_code in (None, ""):
                recognized = processor_recognizer.recognize_google(audio)
            else:
                # For some languages, Google expects e.g. "en-IN" — this is a simple pass
                recognized = processor_recognizer.recognize_google(audio, language=src_code)
        except sr.UnknownValueError:
            push_ui("input", "Could not understand!")
            audio_queue.task_done()
            continue
        except sr.RequestError as e:
            push_ui("output", f"STT request error: {e}")
            # fatal network issue: stop processing to avoid spam
            running_flag.clear()
            break
        except Exception as e:
            push_ui("output", f"STT unexpected error: {e}")
            running_flag.clear()
            break

        # Transliteration (if needed)
        try:
            if src_code not in ("en", "auto"):
                try:
                    translit = transliterate_text(recognized, lang_code=src_code)
                except Exception:
                    translit = recognized
            else:
                translit = recognized
        except Exception:
            translit = recognized

        push_ui("input", translit)

        # Allow spoken stop
        if recognized.strip().lower() in {"exit", "stop"}:
            running_flag.clear()
            push_ui("status", "Stop command received")
            audio_queue.task_done()
            break

        # Translation (network)
        try:
            out_name = output_lang_cb.get()
            tgt_code = language_codes.get(out_name, "en")
            translated = translator(source=src_code if src_code != "auto" else "auto", target=tgt_code).translate(text=translit)
        except Exception as e:
            push_ui("output", f"Translation error: {e}")
            # Break loop on repeated translator errors
            running_flag.clear()
            audio_queue.task_done()
            break

        push_ui("output", translated)

        # TTS - use macOS 'say' for speed (no network)
        try:
            if USE_MAC_SAY:
                # 'say' defaults to system voice, if language not supported it will approximate
                subprocess.call(["say", translated])
            else:
                # Fallback: generate mp3 using gTTS (slower) then play via afplay
                from gtts import gTTS
                script_dir = os.path.dirname(os.path.abspath(__file__))
                voice_path = os.path.join(script_dir, "voice.mp3")
                tts = gTTS(translated, lang=tgt_code)
                tts.save(voice_path)
                subprocess.call(["afplay", voice_path])
        except Exception as e:
            push_ui("output", f"TTS error: {e}")
            running_flag.clear()
            audio_queue.task_done()
            break

        audio_queue.task_done()

    push_ui("status", "Processor stopped")


# =========================
# Start / Stop control from UI
# =========================

listener_thread = None
processor_thread = None

# Put these definitions into your file (replace existing start/stop functions)

def watch_threads_and_update_ui(listener_t, processor_t):
    """Run on background thread: wait for worker threads to finish then update UI."""
    # Wait a reasonable time (but not forever)
    # We poll every 0.2s so the UI remains responsive
    timeout_seconds = 10.0
    start_time = time.time()
    while True:
        alive = False
        if listener_t is not None and listener_t.is_alive():
            alive = True
        if processor_t is not None and processor_t.is_alive():
            alive = True
        if not alive:
            break
        if time.time() - start_time > timeout_seconds:
            # give up waiting after timeout; still report stopped to user
            break
        time.sleep(0.2)

    push_ui("status", "Translation stopped")


def start_translation():
    global listener_thread, processor_thread
    if running_flag.is_set():
        return
    stop_now_flag.clear()
    running_flag.set()
    push_ui("status", "Starting...")
    listener_thread = threading.Thread(target=listener_thread_fn, daemon=True)
    processor_thread = threading.Thread(target=processor_thread_fn, daemon=True)
    listener_thread.start()
    processor_thread.start()


def stop_translation():
    """Request stop, try to wake worker threads, then spawn watcher to confirm stop."""
    # 1) Immediate stop request
    stop_now_flag.set()
    running_flag.clear()
    push_ui("status", "Stopping...")

    # 2) Try to wake up the processor if it's blocked on an empty queue
    try:
        # put a dummy small AudioData-like object to unblock processor.get()
        # We use None as sentinel; processor_thread_fn must handle queue items that are None.
        # If you don't want to send None, instead do: audio_queue.put_nowait(dummy) guarded by try/except
        audio_queue.put_nowait(None)
    except Exception:
        pass

    # 3) Start a watcher thread to show "Translation stopped" when threads exit
    watcher = threading.Thread(target=watch_threads_and_update_ui, args=(listener_thread, processor_thread), daemon=True)
    watcher.start()


# Hook up UI
start_btn.config(command=start_translation)
kill_btn.config(command=stop_translation)

# Start UI polling of ui_queue
process_ui_queue()

# Start Tk mainloop
win.mainloop()
