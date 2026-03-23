"""
Voice to Text Mac — Configuration
"""
import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# API Configuration
# =============================================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

WHISPER_MODEL = "whisper-1"
WHISPER_LANGUAGE = None  # Otomatik

SUPPORTED_LANGUAGES = {
    "Otomatik": None,
    "Turkce": "tr",
    "English": "en",
    "Русский": "ru",
}

# =============================================================================
# Hotkey Configuration
# =============================================================================
# Double-tap Option (⌥) to toggle recording
# Double-tap Control to translate to Turkish
# Control+Option combo to translate to English
DOUBLE_TAP_INTERVAL_MS = 300
MAX_TAP_HOLD_MS = 200

# =============================================================================
# Audio Recording Configuration
# =============================================================================
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
MAX_RECORDING_DURATION = 600  # 10 dakika

TEMP_AUDIO_FILE = os.path.join(tempfile.gettempdir(), "voice_to_text_recording.wav")

# =============================================================================
# UI Configuration
# =============================================================================
INDICATOR_WIDTH = 280
INDICATOR_HEIGHT = 56

# macOS system font
INDICATOR_FONT = ("SF Pro Display", 12, "bold")
TIMER_FONT = ("SF Pro Display", 11)

# =============================================================================
# Messages
# =============================================================================
MSG_LISTENING = "Dinliyor"
MSG_PROCESSING = "Isleniyor..."
MSG_SUCCESS = "Tamam"
MSG_ERROR = "Hata"
MSG_NO_API_KEY = "API anahtari bulunamadi! .env dosyasini kontrol edin."
MSG_RECORDING_TOO_SHORT = "Cok kisa"

TRAY_TITLE = "Voice to Text"
TRAY_TOOLTIP = "Option x2 ile kayit baslatin"
MENU_QUIT = "Cikis"
MENU_ABOUT = "Hakkinda"
