"""Debounced structured-field extraction from committed transcript tails.

The whole point of batching is NOT to call the LLM on every tiny partial. Sarvam
emits `transcript.partial` many times per utterance; those are only shown live.
A segment is only committed on `transcript.final`. We then wait `LLM_DEBOUNCE_SECONDS`
of silence so a burst of consecutive finals settles into one call, and send only
the *unconsumed tail* of the transcript:

    partial  "My..."
    partial  "My name is..."
    partial  "My name is Prateek..."
    final    "My name is Prateek Kumar."      <-- committed
    (1.5s of quiet)
    LLM  ->  [{"field":"name","value":"Prateek Kumar"}]

Provider: by default Sarvam's Chat Completion API (`sarvam-105b` at
api.sarvam.ai/v1), authenticated with the same SARVAM_API_KEY used for
streaming STT. Set LLM_PROVIDER=openai to target any OpenAI-compatible
/chat/completions endpoint with LLM_BASE_URL/LLM_API_KEY instead. When no key at
all is configured, a built-in regex extractor (name, age, weight, email, phone)
is used so the flow still works offline.
"""
import json
import re
import threading
import time

import config


class FieldExtractor:
    def __init__(self, manager, on_fields=None, enabled=None):
        self.manager = manager
        self.on_fields = on_fields or (lambda fields: None)
        self.enabled = config.LLM_EXTRACTION_ENABLED if enabled is None else enabled
        self._lock = threading.Lock()
        self._gen = 0
        self._last_schedule = 0.0
        self._wake = threading.Event()
        self._busy = threading.Event()  # set while an extraction is in flight
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- scheduling
    def schedule(self):
        """Ask the extractor to run once the transcript has gone quiet."""
        if not self.enabled:
            return
        with self._lock:
            self._gen += 1
            self._last_schedule = time.time()
        self._wake.set()

    def flush_now(self):
        """Force an extraction immediately (e.g. when stopping the session)."""
        if not self.enabled:
            return
        with self._lock:
            self._gen += 1
            self._last_schedule = 0.0  # due immediately
        self._wake.set()

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def wait_idle(self, timeout=10.0):
        """Block until no extraction is pending/in-flight (used at session stop)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                active = self._busy.is_set() or self._gen > 0
            if not active:
                return True
            time.sleep(0.1)
        return False

    # ----------------------------------------------------------------- run
    def _run(self):
        while not self._stop_event.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            if self._stop_event.is_set():
                return
            with self._lock:
                gen = self._gen
                due = self._last_schedule == 0.0 or (
                    self._last_schedule > 0
                    and (time.time() - self._last_schedule) >= config.LLM_DEBOUNCE_SECONDS
                )
                if due:
                    self._gen = 0
            if not due or gen == 0:
                continue
            text = self.manager.pending_text()
            if text:
                self._busy.set()
                try:
                    fields = self.extract(text)
                    self.manager.mark_extracted()
                    if fields:
                        self.on_fields(fields)
                except Exception as error:
                    print(f"Field extraction failed: {error}")
                finally:
                    self._busy.clear()

    # ------------------------------------------------------------- extractors
    def extract(self, text):
        if self._llm_available():
            return self._llm_extract(text) or self._regex_extract(text)
        return self._regex_extract(text)

    def _llm_available(self):
        if config.LLM_PROVIDER == "sarvam":
            # Sarvam's chat API authenticates with the same subscription key
            # used for streaming STT, so one key powers the whole pipeline.
            return bool(config.SARVAM_API_KEY or config.LLM_API_KEY)
        return bool(config.LLM_API_KEY)

    def _llm_extract(self, text):
        import httpx

        api_key = config.LLM_API_KEY or config.SARVAM_API_KEY
        is_sarvam = "sarvam.ai" in config.LLM_BASE_URL or config.LLM_PROVIDER == "sarvam"
        headers = {"Content-Type": "application/json"}
        if is_sarvam:
            # Sarvam's documented auth header; it also accepts Bearer, but
            # api-subscription-key works for every Sarvam API.
            headers["api-subscription-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        user_prompt = (
            f"Extract fields from this meeting transcript.\n\n"
            f"Schema: {config.LLM_FIELD_SCHEMA}\n\n"
            "Return ONLY a JSON array of the fields you found, e.g. "
            '[{"field": "name", "value": "Asha Rao", "confidence": 0.95}]. '
            "Omit any field the speaker did not state.\n\n"
            f"Transcript:\n{text[:12000]}"
        )
        payload = {
            "model": config.LLM_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": config.LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        # Sarvam thinks by default; the thinking tokens count against
        # max_tokens and can leave the JSON array empty on short replies.
        if config.LLM_DISABLE_THINKING and is_sarvam:
            payload["reasoning_effort"] = None
        with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
            response = client.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return self._parse_fields(content)

    @staticmethod
    def _parse_fields(content):
        """Robustly parse the LLM's JSON array (handles markdown fences and prose)."""
        if not content:
            return []
        cleaned = re.sub(r"```(?:json)?", "", content).strip()
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            parsed = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return []
        fields = []
        for item in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            value = str(item.get("value") or "").strip()
            if field and value:
                try:
                    confidence = float(item.get("confidence", 1.0))
                except (TypeError, ValueError):
                    confidence = 1.0
                fields.append({"field": field, "value": value, "confidence": confidence})
        return fields

    @staticmethod
    def _regex_extract(text):
        """Offline fallback so the demo works without an LLM key."""
        fields = []

        name = re.search(
            r"\b(?:my name is|i am called|this is|myself)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
            text,
        )
        if name:
            fields.append({"field": "name", "value": name.group(1).strip(), "confidence": 0.7, "source": "regex"})

        age = re.search(
            r"\b(?:i am|i'm|my age is|age is|aged)\s+(\d{1,3})\s*(?:years old|yrs old|years|yrs)?\b",
            text,
            re.IGNORECASE,
        )
        if age:
            value = int(age.group(1))
            if 1 <= value <= 120:
                fields.append({"field": "age", "value": f"{value} years", "confidence": 0.7, "source": "regex"})

        weight = re.search(
            r"\b(?:i weigh|my weight is|weight is)\s+(\d{2,3}(?:\.\d)?)\s*(?:kg|kgs|kilos|kilograms)?\b",
            text,
            re.IGNORECASE,
        )
        if weight:
            value = float(weight.group(1))
            if 1 <= value <= 500:
                fields.append({"field": "weight", "value": f"{value:g} kg", "confidence": 0.7, "source": "regex"})

        email = re.search(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}", text)
        if email:
            fields.append({"field": "email", "value": email.group(0), "confidence": 0.8, "source": "regex"})

        compact = re.sub(r"[\s\-]+", "", text)
        phone = re.search(r"(?:\+?91)?[6-9]\d{9}", compact)
        if phone:
            fields.append({"field": "phone", "value": phone.group(0), "confidence": 0.7, "source": "regex"})

        return fields