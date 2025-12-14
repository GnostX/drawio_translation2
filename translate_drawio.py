#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Translate labels in a .drawio file (or all files in a folder), per diagram page:
- Detect the primary language of each diagram (page)
- Generate translations only for languages in configuration.LANGUAGES
- Special case for English:
    * If 'en' in LANGUAGES and primary != 'en', set the visible base text to English
      (UserObject@label), do NOT create label_en/value_en keys, and preserve the original
      primary language under label_<src> and label-<src>.
    * If primary == 'en', keep English in base label/value and do not create label_en/value_en.

- Writes translation keys onto a UserObject wrapper so they appear in diagrams.net “Edit Data…”.
- Uses the 'translators' package as translation backend.

Output:
- Writes to configuration.OUTPUT_DIR, using the same base filename as the input.

Usage:
  python translate_drawio.py path/to/file.drawio
  python translate_drawio.py path/to/folder/with/drawio
Options:
  --nooverwrite    Do not overwrite existing translation keys
  --uncompressed   Write page XML uncompressed (useful for inspection)
  --out-name NAME  Override output filename (ignored when input is a folder)

Requirements:
  pip install translators langdetect requests
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
import xml.etree.ElementTree as ET
from html import unescape as html_unescape

# Load configuration
try:
    import configuration  # Must be in the same directory or on PYTHONPATH
except ImportError as e:
    print("ERROR: Could not import configuration.py. Make sure it is accessible.", file=sys.stderr)
    raise

LANGUAGES: List[str] = getattr(configuration, "LANGUAGES", [])
OUTPUT_DIR: str = getattr(configuration, "OUTPUT_DIR", "translated_drawio")
SOURCE_LANG: str = getattr(configuration, "SOURCE_LANG", "en")
CFG_OVERWRITE: bool = getattr(configuration, "OVERWRITE_EXISTING", True)

TRANSLATOR_ENGINE: str = getattr(configuration, "TRANSLATOR_ENGINE", "google")
TRANSLATOR_TIMEOUT: int = int(getattr(configuration, "TRANSLATOR_TIMEOUT", 20))
TRANSLATOR_PROXIES = getattr(configuration, "TRANSLATOR_PROXIES", None)  # e.g., {"http":"http://...","https":"http://..."}

if not LANGUAGES:
    print("ERROR: configuration.LANGUAGES must be defined (e.g., ['en','de','fr','it']).", file=sys.stderr)
    sys.exit(2)

