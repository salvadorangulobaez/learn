#!/usr/bin/env python3
"""
doc_parser.py - Surgical document reader for study materials (PDFs and EPUBs)
Designed for learning harnesses: extracts TOC, page ranges, chapters, and performs search
without loading entire massive files into the LLM context window.
"""

import sys
import os
import json
import re
import zipfile
import subprocess
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import List, Dict, Any, Optional

# --- HTML to Markdown parser for EPUBs ---
class HTMLToMarkdownParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.output = []
        self.in_code = False
        self.in_pre = False
        self.in_list_item = False
        self.list_depth = 0
        self.current_header_level = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.current_header_level = int(tag[1])
            self.output.append(f"\n\n{'#' * self.current_header_level} ")
        elif tag == 'p':
            self.output.append("\n\n")
        elif tag == 'br':
            self.output.append("\n")
        elif tag in ('pre',):
            self.in_pre = True
            self.output.append("\n```\n")
        elif tag in ('code',):
            if not self.in_pre:
                self.output.append("`")
            self.in_code = True
        elif tag in ('ul', 'ol'):
            self.list_depth += 1
            self.output.append("\n")
        elif tag == 'li':
            self.in_list_item = True
            indent = "  " * (self.list_depth - 1)
            self.output.append(f"\n{indent}- ")
        elif tag in ('b', 'strong'):
            self.output.append("**")
        elif tag in ('i', 'em'):
            self.output.append("*")
        elif tag == 'hr':
            self.output.append("\n\n---\n\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.current_header_level = 0
            self.output.append("\n")
        elif tag == 'p':
            self.output.append("\n")
        elif tag == 'pre':
            self.in_pre = False
            self.output.append("\n```\n")
        elif tag == 'code':
            if not self.in_pre:
                self.output.append("`")
            self.in_code = False
        elif tag in ('ul', 'ol'):
            self.list_depth = max(0, self.list_depth - 1)
            self.output.append("\n")
        elif tag == 'li':
            self.in_list_item = False
        elif tag in ('b', 'strong'):
            self.output.append("**")
        elif tag in ('i', 'em'):
            self.output.append("*")

    def handle_data(self, data):
        # Normalize whitespace unless inside pre/code
        if self.in_pre:
            self.output.append(data)
        else:
            cleaned = re.sub(r'[ \t\r\f\v]+', ' ', data)
            self.output.append(cleaned)

    def get_markdown(self) -> str:
        text = "".join(self.output)
        # Clean multiple empty lines
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


# --- PDF Parser via Poppler CLI ---
class PDFParser:
    def __init__(self, filepath: str):
        self.filepath = os.path.abspath(filepath)
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"PDF file not found: {self.filepath}")

    def get_info(self) -> Dict[str, Any]:
        result = {"type": "pdf", "file": self.filepath, "pages": 0, "title": "", "author": ""}
        try:
            out = subprocess.check_output(["pdfinfo", self.filepath], stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')
            for line in out.splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip().lower()
                    val = val.strip()
                    if key == "pages":
                        result["pages"] = int(val)
                    elif key == "title":
                        result["title"] = val
                    elif key == "author":
                        result["author"] = val
        except Exception as e:
            # Fallback if pdfinfo fails
            result["error"] = str(e)
        return result

    def get_pages(self, start_page: int, end_page: int) -> str:
        try:
            cmd = ["pdftotext", "-f", str(start_page), "-l", str(end_page), "-layout", self.filepath, "-"]
            text = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')
            return text.strip()
        except Exception as e:
            return f"Error extracting PDF pages {start_page}-{end_page}: {e}"

    def search(self, query: str, max_matches: int = 15) -> List[Dict[str, Any]]:
        info = self.get_info()
        total_pages = info.get("pages", 0)
        matches = []
        if total_pages <= 0:
            return matches

        regex = re.compile(re.escape(query), re.IGNORECASE)
        # Extract page by page or in batches
        batch_size = 25
        for start in range(1, total_pages + 1, batch_size):
            end = min(start + batch_size - 1, total_pages)
            try:
                cmd = ["pdftotext", "-f", str(start), "-l", str(end), self.filepath, "-"]
                raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')
                pages_text = raw.split('\x0c') # Form feed separates pages in pdftotext
                for i, page_text in enumerate(pages_text):
                    current_page = start + i
                    if current_page > end:
                        break
                    for line in page_text.splitlines():
                        if regex.search(line):
                            matches.append({
                                "page": current_page,
                                "snippet": line.strip()
                            })
                            if len(matches) >= max_matches:
                                return matches
            except Exception:
                continue
        return matches


# --- EPUB Parser (Zero external dependencies) ---
class EPUBParser:
    def __init__(self, filepath: str):
        self.filepath = os.path.abspath(filepath)
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"EPUB file not found: {self.filepath}")
        self.zip_ref = zipfile.ZipFile(self.filepath, 'r')
        self.opf_dir = ""
        self.opf_file = self._find_opf()
        self.manifest = {}
        self.spine = []
        self.metadata = {}
        self._parse_opf()

    def _find_opf(self) -> str:
        try:
            container_data = self.zip_ref.read('META-INF/container.xml')
            root = ET.fromstring(container_data)
            # Find rootfile element
            for elem in root.iter('{urn:oasis:names:tc:opendocument:xmlns:container}rootfile'):
                full_path = elem.attrib.get('full-path')
                if full_path:
                    self.opf_dir = os.path.dirname(full_path)
                    return full_path
        except Exception:
            pass
        # Fallback search for any .opf file in the zip
        for name in self.zip_ref.namelist():
            if name.endswith('.opf'):
                self.opf_dir = os.path.dirname(name)
                return name
        raise ValueError("Could not find OPF file in EPUB.")

    def _parse_opf(self):
        opf_data = self.zip_ref.read(self.opf_file)
        root = ET.fromstring(opf_data)

        # Parse metadata
        for meta in root.iter():
            tag = meta.tag.split('}')[-1].lower()
            if tag in ('title', 'creator', 'language', 'publisher'):
                self.metadata[tag] = meta.text or ""

        # Parse manifest (id -> href)
        for item in root.iter():
            tag = item.tag.split('}')[-1].lower()
            if tag == 'item':
                item_id = item.attrib.get('id')
                href = item.attrib.get('href')
                media_type = item.attrib.get('media-type', '')
                if item_id and href:
                    full_href = os.path.normpath(os.path.join(self.opf_dir, href)) if self.opf_dir else href
                    self.manifest[item_id] = {
                        "href": full_href,
                        "media_type": media_type
                    }

        # Parse spine (reading order)
        for itemref in root.iter():
            tag = itemref.tag.split('}')[-1].lower()
            if tag == 'itemref':
                idref = itemref.attrib.get('idref')
                if idref:
                    self.spine.append(idref)

    def get_info(self) -> Dict[str, Any]:
        return {
            "type": "epub",
            "file": self.filepath,
            "title": self.metadata.get("title", "Untitled"),
            "author": self.metadata.get("creator", "Unknown"),
            "total_spine_items": len(self.spine)
        }

    def get_toc(self) -> List[Dict[str, Any]]:
        toc = []
        # Try to find toc.ncx or nav.xhtml in manifest
        ncx_id = None
        nav_href = None
        for item_id, item in self.manifest.items():
            if 'toc' in item_id.lower() or item.get('media_type') == 'application/x-dtbncx+xml':
                ncx_id = item_id
            if 'nav' in item.get('href', '').lower() or 'nav' in item_id.lower():
                nav_href = item.get('href')

        # Try NCX first (EPUB2 / backwards compatible EPUB3)
        if ncx_id and ncx_id in self.manifest:
            try:
                ncx_data = self.zip_ref.read(self.manifest[ncx_id]['href'])
                root = ET.fromstring(ncx_data)
                for navpoint in root.iter('{http://www.daisy.org/z3986/2005/ncx/}navPoint'):
                    text_elem = navpoint.find('{http://www.daisy.org/z3986/2005/ncx/}navLabel/{http://www.daisy.org/z3986/2005/ncx/}text')
                    content_elem = navpoint.find('{http://www.daisy.org/z3986/2005/ncx/}content')
                    title = text_elem.text if text_elem is not None else "Untitled"
                    src = content_elem.attrib.get('src') if content_elem is not None else ""
                    toc.append({"title": title.strip(), "src": src})
            except Exception:
                pass

        # If NCX failed or empty, list chapters from spine
        if not toc:
            for idx, idref in enumerate(self.spine):
                item = self.manifest.get(idref, {})
                href = item.get("href", "")
                toc.append({
                    "title": f"Section {idx + 1} ({os.path.basename(href)})",
                    "src": href,
                    "idref": idref
                })
        return toc

    def read_section(self, target: str) -> str:
        """Read a section by href or spine index or idref"""
        target_path = None
        # Check if target is integer index in spine
        if target.isdigit():
            idx = int(target)
            if 0 <= idx < len(self.spine):
                idref = self.spine[idx]
                target_path = self.manifest.get(idref, {}).get("href")
            elif 1 <= idx <= len(self.spine):
                idref = self.spine[idx - 1]
                target_path = self.manifest.get(idref, {}).get("href")

        if not target_path:
            # Check by idref
            if target in self.manifest:
                target_path = self.manifest[target]["href"]
            else:
                # Check by href substring or exact match
                clean_target = target.split('#')[0]
                for item in self.manifest.values():
                    if clean_target in item.get('href', '') or item.get('href', '').endswith(clean_target):
                        target_path = item.get('href')
                        break

        if not target_path or target_path not in self.zip_ref.namelist():
            return f"Section '{target}' not found in EPUB. Available spine sections: 0 to {len(self.spine)-1}"

        try:
            html_content = self.zip_ref.read(target_path).decode('utf-8', errors='ignore')
            parser = HTMLToMarkdownParser()
            parser.feed(html_content)
            return parser.get_markdown()
        except Exception as e:
            return f"Error reading section {target_path}: {e}"

    def search(self, query: str, max_matches: int = 15) -> List[Dict[str, Any]]:
        matches = []
        regex = re.compile(re.escape(query), re.IGNORECASE)
        for idx, idref in enumerate(self.spine):
            item = self.manifest.get(idref, {})
            href = item.get("href")
            if not href or href not in self.zip_ref.namelist():
                continue
            try:
                html_data = self.zip_ref.read(href).decode('utf-8', errors='ignore')
                parser = HTMLToMarkdownParser()
                parser.feed(html_data)
                md = parser.get_markdown()
                for line in md.splitlines():
                    if regex.search(line):
                        matches.append({
                            "section_index": idx,
                            "section_file": os.path.basename(href),
                            "snippet": line.strip()
                        })
                        if len(matches) >= max_matches:
                            return matches
            except Exception:
                continue
        return matches


