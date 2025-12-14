# drawio-label-translator
Matthias Günter, matthias.guenter@gnostx.ch

Translate text in draw.io (.drawio) diagrams into multiple languages and embed the translations so they are visible in diagrams.net’s “Edit Data…” panel and compatible with the translate-diagram helper.

To make use of the translations use: https://app.diagrams.net/?ui=kennedy&translate-diagram=1
The way to switch then between languages and how to handle the translations is discussed here: https://www.drawio.com/blog/translate-diagrams 

This tool can:
- Detect the primary language of each page (diagram) automatically
- Generate translations only for the languages you specify
- Treat English specially: if “en” is one of your target languages, the visible text becomes English (no label_en/value_en keys), and the original language is preserved under label_<src> (and with a code change also in label-<src>)
- Add translations to the right place (UserObject), so you can see and manage them in “Edit Data…”
- Handle both compressed and uncompressed pages and multi-page files
- Process a single file or all .drawio files in a folder
- Provide a small web UI (drag-and-drop) to translate and download the result

Contents
- translate_drawio.py — CLI for translating labels in .drawio files
- server.py — Flask server with a drag-and-drop upload page
- configuration.py — your configuration file (languages, output directory, options)
- pyproject.toml — project metadata, dependencies, mypy config
- README.md — this document

Features at a glance
- Per-page primary language detection (langdetect)
- Translation backend: “translators” library (supports google, bing, deepl, etc.)
- Writes translation keys onto UserObject so they appear in “Edit Data…”
- Writes both underscore and hyphen variants: label_de and label-de
- Keeps outputs in a specified output folder with the same filename as input
- Folder mode: process all .drawio files in a directory (non-recursive)
- Optionally emit uncompressed inner XML for inspection

How it works
- The tool finds visible texts in shapes (typically in mxCell@value or @label).
- It detects the page’s primary language from a sample of visible texts.
- It wraps text-bearing mxCell nodes in a UserObject if needed (this is how diagrams.net stores custom data you see in “Edit Data…”).
- It writes translation attributes to the UserObject:
  - label_xx (and in future also for label-xx) for each configured language
  - If English is in your language list and the diagram isn’t in English:
    - The visible text (UserObject@label) is set to the English translation
    - The original language text is preserved under label_<src> (and label-<src>)
    - No label_en/value_en are created; English is stored as the base label/value
- It preserves page compression when the diagram was originally compressed (unless you ask for uncompressed output for inspection).

Requirements
- Python 3.10+
- Dependencies (installed via pyproject.toml):
  - translators (translation backend)
  - langdetect (language detection)
  - requests
  - Flask (for the optional web server)

Installation
Option A: install as an editable package using pyproject.toml
- Create and activate a virtual environment
- From the project root:
  - pip install -e .

Option B: install dependencies directly
- pip install translators langdetect requests Flask

Configuration
Create configuration.py in the project directory. Example:

```python
# configuration.py

# Two-letter language codes. Only these will be generated.
# Example assumes diagrams primarily in German should be translated to English, French, Italian.
LANGUAGES = ["en", "de", "fr", "it"]

# Output directory for translated files (will be created if missing)
OUTPUT_DIR = "translated_drawio"

# Fallback/default source language if detection fails
SOURCE_LANG = "en"

# If True, existing keys like label_de will be overwritten unless --nooverwrite is passed
OVERWRITE_EXISTING = True

# Translators backend config
# Supported engines vary; common ones include "google", "bing", "deepl".
TRANSLATOR_ENGINE = "google"
TRANSLATOR_TIMEOUT = 20
# Optional HTTP(S) proxies for translators backend, e.g.:
# TRANSLATOR_PROXIES = {"http": "http://proxy.local:8080", "https": "http://proxy.local:8080"}
TRANSLATOR_PROXIES = None
```

Usage (CLI)
Translate a single file:
- python translate_drawio.py path/to/diagram.drawio

Translate all .drawio files in a folder (non-recursive):
- python translate_drawio.py path/to/folder

Options:
- --nooverwrite
  - Do not overwrite existing label_xx/label-xx keys
- --uncompressed
  - Write page XML uncompressed (useful for inspecting the inner XML)
- --out-name NAME
  - Override output filename (ignored when input is a folder). By default, the output filename is the same as the input filename and is written to OUTPUT_DIR.

