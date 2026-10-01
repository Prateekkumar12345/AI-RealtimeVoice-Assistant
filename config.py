import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(PROJECT_ROOT, "assets", "recordings")
# Screenshots + page text of every Meet page the bot was shown when a join failed.
# "You can't join this video call" is Meet refusing the bot's identity; without
# the page text you cannot tell which of its several reasons it was.
DIAGNOSTICS_DIR = os.path.join(PROJECT_ROOT, "assets", "join_diagnostics")
SESSIONS_DIR = os.path.join(PROJECT_ROOT, "data", "sessions")
FRONTEND_DIST = os.path.join(PROJECT_ROOT, "frontend", "dist")

# ---------------------------------------------------------------------------
# Chrome / Google Meet bot profile (kept from the batch project)
# ---------------------------------------------------------------------------
# The bot must NOT join under your personal Google identity. By default it runs in a
# fresh, throwaway Chrome profile every time (not signed into any account), so Google
# Meet sees a guest and either puts it in the lobby ("Ask to join") or lets it in as a
# guest named BOT_DISPLAY_NAME — never as you.
#   - To use a dedicated bot Google account, set CHROME_USER_DATA_DIR to a
#     dedicated folder and sign into it manually before starting the bot.
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "").strip() or None
EPHEMERAL_PROFILE = (
    CHROME_USER_DATA_DIR is None
    and os.getenv("EPHEMERAL_PROFILE", "true").strip().lower() in ("1", "true", "yes")
)
CHROME_PROFILE_DIRECTORY = os.getenv("CHROME_PROFILE_DIRECTORY", "Default")
# Defaults to a real name so the bot never sits in the participant list as a
# blank/"Anonymous" guest. Still fully overridable via .env.
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "AI Notetaker").strip()

# AUTO_CLICK_JOIN=true (default): the bot types its guest name, turns its own
# mic/camera off, and clicks "Ask to join" / "Join now" itself — no admin action
# needed in the bot's Chrome window at all.
# AUTO_CLICK_JOIN=false: manual mode — the bot waits to be admitted and
# recording starts automatically as soon as it is inside the meeting room.
AUTO_CLICK_JOIN = os.getenv("AUTO_CLICK_JOIN", "true").strip().lower() in ("1", "true", "yes")
# Manual mode: how long (seconds) to wait for the bot to be admitted into the meeting.
JOIN_WAIT_TIMEOUT = int(os.getenv("JOIN_WAIT_TIMEOUT", "120") or "120")
# How long Meet may take to commit to a page state after the URL is opened.
PREJOIN_LOAD_TIMEOUT = int(os.getenv("PREJOIN_LOAD_TIMEOUT", "45") or "45")
# How long the bot keeps looking for a usable 'Join now' / 'Ask to join' button.
JOIN_BUTTON_TIMEOUT = int(os.getenv("JOIN_BUTTON_TIMEOUT", "30") or "30")
# How many times to reload Meet and retry the whole join. A refusal is a verdict
# on the bot's identity, so a retry only helps for the transient cases (lobby
# bounce, stale page); the real fix is the meeting's access settings.
JOIN_ATTEMPTS = max(1, int(os.getenv("JOIN_ATTEMPTS", "2") or "2"))
# Save a screenshot + the page text every time a join fails.
CAPTURE_JOIN_DIAGNOSTICS = os.getenv("CAPTURE_JOIN_DIAGNOSTICS", "true").strip().lower() in ("1", "true", "yes")
# Hard stop: refuse to try the join unless the bot's Chrome profile is signed
# into a Google account. Use this once the meetings you must join are all
# invite-only / domain-restricted, where an anonymous guest is always refused.
REQUIRE_SIGNED_IN_PROFILE = os.getenv("REQUIRE_SIGNED_IN_PROFILE", "false").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Zero-intervention Google sign-in
# ---------------------------------------------------------------------------
# The bot signs itself in with no human step. Two mechanisms cooperate:
#
#   1. SESSION_VAULT_DIR -- a ~180 KB copy of the 8 Chrome files that actually
#      hold the Google session (Local State's DPAPI key + Default/Network/Cookies
#      + Preferences + 5 others). Chrome unlocks the cookies itself on startup,
#      so nothing here is ever decrypted by the bot. Once the vault is filled,
#      every later run joins with no password involved at all.
#
#   2. GOOGLE_EMAIL/GOOGLE_PASSWORD -- used ONLY when the vault is empty or
#      Google has expired the session (roughly once a year; the SID cookies on
#      a live profile run to Oct 2027). Google often refuses a scripted
#      sign-in with a CAPTCHA, so this is a recovery path, not a daily
#      dependency. If it is refused, the bot says so plainly instead of
#      hanging or silently joining as an anonymous guest.
#
# The password is deliberately read WITHOUT .strip(): leading/trailing spaces
# are significant characters in a password, and every other secret in this file
# is stripped. Truncating it here would make the bot unloginable.
GOOGLE_EMAIL = (os.getenv("GOOGLE_EMAIL") or "").strip()
GOOGLE_PASSWORD = os.getenv("GOOGLE_PASSWORD") or ""
# Zero-intervention is ON by default: with credentials present the bot signs
# itself in rather than waiting for a human to admit it.
AUTO_LOGIN = os.getenv("AUTO_LOGIN", "true").strip().lower() in ("1", "true", "yes")
# Where the session vault lives. Empty disables the vault (password login only).
# No implicit default: a missing/blank value must actually turn the vault off, or
# it silently takes priority over CHROME_USER_DATA_DIR and the bot seeds itself
# from a vault that may hold no usable account.
SESSION_VAULT_DIR = os.getenv("SESSION_VAULT_DIR", "").strip()
# How long to wait for each Google sign-in field before giving up.
LOGIN_FIELD_TIMEOUT = int(os.getenv("LOGIN_FIELD_TIMEOUT", "30") or "30")
# How long to wait for Google to land us back on a signed-in page.
LOGIN_SETTLE_TIMEOUT = int(os.getenv("LOGIN_SETTLE_TIMEOUT", "25") or "25")
# Overrides undetected-chromedriver's unreliable ChromeDriver version guess.
# Leave empty to read the real version from the registry; set it only if
# auto-detection fails (chrome://version shows the major).
CHROME_VERSION_MAIN = (os.getenv("CHROME_VERSION_MAIN") or "").strip()

