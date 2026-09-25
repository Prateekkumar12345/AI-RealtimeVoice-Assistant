from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from utils.audio_recorder import StreamingAudioRecorder, load_capture_script
import os
import shutil
import tempfile
import time
import wave
import psutil
import config

BLOCKING_SCREEN_PHRASES = [
    "you can't join this video call",
    "check your meeting code",
    "this meeting has ended",
    "your account doesn't allow you to join",
]

# Shown when the Chrome profile is signed into a Google account; the bot must never
# join under a personal identity, so it aborts with a clear message instead.
ACCOUNT_CHOOSER_PHRASES = [
    "choose an account",
    "switch account",
    "use another account",
    "sign in to continue",
    "continue to google meet",
    "your organization needs you to sign in",
]

GUEST_NAME_FIELD_CSS = [
    'input[aria-label*="name" i]',
    'input[placeholder*="name" i]',
    'div[role="textbox"][aria-label*="name" i]',
    'input[id="Name"]',
    'div[contenteditable="true"][data-placeholder*="name" i]',
]

TEMP_PROFILE_PREFIX = "meetbot_profile_"


def ensure_silence_wav():
    """A one-second silent WAV. Chrome loops it as the bot's virtual microphone."""
    path = os.path.join(tempfile.gettempdir(), "meetbot_silence.wav")
    if not os.path.exists(path):
        with wave.open(path, "wb") as silence:
            silence.setnchannels(1)
            silence.setsampwidth(2)
            silence.setframerate(48000)
            silence.writeframes(b"\x00\x00" * 48000)
    return path


def close_stale_automation_chrome():
    targets = []
    if config.EPHEMERAL_PROFILE:
        targets.append(TEMP_PROFILE_PREFIX)
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            # Orphaned chromedriver.exe processes never get killed by the
            # chrome.exe match below (chromedriver survives its browser dying),
            # and they keep a local port open that a later session's Selenium
            # commands can silently stall against. Always clear these out.
            if "chromedriver" in name:
                proc.kill()
                continue
            if "chrome" not in name:
                continue
            cmdline = proc.info["cmdline"] or []
            if any(target in arg for arg in cmdline for target in targets):
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


