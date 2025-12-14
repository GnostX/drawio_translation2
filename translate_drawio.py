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
import re


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
WRITE_EN_KEYS: bool = getattr(configuration, "WRITE_EN_KEYS", True)

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
        self.fallback_engines: List[str] = getattr(configuration, "TRANSLATOR_FALLBACK_ENGINES", [])
        self._cache: Dict[Tuple[str, str], str] = {}
        try:
            import translators as ts  # noqa: F401
        except Exception:
            print("ERROR: The 'translators' package is required. Install with: pip install translators", file=sys.stderr)
            raise

    def _translate_with_engine(self, txt: str, src: str, tgt: str, engine: str) -> Optional[str]:
        try:
            import translators as ts
            out = ts.translate_text(
                txt,
                translator=engine,
                from_language=src,
                to_language=tgt,
                timeout=self.timeout,
                if_use_preacceleration=False,
                proxies=self.proxies,
            )
            if isinstance(out, str) and out.strip():
                return out
        except Exception as e:
            sys.stderr.write(f"translators backend exception ({engine}): {e}\n")
        return None

    def translate(self, text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
        txt = (text or "").strip()
        if not txt:
            return txt
        src = (source_lang or self.source_lang or "auto").lower()
        tgt = target_lang.lower()

        key = (f"{src}:{txt}", tgt)
        if key in self._cache:
            return self._cache[key]

        # Primary engine
        out = self._translate_with_engine(txt, src, tgt, self.engine)
        if out and (src == tgt or out.strip() != txt.strip()):
            self._cache[key] = out
            return out

        # Try fallbacks if the result equals the source and src != tgt
        if src != tgt:
            for eng in self.fallback_engines:
                out2 = self._translate_with_engine(txt, src, tgt, eng)
                if out2 and out2.strip() != txt.strip():
                    self._cache[key] = out2
                    return out2

        # Fallback: return original
        self._cache[key] = txt
        return txt


# -------------------------
# Language detection (per diagram)
# -------------------------
def _strip_html_tags(s: str) -> str:
    # Remove draw.io inline HTML tags so detection sees plain text
    return re.sub(r"<[^>]+>", " ", s or "")

def detect_primary_language_allowed(texts: List[str], allowed_langs: List[str], default_lang: str = "en") -> str:
    """
    Detect primary language using only the allowed_langs set.
    Aggregates langdetect probabilities across snippets but keeps scores only
    for languages present in allowed_langs. Returns the best allowed code.
    Falls back to default_lang if no allowed scores could be computed.
    """
    allowed = {l.lower() for l in allowed_langs if l}
    if not allowed:
        return default_lang.lower()

    # Clean and filter snippets to reduce noise
    samples = []
    for t in texts:
        if not t:
            continue
        clean = _strip_html_tags(decode_label_text(t)).strip()
        if len(clean) >= 3:
            samples.append(clean)

    if not samples:
        # No usable text; prefer default if in allowed, else first allowed
        return (default_lang.lower() if default_lang.lower() in allowed else next(iter(allowed)))

    # Map some variant codes to base ones (helps when allowed uses base codes)
    def normalize_code(code: str) -> str:
        c = code.lower()
        if c in ("zh-cn", "zh-tw", "zh-hans", "zh-hant"):
            return "zh"
        if c in ("pt-br", "pt-pt"):
            return "pt"
        return c

    try:
        from langdetect import detect_langs, DetectorFactory  # type: ignore
        DetectorFactory.seed = 0

        scores: Dict[str, float] = {a: 0.0 for a in allowed}
        total_weight = 0.0

        # Cap number of samples and weight by length (bounded)
        for s in samples[:200]:
            weight = min(float(len(s)), 500.0)
            try:
                guesses = detect_langs(s)
            except Exception:
                continue
            if not guesses:
                continue
            # Use top-1 for stability
            top = max(guesses, key=lambda g: float(getattr(g, "prob", 0.0)))
            pred = normalize_code(top.lang)
            prob = float(getattr(top, "prob", 0.0))
            if pred in scores:
                scores[pred] += prob * weight
            total_weight += weight

        # If no allowed language got any score, fall back
        if all(v <= 0.0 for v in scores.values()):
            return (default_lang.lower() if default_lang.lower() in allowed else next(iter(allowed)))

        best_lang = max(scores.items(), key=lambda kv: kv[1])[0]
        return best_lang
    except Exception:
        # If langdetect not available or fails: fallback bounded to allowed
        return (default_lang.lower() if default_lang.lower() in allowed else next(iter(allowed)))


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
# Other processing  helpers
# -------------------------
def get_inner_mxcell_of_wrapper(wrapper: ET.Element) -> Optional[ET.Element]:
    if _localname(wrapper.tag) != "UserObject":
        return None
    for ch in list(wrapper):
        if _localname(ch.tag) == "mxCell":
            return ch
    return None

def set_base_label_and_clear_inner(container: ET.Element, base_text: str) -> None:
    """
    Set the visible base label on the UserObject and remove 'value'/'label'
    from the inner mxCell so it cannot override the wrapper label.
    If 'container' is not a UserObject (edge case), we still set its 'label'.
    """
    container.set("label", base_text)
    if _localname(container.tag) == "UserObject":
        inner = get_inner_mxcell_of_wrapper(container)
        if inner is not None:
            for k in ("value", "label"):
                if k in inner.attrib:
                    del inner.attrib[k]

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
    Ensure a UserObject container for text-bearing elements, set base label,
    and write label_xx/label-xx for languages in LANGUAGES.
    Also writes label_en/label-en if configured and 'en' is requested.
    Returns count of attributes written/updated.
    """
    pair = find_label_text(elem)
    if not pair:
        return 0
    _, raw_value = pair
    original_text = decode_label_text(raw_value)
    if not original_text:
        return 0

    # Ensure we write on a UserObject wrapper
    container = elem
    if _localname(elem.tag) == "mxCell":
        container = ensure_userobject_wrapper(elem, parent_map)
    elif _localname(elem.tag) != "UserObject":
        par = parent_map.get(elem)
        if par is not None and _localname(par.tag) == "UserObject":
            container = par

    written = 0
    target_langs = [lc.lower() for lc in langs]
    has_en = ("en" in target_langs)

    # Determine the English base text if requested
    if has_en:
        english_text = original_text if primary_lang == "en" else translator.translate(original_text, "en", source_lang=primary_lang)
        # Always set the base visible label to English when 'en' is in LANGUAGES
        if overwrite_existing or container.get("label", "") != english_text:
            set_base_label_and_clear_inner(container, english_text)
            written += 1
        # Also create label_en / label-en if enabled
        if WRITE_EN_KEYS:
            for key in ("label_en", "label-en"):
                if overwrite_existing or (key not in container.attrib):
                    if not("-" in key):
                        container.set(key, english_text)
                    written += 1
        # Preserve original primary text under label_<src> if the primary is not English
        if primary_lang != "en":
            for key in (f"label_{primary_lang}", f"label-{primary_lang}"):
                if overwrite_existing or (key not in container.attrib):
                    if not("-" in key):
                        container.set(key, original_text)
                    written += 1
    else:
        # English not requested: keep base text in the primary language as-is
        # but ensure the wrapper is authoritative
        if overwrite_existing or container.get("label", "") != original_text:
            set_base_label_and_clear_inner(container, original_text)
            written += 1
        # Make sure primary language key exists if you want explicit variants
        # (optional; keep as-is if you don't want to duplicate)
        # for key in (f"label_{primary_lang}", f"label-{primary_lang}"):
        #     if overwrite_existing or (key not in container.attrib):
        #         container.set(key, original_text)
        #         written += 1

    # Write translations for other languages in LANGUAGES (excluding 'en', handled above)
    for lang in target_langs:
        if lang == "en":
            continue
        if lang == primary_lang:
            # Already preserved above when 'en' requested; if 'en' not requested,
            # you may still ensure label_<primary> exists (optional, see commented block above).
            continue
        translated = translator.translate(original_text, lang, source_lang=primary_lang)
        for key in (f"label_{lang}", f"label-{lang}"):
            if overwrite_existing or (key not in container.attrib):
                if not("-" in key):
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

    primary_lang = detect_primary_language_allowed(texts, allowed_langs=languages, default_lang=SOURCE_LANG.lower())
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
            primary_lang = detect_primary_language_allowed(texts, allowed_langs=languages,
                                                           default_lang=SOURCE_LANG.lower())
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