# ---------------------------------------------------------------------------
# Recording health
# ---------------------------------------------------------------------------
# The bot never touches anyone's physical mic — it only captures the remote
# WebRTC audio tracks that OTHER participants' mics actually send. If every
# participant stays muted there is nothing to record; that isn't fixable from
# the bot's side. What we CAN automate is noticing it fast: if no remote audio
# track/sound has been seen this long after recording starts (or since audio
# last stopped), the UI shows a clear "no audio detected" warning instead of
# silently recording nothing.
NO_AUDIO_WARNING_SECONDS = int(os.getenv("NO_AUDIO_WARNING_SECONDS", "20") or "20")

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
# Speaker attribution ("who spoke?")
# ---------------------------------------------------------------------------
# Meet mixes every participant into one stream, so speaker numbers are derived
# from per-participant voice activity (see utils/speaker_registry.py) rather
# than by sending a separate speech-to-text stream per person.
SPEAKER_ATTRIBUTION = str(os.getenv("SPEAKER_ATTRIBUTION", "true")).strip().lower() in (
    "1", "true", "yes", "on",
)

# ---------------------------------------------------------------------------
# LLM field extraction (form filling)
# ---------------------------------------------------------------------------
# Provider precedence: LLM_PROVIDER=sarvam (default) reuses the existing
# SARVAM_API_KEY with Sarvam's /v1/chat/completions; "openai" targets any
# OpenAI-compatible endpoint via LLM_API_KEY / LLM_BASE_URL.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "sarvam").strip().lower()
LLM_EXTRACTION_ENABLED = os.getenv("LLM_EXTRACTION_ENABLED", "true").strip().lower() in ("1", "true", "yes")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.sarvam.ai/v1").strip().rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "sarvam-105b").strip()
LLM_DEBOUNCE_SECONDS = float(os.getenv("LLM_DEBOUNCE_SECONDS", "1.5"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "30"))
# Sarvam thinks by default; the thinking tokens count against max_tokens and can
# leave the JSON array empty on short replies (finish_reason="length").
LLM_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "true").strip().lower() in ("1", "true", "yes")

# Schema asked of the LLM. Write it as instructions; the LLM returns a JSON array
# of {field, value, confidence}. The field names must match the canonical keys the
# form renders (name / age / weight) or the values land in the extras strip
# instead of the inputs.
LLM_FIELD_SCHEMA = os.getenv(
    "LLM_FIELD_SCHEMA",
    "Extract these personal details only if actually spoken: "
    "name (person's name), age (years), weight (kg). "
    "Include the unit in the value for measurements (e.g. '72.5 kg'). "
    "Never guess or compute values; omit fields that were not mentioned.",
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
    for directory in (RECORDINGS_DIR, SESSIONS_DIR, DIAGNOSTICS_DIR):
        os.makedirs(directory, exist_ok=True)


create_project_directories()