Output location and naming
- All outputs are written to configuration.OUTPUT_DIR
- By default the output file name equals the input file name (no suffix changes)
- For folder inputs, each .drawio file is processed into a same-named file inside OUTPUT_DIR

Web server (drag-and-drop)
Run:
- python server.py
- Open http://127.0.0.1:5000
- Drag-and-drop a .drawio file and download the translated output
- Endpoints:
  - GET / — upload page
  - POST /translate — returns the translated file as attachment
  - GET /healthz — health check

Behavior details

1) Primary language detection
- For each page (<diagram>), the tool collects up to 100 label/value texts and uses langdetect to determine the primary language, with a deterministic seed.
- If detection fails, SOURCE_LANG from configuration.py is used.

2) English as the base label/value
- If "en" is in LANGUAGES:
  - If the page’s primary language is English:
    - English stays as the visible base text (UserObject@label), and no label_en/value_en are created.
  - If the page’s primary language is not English:
    - The base visible text is replaced with English (UserObject@label).
    - The original language’s text is preserved in label_<src> and label-<src>.
- For other languages in LANGUAGES (e.g., de, fr, it), the tool writes label_xx and label-xx attributes containing translations from the detected primary language.

3) Where translations are stored
- Translations are written onto a UserObject that wraps the mxCell. This is how diagrams.net exposes data in the “Edit Data…” dialog.
- If a shape isn’t already wrapped, the tool creates a UserObject wrapper, preserving structure:
  - The wrapper keeps the original ID so edges/groups still refer to the same element
  - The inner mxCell loses its id/value/label; geometry, style, and other attributes are preserved

4) Attribute names
- The tool writes both underscore and hyphen variants to maximize compatibility:
  - label_de and label-de, label_fr and label-fr, etc.
- It focuses on label_* keys because UserObject@label corresponds to visible text. If you need parallel value_* keys, that can be enabled on request.

Examples

- Diagram primarily in German (detected), LANGUAGES = ["en","de","fr","it"]
  - Base text set to English (UserObject@label = English)
  - Preserve German under label_de and label-de
  - Generate French label_fr/label-fr and Italian label_it/label-it
  - No label_en keys created

- Diagram primarily in English (detected), LANGUAGES = ["en","fr"]
  - Base English text unchanged (no label_en)
  - Create label_fr/label-fr with French translation

Troubleshooting

- I don’t see any translations in “Edit Data…”
  - Ensure you’re opening the translated output file from OUTPUT_DIR
  - The tool writes onto UserObject; if a shape didn’t show data before, the tool should have wrapped it automatically
  - If you still don’t see keys, try View > XML to confirm label_xx/label-xx are present. If missing, run with --uncompressed for easier inspection and share a small sample

- Translations look identical to the source text
  - The “translators” backend may have been blocked or rate-limited, or the selected engine returned the same text
  - Try a different engine by setting TRANSLATOR_ENGINE = "bing" (or another) in configuration.py
  - Configure TRANSLATOR_PROXIES if your environment requires an outbound proxy

- The base text didn’t switch to English
  - Ensure "en" is included in LANGUAGES
  - If the page’s primary language is detected as English, the base stays English. If detection misfired, set SOURCE_LANG to your expected default or share the diagram for tuning

- Folder processing didn’t find any files
  - Only .drawio files are processed (non-recursive). Put your files directly in the folder or ask for a recursive option

Security and privacy
- The “translators” library uses provider web endpoints. Your diagram texts are sent over the network to obtain translations, depending on the engine you select. Consider provider terms of service and privacy implications, and use proxies or on-prem solutions as needed.
- If you require strict privacy, consider integrating an on-prem translation API and replacing the Translator.translate() implementation.

Development
- Type checking with mypy is configured in pyproject.toml.
- Run: mypy translate_drawio.py server.py
- You can adjust strictness in pyproject.toml as needed.

Project structure
```
.
├─ translate_drawio.py   # CLI tool; per-page language detection; translators backend
├─ server.py             # Flask server; drag-and-drop upload/download
├─ configuration.py      # Your settings (languages, output folder, translator options)
├─ pyproject.toml        # Build metadata, dependencies, mypy config
└─ README.md
```

License
- AGPL 3.0 

Contributions
- Issues and pull requests are welcome. If you’re proposing behavior changes (e.g., value_* mirrors or recursive folder processing), please include a small sample .drawio illustrating the expected result.

Thank you
- diagrams.net team for the diagram format
- Maintainers of translators and langdetect for their libraries
- ChatGPT