# -------------------------
# Translation via 'translators'
# -------------------------
class Translator:
    def __init__(self, source_lang: str = "en", engine: str = "google", timeout: int = 20, proxies=None):
        self.source_lang = source_lang
        self.engine = engine
        self.timeout = timeout
        self.proxies = proxies
        self._cache: Dict[Tuple[str, str], str] = {}

        try:
            import translators as ts  # noqa: F401
        except Exception:
            print("ERROR: The 'translators' package is required. Install with: pip install translators", file=sys.stderr)
            raise

    def translate(self, text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
        """
        Translate text into target_lang. If source_lang provided, use it; otherwise
        let the engine autodetect based on self.source_lang setting.
        """
        txt = (text or "").strip()
        if not txt:
            return txt

        # Cache key includes target_lang and source_lang (falls back to self.source_lang)
        src = (source_lang or self.source_lang or "auto").lower()
        key = (f"{src}:{txt}", target_lang.lower())
        if key in self._cache:
            return self._cache[key]

        try:
            import translators as ts
            out = ts.translate_text(
                txt,
                translator=self.engine,
                from_language=src,
                to_language=target_lang,
                timeout=self.timeout,
                if_use_preacceleration=False,
                proxies=self.proxies,
            )
            if isinstance(out, str) and out.strip():
                self._cache[key] = out
                return out
        except Exception as e:
            sys.stderr.write(f"translators backend exception ({self.engine}): {e}\n")

        # Fallback: return original text on failure
        self._cache[key] = txt
        return txt


# -------------------------
# Language detection (per diagram)
# -------------------------
def detect_primary_language(texts: List[str], default_lang: str = "en") -> str:
    """
    Detect a primary language from a list of sample texts using langdetect.
    Returns a two-letter code if possible, else default_lang.
    """
    sample = " ".join(t for t in texts if t).strip()
    # Limit length to reduce noise
    if len(sample) > 8000:
        sample = sample[:8000]

    if not sample:
        return default_lang

    try:
        from langdetect import detect, DetectorFactory  # type: ignore
        # Make results deterministic
        DetectorFactory.seed = 0
        lang = detect(sample)
        # Normalize and map to two-letter lowercase
        return (lang or default_lang).lower()
    except Exception:
        # If langdetect missing or fails, fall back
        return default_lang


# -------------------------
# draw.io encoding helpers
# -------------------------
def is_compressed_diagram_text(s: str) -> bool:
    return not (s or "").lstrip().startswith("<")

def decompress_diagram_text(s: str) -> str:
    data = (s or "").strip()
    try:
        b = base64.b64decode(data)
        try:
            xml = zlib.decompress(b, -15)  # raw DEFLATE
            return xml.decode("utf-8")
        except zlib.error:
            xml = zlib.decompress(b)  # zlib header
            return xml.decode("utf-8")
    except Exception:
        return s

def compress_diagram_text(xml: str) -> str:
    raw = (xml or "").encode("utf-8")
    comp = zlib.compressobj(level=9, wbits=-15)  # raw deflate
    out = comp.compress(raw) + comp.flush()
    return base64.b64encode(out).decode("ascii")


# -------------------------
# Utilities and label handling
# -------------------------
def _localname(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag

def build_parent_map(root: ET.Element) -> Dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}

def find_label_text(elem: ET.Element) -> Optional[Tuple[str, str]]:
    """
    Return the first label-bearing attribute ('value' or 'label') and its text.
    Preference order: value, then label.
    """
    if "value" in elem.attrib and elem.attrib["value"].strip():
        return ("value", elem.attrib["value"])
    if "label" in elem.attrib and elem.attrib["label"].strip():
        return ("label", elem.attrib["label"])
    return None

def decode_label_text(raw: str) -> str:
    return html_unescape(raw or "").strip()

def collect_diagram_texts(container: ET.Element) -> List[str]:
    """
    Collect sample texts from a diagram/page (mxGraphModel subtree).
    """
    texts: List[str] = []
    for elem in container.iter():
        pair = find_label_text(elem)
        if pair:
            texts.append(decode_label_text(pair[1]))
            if len(texts) >= 100:
                break
    return texts

def ensure_userobject_wrapper(cell: ET.Element, parent_map: Dict[ET.Element, ET.Element]) -> ET.Element:
    """
    If 'cell' is an <mxCell> not already inside a <UserObject>, wrap it so that
    custom data is visible in diagrams.net 'Edit Data…'.

    Wrapper keeps the original id; the inner mxCell loses id/value/label.
    """
    tag = _localname(cell.tag)
    if tag != "mxCell":
        return cell

    parent = parent_map.get(cell)
    if parent is not None and _localname(parent.tag) == "UserObject":
        return parent  # already wrapped

    old_id = cell.attrib.get("id")
    value = cell.attrib.get("value", "")
    label = cell.attrib.get("label", "")
    visible_text = value if value.strip() else label

    # Clone inner mxCell
    inner = ET.Element("mxCell", attrib={k: v for k, v in cell.attrib.items()})
    for k in ("id", "value", "label"):
        if k in inner.attrib:
            del inner.attrib[k]
    for ch in list(cell):
        inner.append(ch)

    wrapper = ET.Element("UserObject")
    if old_id:
        wrapper.set("id", old_id)
    if visible_text:
        wrapper.set("label", visible_text)

    if parent is None:
        # Unexpected; leave unchanged
        return cell

    idx = list(parent).index(cell)
    parent.remove(cell)
    parent.insert(idx, wrapper)
    wrapper.append(inner)
    # Update parent map for new nodes
    parent_map[inner] = wrapper
    parent_map[wrapper] = parent
    return wrapper


# -------------------------
# Core per-element update
# -------------------------
def translate_and_apply_for_element(
    elem: ET.Element,
    translator: Translator,
    parent_map: Dict[ET.Element, ET.Element],
    langs: List[str],
    primary_lang: str,
    english_in_langs: bool,
    overwrite_existing: bool,
) -> int:
    """
    For a single element that has visible text, ensure a UserObject container,
    set base label to English if requested, and write label_xx/label-xx for other langs.
    Returns count of attributes written/updated.
    """
    pair = find_label_text(elem)
    if not pair:
        return 0

    _, raw_value = pair
    base_text = decode_label_text(raw_value)
    if not base_text:
        return 0

    # Ensure we write onto a UserObject
    container = elem
    if _localname(elem.tag) == "mxCell":
        container = ensure_userobject_wrapper(elem, parent_map)
    elif _localname(elem.tag) != "UserObject":
        par = parent_map.get(elem)
        if par is not None and _localname(par.tag) == "UserObject":
            container = par

    written = 0

    # Determine which languages to generate (only those in langs)
    target_langs = [lc.lower() for lc in langs]

    # Optionally set English as base label
    if english_in_langs:
        if primary_lang == "en":
            # Keep English base as-is; do not create label_en/value_en
            english_text = base_text
        else:
            english_text = translator.translate(base_text, "en", source_lang=primary_lang)
        # Set visible base text on the container
        if container.get("label") != english_text:
            container.set("label", english_text)
            written += 1

    # Preserve original primary language under label_<src> if we switched base to English
    if english_in_langs and primary_lang != "en":
        for key in (f"label_{primary_lang}", f"label-{primary_lang}"):
            if overwrite_existing or (key not in container.attrib):
                container.set(key, base_text)
                written += 1

    # Create other translations for langs except 'en' (we never create label_en/value_en)
    for lang in target_langs:
        if lang == "en":
            continue  # no label_en keys; English is base if requested
        # If lang == primary, we already preserved the original (above) when english_in_langs
        if lang == primary_lang:
            # If 'en' not requested, we may still want to ensure label_<primary> exists
            if not english_in_langs:
                for key in (f"label_{lang}", f"label-{lang}"):
                    if overwrite_existing or (key not in container.attrib):
                        container.set(key, base_text)
                        written += 1
            continue

        # Translate from primary_lang to lang
        translated = translator.translate(base_text, lang, source_lang=primary_lang)
        # only doing it for label_ in the moment
        #for key in (f"label_{lang}", f"label-{lang}"):
        for key in (f"label_{lang}"):
            if overwrite_existing or (key not in container.attrib):
                container.set(key, translated)
                written += 1

    return written


# -------------------------
# Diagram processing (per page)
# -------------------------
def process_diagram_xml(diagram_xml: str, translator: Translator, languages: List[str], overwrite: bool) -> Tuple[str, str]:
    """
    Process one page's inner XML (<mxGraphModel...>).
    - Detect primary language for the page.
    - Apply translations per element.
    Returns (modified_xml, detected_primary_lang)
    """
    inner_root = ET.fromstring(diagram_xml)
    parent_map = build_parent_map(inner_root)

    # Detect primary language from sample texts
    texts = collect_diagram_texts(inner_root)
    primary_lang = detect_primary_language(texts, default_lang=SOURCE_LANG.lower())

    english_in_langs = ("en" in {l.lower() for l in languages})

    # Iterate over a static list to tolerate tree changes (wrapping)
    elems = list(inner_root.iter())
    for elem in elems:
        translate_and_apply_for_element(
            elem=elem,
            translator=translator,
            parent_map=parent_map,
            langs=languages,
            primary_lang=primary_lang,
            english_in_langs=english_in_langs,
            overwrite_existing=overwrite,
        )

    xml_out = ET.tostring(inner_root, encoding="utf-8").decode("utf-8")
    return xml_out, primary_lang


def process_drawio_file(
    input_path: Path,
    output_dir: Path,
    out_name: Optional[str],
    languages: List[str],
    source_lang: str,
    overwrite_existing: bool,
    write_uncompressed: bool = False,
) -> Path:
    """
    Read the .drawio, process each page, and write the result to output_dir/<same-name>.
    If out_name is provided, it overrides the output filename.
    """
    tree = ET.parse(str(input_path))
    root = tree.getroot()

    translator = Translator(
        source_lang=source_lang,
        engine=TRANSLATOR_ENGINE,
        timeout=TRANSLATOR_TIMEOUT,
        proxies=TRANSLATOR_PROXIES,
    )

    # Iterate over all <diagram> regardless of namespace
    for diagram in root.iter():
        if _localname(diagram.tag) != "diagram":
            continue

        # Case 1: nested XML (has children)
        if any(True for _ in diagram):
            # Build parent map for the diagram node
            parent_map = build_parent_map(diagram)
            texts = collect_diagram_texts(diagram)
            primary_lang = detect_primary_language(texts, default_lang=SOURCE_LANG.lower())
            english_in_langs = ("en" in {l.lower() for l in languages})

            for elem in list(diagram.iter()):
                translate_and_apply_for_element(
                    elem=elem,
                    translator=translator,
                    parent_map=parent_map,
                    langs=languages,
                    primary_lang=primary_lang,
                    english_in_langs=english_in_langs,
                    overwrite_existing=overwrite_existing,
                )
            continue

        # Case 2: text content (compressed or plain)
        text = (diagram.text or "").strip()
        if not text:
            continue

        if is_compressed_diagram_text(text):
            page_xml = decompress_diagram_text(text)
            modified, primary_lang = process_diagram_xml(page_xml, translator, languages, overwrite_existing)
            diagram.text = modified if write_uncompressed else compress_diagram_text(modified)
        else:
            modified, primary_lang = process_diagram_xml(text, translator, languages, overwrite_existing)
            diagram.text = modified

    output_dir.mkdir(parents=True, exist_ok=True)
    # Default: keep same file name as input
    out_path = output_dir / (out_name if out_name else input_path.name)
    tree.write(str(out_path), encoding="utf-8", xml_declaration=True)
    return out_path


# -------------------------
# Input collection (file or folder)
# -------------------------
def collect_input_files(input_path: Path) -> List[Path]:
    """
    Return a list of input files to process.
    - If input_path is a file: return [input_path]
    - If input_path is a directory: return all *.drawio files (non-recursive)
    """
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        files = sorted(
            p for p in input_path.iterdir()
            if p.is_file() and p.suffix.lower() == ".drawio"
        )
        if not files:
            print(f"WARNING: No .drawio files found in folder: {input_path}", file=sys.stderr)
        return files
    raise FileNotFoundError(f"Input path not found: {input_path}")


# -------------------------
# CLI
# -------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Detect primary language per draw.io diagram and add translations for configured LANGUAGES. "
                    "English (if requested) is stored in base label/value (no label_en/value_en).")
    parser.add_argument("input", help="Path to input .drawio file OR a folder containing .drawio files")
    parser.add_argument("--out-name", default=None, help="Output filename (ignored if input is a folder). Defaults to same as input name.")
    parser.add_argument("--nooverwrite", action="store_true", help="Do not overwrite existing translation keys")
    parser.add_argument("--uncompressed", action="store_true", help="Write pages uncompressed for easier inspection")
    args = parser.parse_args()

    input_path = Path(args.input)
    try:
        files = collect_input_files(input_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    output_dir = Path(OUTPUT_DIR)
    overwrite = CFG_OVERWRITE and (not args.nooverwrite)

    # If processing a folder, ignore --out-name to avoid collisions
    if input_path.is_dir() and args.out_name:
        print("Note: --out-name is ignored when input is a folder.", file=sys.stderr)

    successes = 0
    failures = 0

    for f in files:
        try:
            out_path = process_drawio_file(
                input_path=f,
                output_dir=output_dir,
                out_name=(args.out_name if input_path.is_file() else None),
                languages=[l.lower() for l in LANGUAGES],
                source_lang=SOURCE_LANG.lower(),
                overwrite_existing=overwrite,
                write_uncompressed=args.uncompressed,
            )
            print(f"[OK] {f.name} -> {out_path}")
            successes += 1
        except Exception as e:
            print(f"[ERROR] {f}: {e}", file=sys.stderr)
            failures += 1

    if input_path.is_dir():
        print(f"Done. {successes} file(s) processed successfully, {failures} failed.")
    else:
        if failures:
            sys.exit(1)


if __name__ == "__main__":
    main()
