#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Translate labels in a .drawio file and add per-language attributes next to them.

- Detects label-bearing attributes:
  * "label" (e.g., on <UserObject>)
  * "value" (commonly on <mxCell>)
- For each found label text, creates:
  * label_xx or value_xx attributes for each code in configuration.LANGUAGES
- Assumes source labels are English (configurable via configuration.SOURCE_LANG)
- Writes a new .drawio file to a folder defined in configuration.OUTPUT_DIR
- Handles both plain and compressed <diagram> contents

Dependencies (install what you need):
  pip install requests
  # optional fallback:
  pip install "googletrans==4.0.0rc1"

DeepL support:
  export DEEPL_API_KEY=your_key_here
  # For free keys, the endpoint is https://api-free.deepl.com
  # For paid keys, the endpoint is https://api.deepl.com

Usage:
  python translate_drawio.py path/to/input.drawio
  # Optional args:
  #   --out-name myfile_translated.drawio
  #   --nooverwrite  (to preserve existing label_xx/value_xx)
"""

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

if not LANGUAGES:
    print("ERROR: configuration.LANGUAGES must be defined (e.g., ['de','fr']).", file=sys.stderr)
    sys.exit(2)

# -------------------------
# Translation abstraction
# -------------------------
class Translator:
    def __init__(self, source_lang: str = "en"):
        self.source_lang = source_lang
        self._cache: Dict[Tuple[str, str], str] = {}
        # Backends/config
        self._deepl_key = os.getenv("DEEPL_API_KEY")
        self._deepl_endpoint = os.getenv("DEEPL_API_URL")  # optional override
        self._libre_url = os.getenv("LIBRETRANSLATE_URL")  # e.g., https://libretranslate.yourhost.tld
        self._libre_key = os.getenv("LIBRETRANSLATE_API_KEY")
        self._use_googletrans = os.getenv("USE_GOOGLETRANS", "1") in ("0","false","no","1", "true", "yes")
        self._googletrans = None  # lazy init

    def translate(self, text: str, target_lang: str) -> str:
        txt = text.strip()
        if not txt:
            return txt
        key = (txt, target_lang.lower())
        if key in self._cache:
            return self._cache[key]

        translated: Optional[str] = None
        translated = self._translate_translators(txt, target_lang)
        # 1) DeepL (best quality if available)
        if translated is None and self._deepl_key:
            translated = self._translate_deepl(txt, target_lang)
        # 2) LibreTranslate (self-host or configured endpoint)
        if translated is None and self._libre_url:
            translated = self._translate_libretranslate(txt, target_lang)
        # 3) MyMemory via deep-translator (no key, rate-limited, but reliable enough)
        if translated is None:
            translated = self._translate_mymemory(txt, target_lang)
        # 4) googletrans (disabled by default; enable via USE_GOOGLETRANS=1)
        if translated is None and self._use_googletrans:
            translated = self._translate_googletrans(txt, target_lang)

        if translated is None:
            # Last resort: return original text (or raise if you prefer)
            translated = txt

        self._cache[key] = translated
        return translated

    def _translate_translators(self, text: str, target_lang: str) -> Optional[str]:
        try:
            import translators as ts
        except ImportError:
            return None
        #TODO translate_html when necessary
        if ("<" or "&gl;") in text:
            return ts.translate_html(text, from_language="en", to_language=target_lang)
        return ts.translate_text(text, from_language="en", to_language=target_lang)

    def _translate_deepl(self, text: str, target_lang: str) -> Optional[str]:
        try:
            import requests
        except ImportError:
            return None
        t_lang = target_lang.upper()
        s_lang = self.source_lang.upper() if self.source_lang else None

        endpoint = self._deepl_endpoint
        if not endpoint:
            endpoint = "https://api-free.deepl.com/v2/translate" if "free" in self._deepl_key else "https://api.deepl.com/v2/translate"

        try:
            resp = requests.post(
                endpoint,
                headers={"Authorization": f"DeepL-Auth-Key {self._deepl_key}"},
                data={
                    "text": text,
                    "target_lang": t_lang,
                    **({"source_lang": s_lang} if s_lang else {}),
                    "preserve_formatting": "1",
                },
                timeout=20,
            )
            if resp.status_code == 200:
                data = resp.json()
                tr = data.get("translations", [])
                if tr:
                    return tr[0].get("text")
            else:
                sys.stderr.write(f"DeepL error {resp.status_code}: {resp.text}\n")
        except Exception as e:
            sys.stderr.write(f"DeepL exception: {e}\n")
        return None

    def _translate_libretranslate(self, text: str, target_lang: str) -> Optional[str]:
        """
        Use a LibreTranslate server. Set LIBRETRANSLATE_URL (and optionally LIBRETRANSLATE_API_KEY).
        """
        try:
            import requests
        except ImportError:
            return None

        url = self._libre_url.rstrip("/") + "/translate"
        payload = {
            "q": text,
            "source": self.source_lang or "auto",
            "target": target_lang,
            "format": "text",
        }
        if self._libre_key:
            payload["api_key"] = self._libre_key

        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("translatedText")
            else:
                sys.stderr.write(f"LibreTranslate error {resp.status_code}: {resp.text}\n")
        except Exception as e:
            sys.stderr.write(f"LibreTranslate exception: {e}\n")
        return None

    def _translate_mymemory(self, text: str, target_lang: str) -> Optional[str]:
        """
        Free fallback using deep-translator's MyMemory backend. Rate-limited.
        """
        try:
            from deep_translator import MyMemoryTranslator  # type: ignore
        except Exception:
            return None

        try:
            tr = MyMemoryTranslator(source=self.source_lang or "en", target=target_lang)
            out = tr.translate(text)
            if isinstance(out, str) and out.strip():
                return out
        except Exception as e:
            sys.stderr.write(f"MyMemory exception: {e}\n")
        return None

    def _translate_googletrans(self, text: str, target_lang: str) -> Optional[str]:
        """
        Last-resort googletrans fallback. Disabled by default; enable via USE_GOOGLETRANS=1.
        We handle both sync and async implementations and guard against the 'NoneType.send' bug.
        """
        try:
            if self._googletrans is None:
                from googletrans import Translator as GTTranslator  # type: ignore
                # Create a fresh instance per call as a workaround for the 'NoneType.send' issue
                self._googletrans = GTTranslator()

            res = self._googletrans.translate(text, src=self.source_lang or "en", dest=target_lang)
            import inspect
            if inspect.isawaitable(res):
                import asyncio
                try:
                    res = asyncio.run(res)
                except RuntimeError:
                    # If a loop is running, spawn a helper thread
                    import threading
                    out: Dict[str, object] = {}
                    def _runner() -> None:
                        out["res"] = asyncio.run(res)  # type: ignore[arg-type]
                    t = threading.Thread(target=_runner, daemon=True)
                    t.start()
                    t.join()
                    res = out.get("res")

            # If res is None or not the expected object, bail out gracefully.
            if res is None:
                return None

            return getattr(res, "text", None)
        except Exception as e:
            sys.stderr.write(f"googletrans exception: {e}\n")
            return None

# -------------------------
# draw.io encoding helpers
# -------------------------
def is_compressed_diagram_text(s: str) -> bool:
    # Heuristic: compressed text will not start with '<'
    return not s.lstrip().startswith("<")

def decompress_diagram_text(s: str) -> str:
    """
    draw.io typically stores <diagram> inner content as Deflate-compressed + base64.
    This returns the decompressed XML string of the page (starting with <mxGraphModel ...>).
    """
    data = s.strip()
    # In .drawio files it's base64 of raw deflate bytes.
    # Try base64 decode + raw DEFLATE first; otherwise try standard zlib header.
    try:
        b = base64.b64decode(data)
        try:
            xml = zlib.decompress(b, -15)  # raw DEFLATE
            return xml.decode("utf-8")
        except zlib.error:
            xml = zlib.decompress(b)  # with zlib header
            return xml.decode("utf-8")
    except Exception:
        # If the decode fails, return original string to avoid data loss
        return s

def compress_diagram_text(xml: str) -> str:
    """
    Compress a page XML string as raw DEFLATE + base64, which is what draw.io expects.
    """
    raw = xml.encode("utf-8")
    comp = zlib.compressobj(level=9, wbits=-15)  # raw deflate
    out = comp.compress(raw) + comp.flush()
    return base64.b64encode(out).decode("ascii")


# -------------------------
# Label discovery and update
# -------------------------
def find_label_attributes(elem: ET.Element) -> List[Tuple[str, str]]:
    """
    Return list of (attr_name, text_value) pairs for attributes that represent labels.
    We consider "label" and "value" attributes if they are non-empty.
    """
    results: List[Tuple[str, str]] = []
    for attr_name in ("label", "value"):
        if attr_name in elem.attrib:
            val = elem.attrib.get(attr_name, "")
            if val and val.strip():
                results.append((attr_name, val))
    return results

def decode_label_text(raw: str) -> str:
    """
    draw.io often escapes HTML fragments in attributes. We decode HTML entities here to
    get the visible text. This is a heuristic; we do not attempt to preserve tags in
    translations since we are writing to new attributes only.
    """
    # Unescape HTML entities to get readable text
    txt = html_unescape(raw)
    return txt.strip()

def add_translations_to_element(
    elem: ET.Element,
    translator: Translator,
    languages: Iterable[str],
    source_lang: str,
    overwrite_existing: bool = True,
    parent_map: Optional[Dict[ET.Element, ET.Element]] = None,
    mirror_value_to_label: bool = False,  # also add label_xx when source was in 'value'
    write_hyphen_variants: bool = False,  # add label-de/value-de alongside label_de/value_de
) -> int:
    count = 0
    label_attrs = find_label_attributes(elem)
    if not label_attrs:
        return 0

    # Choose where to store the data so 'Edit Data' sees it
    container = get_data_container(elem, parent_map or {})

    for attr_name, raw_value in label_attrs:
        base_text = decode_label_text(raw_value)
        if not base_text:
            continue

        target_bases = [attr_name]
        if mirror_value_to_label and attr_name == "value":
            target_bases.append("label")

        for lang in languages:
            lcode = lang.lower()
            for base in target_bases:
                # underscore key
                key_us = f"{base}_{lcode}"
                # hyphen key (used by the translate plugin/UI)
                key_hy = f"{base}-{lcode}"

                # Write underscore
                if overwrite_existing or (key_us not in container.attrib):
                    translated = translator.translate(base_text, lcode)
                    if translated is not None:
                        container.set(key_us, translated)
                        count += 1

                # Write hyphen variant
                if write_hyphen_variants and (overwrite_existing or (key_hy not in container.attrib)):
                    translated = translator.translate(base_text, lcode)
                    if translated is not None:
                        container.set(key_hy, translated)
                        count += 1
    return count

#new helpders #
def _localname(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag

def build_parent_map(root: ET.Element) -> Dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}

def ensure_userobject_wrapper(cell: ET.Element, parent_map: Dict[ET.Element, ET.Element]) -> ET.Element:
    """
    If 'cell' is an <mxCell> that is not already inside a <UserObject>, wrap it:
      <mxCell id="X" value="..."> -> <UserObject id="X" label="..."><mxCell .../></UserObject>

    - Move visible text into wrapper's 'label' (if 'value' exists, prefer that).
    - Keep style/geometry/parent on the inner mxCell.
    - Keep the original id on the wrapper; remove id from inner mxCell.
    - Return the wrapper element (or existing wrapper if already wrapped).
    """
    tag = _localname(cell.tag)
    if tag != "mxCell":
        return cell

    parent = parent_map.get(cell)
    if parent is not None and _localname(parent.tag) == "UserObject":
        return parent  # already wrapped

    # Collect existing info
    old_id = cell.attrib.get("id")
    value = cell.attrib.get("value", "")
    label = cell.attrib.get("label", "")
    # The visible text is typically 'value'; fall back to 'label'
    visible_text = value if value.strip() else label

    # Clone the mxCell for the inner child
    inner = ET.Element("mxCell", attrib={k: v for k, v in cell.attrib.items()})
    # Remove id and presentation text from inner cell; wrapper will carry them
    if "id" in inner.attrib:
        del inner.attrib["id"]
    if "value" in inner.attrib:
        del inner.attrib["value"]
    if "label" in inner.attrib:
        del inner.attrib["label"]

    # Move all children (e.g., <mxGeometry/>) under inner
    for ch in list(cell):
        inner.append(ch)

    # Create wrapper
    wrapper = ET.Element("UserObject")
    if old_id:
        wrapper.set("id", old_id)
    if visible_text:
        wrapper.set("label", visible_text)

    # Replace cell with wrapper in its parent, and attach inner
    if parent is None:
        # This should not happen in normal mxGraphModel, but guard anyway
        # If no parent, we cannot replace; just return cell unchanged
        return cell
    idx = list(parent).index(cell)
    parent.remove(cell)
    parent.insert(idx, wrapper)
    wrapper.append(inner)

    return wrapper

# add translations to right container
def add_translations_to_element(
    elem: ET.Element,
    translator: Translator,
    languages: Iterable[str],
    source_lang: str,
    overwrite_existing: bool = True,
    parent_map: Optional[Dict[ET.Element, ET.Element]] = None,
    mirror_value_to_label: bool = True,
    write_hyphen_variants: bool = True,
) -> int:
    count = 0
    label_attrs = find_label_attributes(elem)
    if not label_attrs:
        return 0

    # Decide where to write data
    container = elem
    if parent_map is not None:
        # Prefer a UserObject wrapper; create one if missing and elem is an mxCell
        if _localname(elem.tag) == "mxCell":
            container = ensure_userobject_wrapper(elem, parent_map)
        elif _localname(elem.tag) != "UserObject":
            # If the parent is a UserObject, write to the parent
            par = parent_map.get(elem)
            if par is not None and _localname(par.tag) == "UserObject":
                container = par

    for attr_name, raw_value in label_attrs:
        base_text = decode_label_text(raw_value)
        if not base_text:
            continue

        # We want label_* keys in data. If the source was 'value', mirror to 'label'.
        target_bases = ["label"] if (mirror_value_to_label and attr_name == "value") else [attr_name]

        for lang in languages:
            lcode = lang.lower()
            for base in target_bases:
                keys = [f"{base}_{lcode}"]
                if write_hyphen_variants:
                    keys.append(f"{base}-{lcode}")

                for key in keys:
                    if (not overwrite_existing) and (key in container.attrib):
                        continue
                    translated = translator.translate(base_text, lcode)
                    if translated is None:
                        continue
                    container.set(key, translated)
                    count += 1
    return count

# -------------------------
# Get data container
# -------------------------

def get_data_container(elem: ET.Element, parent_map: Dict[ET.Element, ET.Element]) -> ET.Element:
    """
    Prefer writing custom data onto a UserObject, because diagrams.net 'Edit Data'
    displays attributes on UserObject. If elem is a UserObject, use it; otherwise,
    if its parent is a UserObject, use the parent; else fall back to elem.
    """
    if _localname(elem.tag) == "UserObject":
        return elem
    parent = parent_map.get(elem)
    if parent is not None and _localname(parent.tag) == "UserObject":
        return parent
    return elem
# -------------------------
# Diagram processing
# -------------------------
def process_diagram_xml(diagram_xml: str, translator: Translator, languages: Iterable[str], overwrite: bool) -> str:
    inner_root = ET.fromstring(diagram_xml)

    # First pass: build parent map
    parent_map = build_parent_map(inner_root)

    # We iterate over a static list to avoid issues while wrapping elements (tree mutation)
    elems = list(inner_root.iter())
    for elem in elems:
        add_translations_to_element(
            elem,
            translator=translator,
            languages=languages,
            source_lang=translator.source_lang,
            overwrite_existing=overwrite,
            parent_map=parent_map,
            mirror_value_to_label=True,
            write_hyphen_variants=False,
        )

    return ET.tostring(inner_root, encoding="utf-8").decode("utf-8")


def process_drawio_file(
    input_path: Path,
    output_dir: Path,
    out_name: Optional[str],
    languages: Iterable[str],
    source_lang: str,
    overwrite_existing: bool,
    write_uncompressed: bool = False,
) -> Path:
    tree = ET.parse(str(input_path))
    root = tree.getroot()

    translator = Translator(source_lang=source_lang)

    # Iterate over all <diagram> regardless of namespace
    for diagram in root.iter():
        if _localname(diagram.tag) != "diagram":
            continue

        # Case 1: nested page XML (has element children)
        if any(True for _ in diagram):
            # Build parent map relative to diagram
            parent_map = build_parent_map(diagram)
            elems = list(diagram.iter())
            for elem in elems:
                add_translations_to_element(
                    elem,
                    translator=translator,
                    languages=languages,
                    source_lang=translator.source_lang,
                    overwrite_existing=overwrite_existing,
                    parent_map=parent_map,
                    mirror_value_to_label=True,
                    write_hyphen_variants=False,
                )
            continue

        # Case 2: text content (compressed or plain)
        text = (diagram.text or "").strip()
        if not text:
            continue

        if is_compressed_diagram_text(text):
            page_xml = decompress_diagram_text(text)
            modified = process_diagram_xml(page_xml, translator, languages, overwrite_existing)
            diagram.text = modified if write_uncompressed else compress_diagram_text(modified)
        else:
            modified = process_diagram_xml(text, translator, languages, overwrite_existing)
            diagram.text = modified

    output_dir.mkdir(parents=True, exist_ok=True)
    if not out_name:
        out_name = f"{input_path.stem}_translated.drawio"
    out_path = output_dir / out_name
    tree.write(str(out_path), encoding="utf-8", xml_declaration=True)

    #verification snippet
    # Verify counts in the output file
    tree = ET.parse(str(out_path))
    root = tree.getroot()
    added = 0
    for e in root.iter():
        for k in list(e.attrib.keys()):
            if k.startswith("label_") or k.startswith("value_"):
                added += 1
    print(f"Found {added} translated attributes in output.")
    return out_path


# -------------------------
# CLI
# -------------------------
def main():
    parser = argparse.ArgumentParser(description="Add per-language label_xx/value_xx translations to a .drawio file.")
    parser.add_argument("input", help="Path to input .drawio file")
    parser.add_argument("--out-name", default=None, help="Output filename (defaults to <input>_translated.drawio)")
    parser.add_argument("--nooverwrite", action="store_true", help="Do not overwrite existing label_xx/value_xx")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(2)
    output_dir = Path(OUTPUT_DIR)

    overwrite = CFG_OVERWRITE and (not args.nooverwrite)

    try:
        out_path = process_drawio_file(
            input_path=input_path,
            output_dir=output_dir,
            out_name=args.out_name,
            languages=LANGUAGES,
            source_lang=SOURCE_LANG,
            overwrite_existing=overwrite,
        )
        print(f"Translated file written to: {out_path}")
        print(f"Languages added: {', '.join(LANGUAGES)}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
