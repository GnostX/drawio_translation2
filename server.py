#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
server.py

A minimal Flask server that:
- Serves an HTML page with a drag-and-drop area to upload a .drawio file
- Translates labels using translate_drawio.py and configuration.py
- Returns the transformed .drawio as a file download

Usage:
  python server.py
  # open http://127.0.0.1:5000

Dependencies:
  pip install Flask requests
  # optional fallback
  pip install "googletrans==4.0.0rc1"
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Optional

from flask import Flask, request, send_file, Response, make_response

# Third-party, part of Flask stack
from werkzeug.utils import secure_filename

# Local imports
import configuration
from translate_drawio import process_drawio_file  # type: ignore

app = Flask(__name__)

# Adjust as desired; default 50 MB
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

ALLOWED_EXTS = {".drawio", ".xml"}  # accept .drawio and draw.io XML exports


def _allowed_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXTS


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Draw.io Translator</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      color-scheme: light dark;
    }
    body {
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      margin: 0; padding: 0;
      display: flex; flex-direction: column; min-height: 100vh;
    }
    header, footer {
      padding: 1rem 1.25rem;
    }
    main {
      flex: 1;
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 1.25rem;
    }
    .container {
      max-width: 860px;
      width: 100%;
    }
    .dropzone {
      border: 2px dashed #888;
      border-radius: 10px;
      padding: 2rem;
      text-align: center;
      transition: border-color 0.2s ease, background 0.2s ease;
    }
    .dropzone.dragover {
      border-color: #2f6feb;
      background: rgba(47, 111, 235, 0.07);
    }
    .hint {
      margin-top: 0.5rem;
      color: #666;
      font-size: 0.95rem;
    }
    .hidden { display: none; }
    button, input[type="file"] {
      font: inherit;
    }
    .actions { margin-top: 1rem; display: flex; gap: 0.5rem; justify-content: center; }
    .small { font-size: 0.9rem; }
    .status { margin-top: 0.75rem; text-align: center; min-height: 1.25rem; }
    .ok { color: #1f7a1f; }
    .err { color: #b00020; }
    .muted { color: #777; }
    code { background: rgba(127,127,127,0.15); padding: 0.1rem 0.25rem; border-radius: 3px; }
  </style>
</head>
<body>
  <header>
    <h2>Translate draw.io labels to multiple languages</h2>
    <p class="muted small">Languages configured in configuration.py</p>
  </header>
  <main>
    <div class="container">
      <div id="dropzone" class="dropzone" tabindex="0">
        <p><strong>Drag & drop</strong> a .drawio file here</p>
        <p class="hint">or</p>
        <p><button id="pickBtn" type="button">Choose a file…</button></p>
        <input id="fileInput" type="file" accept=".drawio,.xml" class="hidden" />
        <p class="hint small">The server will translate labels and return a new file for download.</p>
      </div>
      <div class="status muted" id="status"></div>
    </div>
  </main>
  <footer class="muted small">
    Tip: set <code>DEEPL_API_KEY</code> in your environment for high-quality translations. Otherwise, the fallback may be used.
  </footer>

  <script>
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('fileInput');
    const pickBtn = document.getElementById('pickBtn');
    const statusEl = document.getElementById('status');

    const setStatus = (msg, cls) => {
      statusEl.textContent = msg || '';
      statusEl.className = 'status ' + (cls || 'muted');
    };

    const handleFiles = async (files) => {
      if (!files || !files.length) return;
      const file = files[0];
      if (!file.name.toLowerCase().endsWith('.drawio') && !file.name.toLowerCase().endsWith('.xml')) {
        setStatus('Please upload a .drawio or .xml file.', 'err');
        return;
      }

      try {
        setStatus('Uploading and translating… this may take a moment.', 'muted');

        const fd = new FormData();
        fd.append('file', file, file.name);

        const resp = await fetch('/translate', { method: 'POST', body: fd });
        if (!resp.ok) {
          const t = await resp.text();
          throw new Error(t || ('Server error: ' + resp.status));
        }

        // Get filename from Content-Disposition
        let filename = 'translated.drawio';
        const cd = resp.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename\\*?=([^;]+)/i);
        if (match) {
          // Remove quotes if present
          filename = decodeURIComponent(match[1].replace(/^UTF-8''/, '').trim().replace(/^"(.*)"$/, '$1'));
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
          URL.revokeObjectURL(url);
          a.remove();
        }, 1000);

        setStatus('Done. Your download should begin automatically.', 'ok');
      } catch (err) {
        console.error(err);
        setStatus('Error: ' + (err && err.message ? err.message : err), 'err');
      }
    };

    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      handleFiles(e.dataTransfer.files);
    });

    pickBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => handleFiles(e.target.files));
  </script>
</body>
</html>
"""

@app.get("/")
def index() -> Response:
    return make_response(INDEX_HTML, 200)


@app.post("/translate")
def translate_upload() -> Response:
    """
    Accepts a multipart/form-data upload under 'file', translates it using
    translate_drawio.process_drawio_file, and returns the transformed file
    as an attachment.
    """
    if "file" not in request.files:
        return make_response("No file part in the request.", 400)

    up = request.files["file"]
    if not up or up.filename is None or up.filename.strip() == "":
        return make_response("No selected file.", 400)

    filename = secure_filename(up.filename)
    if not _allowed_file(filename):
        return make_response("Unsupported file type. Please upload a .drawio or .xml file.", 400)

    try:
        # Work in a temp directory to avoid persistent writes
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_path = tmpdir_path / filename
            up.save(str(input_path))

            # Prepare output name similar to CLI default
            out_name: Optional[str] = f"{input_path.stem}_translated.drawio"

            # Use configuration values
            languages = getattr(configuration, "LANGUAGES", [])
            source_lang = getattr(configuration, "SOURCE_LANG", "en")
            overwrite_existing = bool(getattr(configuration, "OVERWRITE_EXISTING", True))

            if not languages:
                return make_response("Server misconfiguration: LANGUAGES is empty.", 500)

            # Process and produce the translated file inside tmpdir
            out_path = process_drawio_file(
                input_path=input_path,
                output_dir=tmpdir_path,
                out_name=out_name,
                languages=languages,
                source_lang=source_lang,
                overwrite_existing=overwrite_existing,
            )

            # Read result into memory and respond
            data = out_path.read_bytes()
            bio = io.BytesIO(data)
            bio.seek(0)

            # Set content type; draw.io uses a generic XML container
            # application/vnd.jgraph.mxfile is commonly used, fallback to application/xml
            mimetype = "application/vnd.jgraph.mxfile"

            return send_file(
                bio,
                mimetype=mimetype,
                as_attachment=True,
                download_name=out_path.name,
                max_age=0,
                etag=False,
                conditional=False,
                last_modified=None,
            )

    except Exception as e:
        # Log in production; here we return the message for simplicity
        return make_response(f"Error while processing: {e}", 500)


@app.get("/healthz")
def healthz() -> Response:
    return make_response("ok", 200)


if __name__ == "__main__":
    # Host 0.0.0.0 to allow access from other devices on the network if desired
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
