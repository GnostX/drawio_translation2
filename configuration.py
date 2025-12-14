# configuration.py

# Two-letter codes
LANGUAGES = ["de", "fr", "it"]

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