# --- Main CLI ---
def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  doc_parser.py info <file.pdf|file.epub>")
        print("  doc_parser.py toc <file.pdf|file.epub>")
        print("  doc_parser.py read <file.pdf> --pages <start>-<end>")
        print("  doc_parser.py read <file.epub> --section <index|id|href>")
        print("  doc_parser.py search <file.pdf|file.epub> <query>")
        sys.exit(1)

    action = sys.argv[1].lower()
    filepath = sys.argv[2]
    ext = os.path.splitext(filepath)[1].lower()

    if ext == '.pdf':
        parser = PDFParser(filepath)
    elif ext == '.epub':
        parser = EPUBParser(filepath)
    else:
        print(f"Unsupported file format: {ext}. Only .pdf and .epub supported.")
        sys.exit(1)

    if action == "info":
        info = parser.get_info()
        print(json.dumps(info, indent=2, ensure_ascii=False))

    elif action == "toc":
        if ext == '.epub':
            toc = parser.get_toc()
            print(f"## Table of Contents: {os.path.basename(filepath)}\n")
            for i, item in enumerate(toc):
                src = item.get('src', '')
                print(f"- [{i}] **{item['title']}** (`{src}`)")
        else:
            info = parser.get_info()
            pages = info.get('pages', 0)
            print(f"## PDF Summary: {os.path.basename(filepath)}")
            print(f"- Total pages: {pages}")
            print(f"- Title: {info.get('title') or '(none)'}")
            print(f"- Author: {info.get('author') or '(none)'}")
            print("\nUse `read <file> --pages <start>-<end>` to read specific sections.")

    elif action == "read":
        if ext == '.pdf':
            pages_arg = None
            if "--pages" in sys.argv:
                idx = sys.argv.index("--pages")
                if idx + 1 < len(sys.argv):
                    pages_arg = sys.argv[idx + 1]

            if not pages_arg:
                print("Error: For PDFs, please specify `--pages <start>-<end>` (e.g. `--pages 1-15`).")
                sys.exit(1)

            parts = pages_arg.split("-")
            start_page = int(parts[0])
            end_page = int(parts[1]) if len(parts) > 1 else start_page
            print(f"--- Extracted Pages {start_page} to {end_page} of {os.path.basename(filepath)} ---\n")
            print(parser.get_pages(start_page, end_page))

        elif ext == '.epub':
            section_arg = None
            if "--section" in sys.argv:
                idx = sys.argv.index("--section")
                if idx + 1 < len(sys.argv):
                    section_arg = sys.argv[idx + 1]

            if section_arg is None:
                print("Error: For EPUBs, please specify `--section <index|id|href>` (e.g. `--section 2`).")
                sys.exit(1)

            print(f"--- Section {section_arg} of {os.path.basename(filepath)} ---\n")
            print(parser.read_section(section_arg))

    elif action == "search":
        if len(sys.argv) < 4:
            print("Error: Please provide a search query.")
            sys.exit(1)
        query = sys.argv[3]
        matches = parser.search(query)
        print(f"## Search Results for '{query}' in {os.path.basename(filepath)} ({len(matches)} matches):\n")
        for m in matches:
            if ext == '.pdf':
                print(f"- **Page {m['page']}**: {m['snippet']}")
            else:
                print(f"- **Section {m['section_index']} ({m['section_file']})**: {m['snippet']}")

    else:
        print(f"Unknown action: {action}")
        sys.exit(1)

if __name__ == "__main__":
    main()
