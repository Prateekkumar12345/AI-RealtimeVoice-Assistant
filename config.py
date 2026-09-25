import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(PROJECT_ROOT, "assets", "recordings")
SESSIONS_DIR = os.path.join(PROJECT_ROOT, "data", "sessions")
FRONTEND_DIST = os.path.join(PROJECT_ROOT, "frontend", "dist")

# ---------------------------------------------------------------------------
# Chrome / Google Meet bot profile (kept from the batch project)
# ---------------------------------------------------------------------------
# The bot must NOT join under your personal Google identity. By default it runs in a
# fresh, throwaway Chrome profile every time (not signed into any account), so Google
# Meet sees a guest and either puts it in the lobby ("Ask to join") or lets it in as a
# guest named BOT_DISPLAY_NAME — never as you.
#   - To use a dedicated bot Google account instead, set CHROME_USER_DATA_DIR to a
#     folder and sign in once inside the bot's Chrome window.
#   - Set EPHEMERAL_PROFILE=false to fall back to the persistent chrome_profile/ folder.
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR")
EPHEMERAL_PROFILE = (
    CHROME_USER_DATA_DIR is None
    and os.getenv("EPHEMERAL_PROFILE", "true").strip().lower() in ("1", "true", "yes")
)
CHROME_PROFILE_DIRECTORY = os.getenv("CHROME_PROFILE_DIRECTORY", "Default")
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "").strip()

# AUTO_CLICK_JOIN=true: the bot clicks "Ask to join" / "Join now" itself.
# AUTO_CLICK_JOIN=false (default): manual mode — the bot waits to be admitted and
# recording starts automatically as soon as it is inside the meeting room.
AUTO_CLICK_JOIN = os.getenv("AUTO_CLICK_JOIN", "false").strip().lower() in ("1", "true", "yes")
# Manual mode: how long (seconds) to wait for the bot to be admitted into the meeting.
JOIN_WAIT_TIMEOUT = int(os.getenv("JOIN_WAIT_TIMEOUT", "120") or "120")

# ---------------------------------------------------------------------------
# Sarvam streaming STT
# ---------------------------------------------------------------------------
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()

# "realtime" -> /speech-to-text-realtime/ws  (saaras:v3-realtime / saaras:v4, true partials)
# "legacy"   -> /speech-to-text/ws          (saaras:v3 / saaras:v4, final per utterance)
SARVAM_STREAMING_ENDPOINT = os.getenv("SARVAM_STREAMING_ENDPOINT", "realtime").strip().lower()
SARVAM_STREAMING_MODEL = os.getenv("SARVAM_STREAMING_MODEL", "saaras:v3-realtime").strip()
SARVAM_STREAM_TYPE = os.getenv("SARVAM_STREAM_TYPE", "balanced").strip()  # fast | balanced | simulated
SARVAM_STREAMING_MODE = os.getenv("SARVAM_STREAMING_MODE", "transcribe").strip()
SARVAM_LANGUAGE_CODE = os.getenv("SARVAM_LANGUAGE_CODE", "auto").strip()
# "auto" maps to the adaptive language detection on the realtime endpoint.
SARVAM_REALTIME_LANGUAGE = "auto" if SARVAM_LANGUAGE_CODE in ("", "unknown", "auto") else SARVAM_LANGUAGE_CODE

SARVAM_VAD_THRESHOLD = float(os.getenv("SARVAM_VAD_THRESHOLD", "0.3"))
SARVAM_SILENCE_DURATION_MS = int(os.getenv("SARVAM_SILENCE_DURATION_MS", "500"))
SARVAM_MIN_SPEECH_DURATION_MS = int(os.getenv("SARVAM_MIN_SPEECH_DURATION_MS", "250"))
SARVAM_PING_INTERVAL = int(os.getenv("SARVAM_PING_INTERVAL", "10"))  # seconds; avoids 1008 inactivity close

# ---------------------------------------------------------------------------
# Audio / recording path (kept from the batch project: raw WAV is the source of truth)
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
WAV_HEADER_SIZE = 44
CHUNK_BYTES = 32000  # ~1s of 16 kHz mono 16-bit PCM

# The WebSocket inside the FastAPI server that receives audio chunks from the bot.
# The bot and server live in the same process by default; the connection is still a
# real WebSocket so the transport is identical to a remotely-hosted bot later.
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))
AUDIO_WS_URL = os.getenv(
    "AUDIO_WS_URL",
    f"ws://{SERVER_HOST}:{SERVER_PORT}/ws/audio",
).strip()

# ---------------------------------------------------------------------------
# LLM field extraction (form filling)
# ---------------------------------------------------------------------------
LLM_EXTRACTION_ENABLED = os.getenv("LLM_EXTRACTION_ENABLED", "true").strip().lower() in ("1", "true", "yes")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
LLM_DEBOUNCE_SECONDS = float(os.getenv("LLM_DEBOUNCE_SECONDS", "1.5"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "30"))

# Schema asked of the LLM. Write it as instructions; the LLM returns a JSON array
# of {field, value, confidence}. Empty -> free-form extraction of anything notable.
LLM_FIELD_SCHEMA = os.getenv(
    "LLM_FIELD_SCHEMA",
    "Extract personal/contact details people give during the meeting: "
    "full_name, email, phone, company, city, request/action_item. "
    "Only return fields that were actually spoken.",
).strip()

LLM_SYSTEM_PROMPT = os.getenv(
    "LLM_SYSTEM_PROMPT",
    "You extract structured fields from meeting transcripts. "
    "Answer with a single JSON array only, no markdown fences: "
    '[{"field": "...", "value": "...", "confidence": 0.0}]',
).strip()

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "").strip()  # empty -> JSON-file fallback storage
MONGO_DB = os.getenv("MONGO_DB", "voicebot")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "sessions")


def create_project_directories():
    for directory in (RECORDINGS_DIR, SESSIONS_DIR):
        os.makedirs(directory, exist_ok=True)


create_project_directories()