"""
config.py
---------
All tunable settings for the Amy assistant live in one place.

Paste your Gemini API key directly below (GEMINI_API_KEY). Get a free one at:
https://aistudio.google.com/apikey

⚠️ Since the key lives in this file now, don't commit this file to a public
git repo or share it with anyone -- treat it like a password.
"""

import os

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
ASSISTANT_NAME = "Amy"

# Who built Amy. The assistant is told to give this answer whenever anyone asks
# who made / created / developed / trained / programmed it (in any language).
CREATOR_NAME = "Salman Azarm"
CREATOR_DESCRIPTION_EN = "Salman Azarm, a 12th-grade computer student"
CREATOR_DESCRIPTION_FA = "سلمان آذرم، دانش‌آموز کلاس دوازدهم رشته‌ی کامپیوتر"

# Words/phrases that wake the assistant up from passive listening.
# Lowercase, no punctuation. Persian + English variants included. Matching is
# done on whole words, so "ایمیل" (email) or "family" will NOT wake her up.
WAKE_WORDS = [
    "amy", "hey amy", "ami", "amie", "aimee",
    "ایمی", "آمی", "امی", "هی ایمی", "هی امی",
]

# ---------------------------------------------------------------------------
# Gemini Live API
# ---------------------------------------------------------------------------
# 👇 PASTE YOUR KEY HERE (between the quotes) 👇
GEMINI_API_KEY = "PASTE_YOUR_GEMINI_API_KEY_HERE"

# (If an env var is also set it wins, so you can override the hardcoded key
# temporarily without editing this file -- but you never have to set one.)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or GEMINI_API_KEY

# The Live API model changes fairly often as Google ships new previews.
# If this model 404s, check https://ai.google.dev/gemini-api/docs/live-api
# for the current native-audio-dialog model name and swap it here.
GEMINI_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

# Native Gemini voices are single words like "Kore", "Aoede", "Puck", "Charon".
# "Aoede" and "Kore" both read as natural female voices.
GEMINI_VOICE = "Aoede"

# Spoken/response language hint passed in the system instruction.
ASSISTANT_LANGUAGE_HINT = "Persian (Farsi) and English, whichever the user speaks"

# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------
MIC_SAMPLE_RATE = 16000      # what we send to Gemini
SPEAKER_SAMPLE_RATE = 24000  # what Gemini sends back
CHANNELS = 1
CHUNK_SIZE = 1024

# ---- Echo protection ------------------------------------------------------
# Laptop speakers + mic = Amy hears her own voice and treats it as YOU talking
# (that's what made her get confused). While she is speaking, the microphone
# is muted (silence is sent instead) so she can't hear herself.
#   True  -> no echo, but you can't interrupt her by voice while she talks
#            (use the Stop button). Best for speakers.
#   False -> allows talking over her (barge-in). Only use with headphones.
MUTE_MIC_WHILE_AMY_SPEAKS = True
# Extra seconds to keep the mic muted after she stops (room echo / speaker lag).
ECHO_TAIL_SECONDS = 0.6

# Seconds of silence after which an open conversation closes and Amy goes back
# to waiting for her name. 0 = never auto-close.
SESSION_IDLE_TIMEOUT = 60

# ---------------------------------------------------------------------------
# Wake word engine
# ---------------------------------------------------------------------------
# "vosk" = fully offline, recommended (small model, no cloud calls)
# "google" = uses SpeechRecognition's free Google Web Speech endpoint (needs internet)
WAKE_WORD_ENGINE = "google"

# Path to a Vosk model directory if WAKE_WORD_ENGINE == "vosk"
# Download a small model from https://alphacephei.com/vosk/models
VOSK_MODEL_PATH = "models/vosk-model-small-en-us"

# Seconds of silence that end a listening turn once the user starts talking.
SILENCE_TIMEOUT = 1.2

# Energy (mean absolute amplitude, int16 scale) above which audio counts as
# "someone is talking" rather than background noise. None = auto-calibrate
# from ~1s of ambient room noise at startup (recommended).
VAD_THRESHOLD = None