class GoogleMeetBot:
    def __init__(self):
        self.browser = None
        self.audio_recorder = None
        self.meeting_is_active = False
        self.last_error = None
        self.profile_dir = None

    def setup_browser(self):
        close_stale_automation_chrome()
        time.sleep(1)

        if config.EPHEMERAL_PROFILE:
            self.profile_dir = tempfile.mkdtemp(prefix=TEMP_PROFILE_PREFIX)
        else:
            self.profile_dir = None

        browser_options = Options()
        browser_options.add_experimental_option("detach", True)
        browser_options.add_argument("--use-fake-ui-for-media-stream")
        # No echo: the bot's Chrome plays nothing to the speakers, and its microphone
        # is a silent virtual device, so the bot can never feed audio back.
        browser_options.add_argument("--mute-audio")
        browser_options.add_argument("--use-fake-device-for-media-stream")
        browser_options.add_argument(f"--use-file-for-fake-audio-capture={ensure_silence_wav()}")
        browser_options.add_argument("--autoplay-policy=no-user-gesture-required")
        browser_options.add_argument("--start-maximized")
        browser_options.add_argument("--no-sandbox")
        browser_options.add_argument("--disable-dev-shm-usage")
        browser_options.add_argument("--disable-blink-features=AutomationControlled")
        browser_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        browser_options.add_experimental_option("useAutomationExtension", False)
        browser_options.add_argument("--lang=en-US")

        if self.profile_dir:
            browser_options.add_argument(f"--user-data-dir={self.profile_dir}")
            browser_options.add_argument(f"--profile-directory={config.CHROME_PROFILE_DIRECTORY}")

        media_permissions = {
            "intl.accept_languages": "en-US",
            "profile.default_content_setting_values": {
                "media_stream_mic": 1,
                "media_stream_camera": 1,
                "notifications": 2
            }
        }
        browser_options.add_experimental_option("prefs", media_permissions)

        try:
            print("Setting up Chrome browser...")
            self.browser = webdriver.Chrome(options=browser_options)

            self.browser.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
            )
            self.browser.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": load_capture_script()}
            )
            print("Chrome setup successful")
            return True

        except Exception as error:
            print(f"Chrome setup failed: {error}")
            self.last_error = str(error)
            return False

    def join_meeting(self, meeting_url):
        if not self.setup_browser():
            return False
        try:
            print(f"Opening meeting: {meeting_url}")
            self.browser.get(meeting_url)
            time.sleep(5)

            blocking_reason = self.detect_blocking_screen()
            if blocking_reason:
                self.last_error = f"Google Meet would not let the bot in: {blocking_reason}"
                print(self.last_error)
                return False

            account_problem = self.detect_account_chooser()
            if account_problem:
                self.last_error = (
                    "The bot's Chrome is signed into a Google account "
                    f"({account_problem!r}). Google Meet would join under your identity "
                    "instead of as an anonymous guest. The bot now runs in a fresh "
                    "profile, so just retry after closing the bot's Chrome window."
                )
                print(self.last_error)
                return False

            if config.AUTO_CLICK_JOIN:
                print("Configuring audio and video...")
                self.turn_off_microphone()
                self.turn_off_camera()
                if config.BOT_DISPLAY_NAME:
                    self.enter_guest_name()
                if not self.attempt_to_join():
                    self.last_error = self.last_error or (
                        "Could not find a join button. The meeting may require a signed-in "
                        "Google account, or the page took too long to load."
                    )
                    print(self.last_error)
                    return False
            else:
                print("Manual mode: waiting for the bot to be in the meeting room/lobby...")
                if not self.wait_until_in_meeting(timeout=config.JOIN_WAIT_TIMEOUT):
                    self.last_error = (
                        "Timed out waiting for the bot to be inside the meeting "
                        f"({config.JOIN_WAIT_TIMEOUT}s). Make sure you clicked "
                        "'Ask to join' / admitted the bot in its Chrome window."
                    )
                    print(self.last_error)
                    return False
                self.turn_off_microphone()
                self.turn_off_camera()

            print("Meeting joined successfully")
            self.meeting_is_active = True
            time.sleep(3)
            return True

        except Exception as error:
            print(f"Meeting join failed: {error}")
            self.last_error = str(error)
            return False

    def detect_blocking_screen(self):
        try:
            page_text = self.browser.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            return None
        for phrase in BLOCKING_SCREEN_PHRASES:
            if phrase in page_text:
                return phrase
        return None

    def detect_account_chooser(self):
        try:
            page_text = self.browser.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            return None
        for phrase in ACCOUNT_CHOOSER_PHRASES:
            if phrase in page_text:
                return phrase
        return None

    def wait_until_in_meeting(self, timeout=120):
        deadline = time.time() + timeout
        print(f"Watching for the bot to be admitted into the meeting (timeout {int(timeout)}s)...")
        while time.time() < deadline:
            if self._is_in_meeting():
                print("Bot is inside the meeting room")
                return True
            time.sleep(2)
        return False

    def _is_in_meeting(self):
        try:
            for element in self.browser.find_elements(
                By.CSS_SELECTOR,
                "button[aria-label*='Leave call' i], [role='button'][aria-label*='Leave call' i]",
            ):
                if element.is_displayed():
                    return True
        except Exception:
            pass
        try:
            page_text = self.browser.find_element(By.TAG_NAME, "body").text.lower()
            if any(phrase in page_text for phrase in (
                "waiting for the host",
                "you're waiting",
                "you are waiting",
                "waiting to be admitted",
                "to let you in",
                "waiting for host",
            )):
                return True
        except Exception:
            pass
        return False

    def enter_guest_name(self):
        if not config.BOT_DISPLAY_NAME:
            return
        try:
            name_field = WebDriverWait(self.browser, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ", ".join(GUEST_NAME_FIELD_CSS)))
            )
            if not name_field.is_displayed():
                return
            name_field.click()
            name_field.send_keys(config.BOT_DISPLAY_NAME)
            name_field.send_keys(Keys.ENTER)
            print(f"Set bot display name to '{config.BOT_DISPLAY_NAME}'")
        except TimeoutException:
            print("No guest name field found; already joining under a signed-in account")
        except Exception as error:
            print(f"Could not set bot name: {error}")

    def turn_off_microphone(self):
        self._turn_off_device("microphone")

    def turn_off_camera(self):
        self._turn_off_device("camera")

    def _turn_off_device(self, device):
        selectors = [
            f'[aria-label*="Turn off {device}"]',
            f'[aria-label*="{device}" i][data-is-muted="false"]',
        ]
        for selector in selectors:
            try:
                for button in self.browser.find_elements(By.CSS_SELECTOR, selector):
                    if button.is_displayed():
                        button.click()
                        print(f"{device.capitalize()} disabled")
                        time.sleep(1)
                        return
            except Exception:
                continue
        print(f"{device.capitalize()} already off or control not found")

    def attempt_to_join(self):
        deadline = time.time() + 30
        while time.time() < deadline:
            for label in ("ask to join", "join now"):
                button = self._find_join_button(label)
                if button is not None:
                    self._click_join(button, label)
                    return True
            time.sleep(0.5)
        self.last_error = "Could not find a 'Join now' / 'Ask to join' button."
        return False

    def _find_join_button(self, label):
        for element in self.browser.find_elements(By.CSS_SELECTOR, "button, [role='button'], [role='link']"):
            try:
                if not element.is_displayed():
                    continue
                text = (element.text or "").replace("\n", " ").strip().lower()
                if label in text:
                    return element
            except Exception:
                continue
        return None

    def _click_join(self, button, label):
        try:
            button.click()
        except Exception:
            self.browser.execute_script("arguments[0].click();", button)
        print(f"Clicked '{label.title()}'")

    def start_recording(self, session_id):
        if not self.meeting_is_active:
            print("No active meeting to record")
            return False
        try:
            print(f"Starting dual-path recording: {session_id}")
            self.audio_recorder = StreamingAudioRecorder(self.browser, ws_url=config.AUDIO_WS_URL)
            started = self.audio_recorder.start_recording(
                session_id, config.RECORDINGS_DIR, browser_ws_url=config.AUDIO_WS_URL
            )
            if not started:
                print("Recording failed to start")
                return False
            print("Recording started (WAV + live streaming)")
            return True
        except Exception as error:
            print(f"Recording error: {error}")
            return False

    def stop_recording(self):
        audio_file_path = None
        if self.audio_recorder:
            print("Stopping recording...")
            audio_file_path = self.audio_recorder.stop_recording()
            print(f"Audio saved: {audio_file_path}")
        return audio_file_path

    def leave_meeting(self):
        print("Leaving meeting...")
        try:
            self.find_and_click_leave_button()
        except Exception as error:
            print(f"Could not leave gracefully: {error}")
        finally:
            if self.browser:
                self.browser.quit()
                print("Browser closed")
                self.meeting_is_active = False
            if self.profile_dir and os.path.isdir(self.profile_dir):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                print("Temporary bot profile removed")

    def find_and_click_leave_button(self):
        leave_button_selectors = [
            '[aria-label*="Leave call"]',
            '[data-testid*="leave"]',
            'button[aria-label*="Leave call"]'
        ]
        for selector in leave_button_selectors:
            try:
                leave_button = self.browser.find_element(By.CSS_SELECTOR, selector)
                if leave_button.is_displayed():
                    leave_button.click()
                    print("Left meeting via button")
                    break
            except Exception:
                continue