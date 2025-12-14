# configuration.py

# Two-letter codes
LANGUAGES = ["en","de", "fr", "it"]

# Where to write the result (folder will be created if missing)
OUTPUT_DIR = "translated_drawio"

# Optional: source language of your labels
SOURCE_LANG = "en"

# Optional: whether to overwrite existing label_xx/value_xx attributes if present
OVERWRITE_EXISTING = True

'''
DEEPL_API_KEY: DeepL API key (preferred backend)
DEEPL_API_URL: optional override endpoint
LIBRETRANSLATE_URL: e.g. https://libretranslate.yourdomain.tld (recommended to self-host); public endpoints exist but are rate-limited
LIBRETRANSLATE_API_KEY: if your LibreTranslate requires a key
USE_GOOGLETRANS=1 to enable the googletrans last-resort fallback (off by default)
'''
USE_GOOGLETRANS=1



# Translation engine for the 'translators' package: e.g., "google", "bing", "deepl", "alibaba", ...
TRANSLATOR_ENGINE = "google"

# Timeout (seconds) for translation requests
TRANSLATOR_TIMEOUT = 20

# Optional HTTP(S) proxies for the translators client (None or a dict)
# Example: {"http": "http://proxy.local:8080", "https": "http://proxy.local:8080"}
TRANSLATOR_PROXIES = None