# ---------------------------------------------------------------------------
# System control
# ---------------------------------------------------------------------------
# Regexes (case-insensitive, matched against the whitespace-normalised command).
# Commands matching any of these are ALWAYS rejected, even if the model asks and
# even if you'd click "Yes". Keep this list even if you trust yourself -- models
# can be tricked by things they read on screen or in a file.
DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\b(?=[^|;&]*\s-[a-z]*r)[^|;&]*\s(/|/\*|~|~/|\$home|\*)(\s|$)",  # rm -r / , ~ , *
    r"\bmkfs(\.\w+)?\b",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",                       # fork bomb
    r"(^|[;&|]\s*)(sudo\s+)?(shutdown|reboot|poweroff|halt)\b",
    r"\binit\s+[06]\b",
    r"\bsystemctl\s+(poweroff|reboot|halt)\b",
    r"\b(stop|restart)-computer\b",
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bdel\s+(/[fsq]\s*)+[a-z]:\\",
    r"\b(rd|rmdir)\s+(/[sq]\s*)+[a-z]:\\",
    r"\bremove-item\b.*-recurse.*\s[a-z]:\\?(\s|$)",
    r"\bdd\s+if=",
    r"\bof=/dev/(sd|nvme|hd|disk)",
    r">\s*/dev/(sd|nvme|hd|disk)",
    r"\breg\s+delete\b",
    r"\bbcdedit\b",
    r"\bchmod\s+-r\s+\S+\s+/(\s|$)",
    r"\bchown\s+-r\s+\S+\s+/(\s|$)",
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|pwsh|powershell)\b",  # pipe-to-shell
    r"\|\s*iex\b",
    r"\binvoke-expression\b",
]

# How Amy asks before doing something DANGEROUS. Ordinary tasks (open things, create
# files, search, type, click, harmless commands...) never ask -- she just does them.
#   "voice"  -> she asks out loud; only YOUR spoken "yes / آره / باشه / تایید ..." (as
#               transcribed from your microphone) lets it run. Text on screen or the
#               model itself can never approve anything. Say "no / نه / نکن" to cancel.
#   "dialog" -> a Yes/No popup window instead.
CONFIRM_METHOD = "voice"
# Seconds a pending question stays valid before it must be asked again.
CONFIRM_TIMEOUT_SECONDS = 90

# Master switch for the risky-action check below. Turn off only if you fully
# understand the risk (then nothing except the blocked list above is stopped).
CONFIRM_RISKY_ACTIONS = True

# Terminal commands whose FIRST word is one of these need a spoken yes first
# (delete / kill / registry / permissions / disk tools ...). Best-effort: it
# recognises the usual suspects, it cannot understand every possible command.
RISKY_COMMAND_WORDS = [
    "rm", "del", "erase", "rmdir", "rd", "unlink", "shred", "remove-item", "ri",
    "taskkill", "stop-process", "kill", "pkill", "killall",
    "reg", "regedit", "sc", "net", "netsh", "icacls", "takeown", "cacls", "schtasks", "wmic",
    "cipher", "bcdedit", "diskpart", "format", "fsutil", "vssadmin", "wevtutil",
    "chmod", "chown", "mkfs", "dd", "truncate", "su", "runas",
]
# ...and so does a command that matches any of these anywhere.
RISKY_COMMAND_PATTERNS = [
    r"\buninstall\b",
    r"\bapt(-get)?\s+(remove|purge|autoremove)\b",
    r"\bgit\s+(reset\s+--hard|clean\b)",
    r"\bgit\s+push\b.*(\s-f\b|--force)",
    r"\bdrop\s+(table|database)\b",
    r"\bset-executionpolicy\b",
    r"-recurse\b",
]
# Python code run through run_python_code that uses any of these needs a spoken yes.
RISKY_PYTHON_PATTERNS = [
    r"\bshutil\.(rmtree|move)\b", r"\bos\.(remove|unlink|rmdir|removedirs|system|popen|kill|killpg|_exit|rename|replace)\b",
    r"\.(unlink|rmdir)\(", r"\bsend2trash\b", r"\bsubprocess\b", r"\bctypes\b", r"\bwinreg\b",
    r"\.(kill|terminate)\(", r"\bshutdown\b", r"\b__import__\b", r"\beval\(", r"\bexec\(",
]

# Allow Amy to shut down / restart / sleep the computer or kill processes at
# all. Even when True, these always need your spoken yes first.
ALLOW_POWER_ACTIONS = True
ALLOW_PROCESS_CONTROL = True

# Let Amy drive the keyboard and mouse (type, press keys, click, scroll) so she can
# do things no dedicated tool covers. Typing something that looks like a risky
# terminal command still needs your spoken yes.
ALLOW_INPUT_CONTROL = True

# Send deleted files to the Recycle Bin / Trash instead of erasing them forever
# (needs `send2trash`; if it's missing Amy falls back to permanent deletion).
USE_RECYCLE_BIN = True

# How many results to return from a filesystem-wide search before stopping.
FILE_SEARCH_MAX_RESULTS = 25
# How many seconds a filesystem-wide search may run before giving up.
FILE_SEARCH_TIMEOUT = 12

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 560
ACCENT_COLOR = "#7C4DFF"      # violet
ACCENT_COLOR_2 = "#00E5FF"    # cyan
BG_COLOR = "rgba(12, 12, 20, 235)"
