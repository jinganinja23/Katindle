"""
Katindle — Single-file, Pi-Zero-friendly EPUB/TXT reader (desktop now, e-ink later)

Controls (desktop): Z=Up / X=Down / C=Select-Open / V=Back, Esc/Q=Quit

Now with autosave reading progress per book.

Install (desktop):
  pip install pygame pillow ebooklib lxml

Optional env:
  BOOKS_FOLDER  -> overrides the default books path.
  FONT_PATH     -> TTF to use (defaults to DejaVu Sans or Consolas on Windows).
  KATINDLE_STATE -> override path to the JSON state file.

Default paths:
  Windows books: C:/Users/jamie/Documents/Stuff/Katindle/python/books
  Linux/Pi books: /home/j/Katindle/books
  State file:
    • Windows: %APPDATA%/Katindle/state.json
    • Linux/Pi: ~/.local/share/katindle/state.json
"""

import zipfile, shutil
from __future__ import annotations
import os, sys, json, tempfile, shutil, hashlib
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
import subprocess
import gpioB
import time
from collections import deque
def tnow(): return time.monotonic()
if sys.platform.startswith("linux"):
        # use absolute path because we run with sudo
    import epd5in79
# -------------------------- Config -----------------------------------------
DEFAULT_BOOKS_FOLDER = (
    os.environ.get("BOOKS_FOLDER")
    or ("C:/Users/jamie/Documents/Stuff/Katindle/python/books" if os.name == "nt" else "/home/j/Katindle/books")
)
SCREEN_W, SCREEN_H = 792, 272
MARGIN = 15
LINE_SPACING = 3
FONT_SIZE = 18
TITLE_FONT_SIZE = 25
FPS = 10

# -------------------------- State file utils --------------------------------
def _default_state_path() -> str:
    override = os.environ.get("KATINDLE_STATE")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Katindle", "state.json")
    else:
        return os.path.join(os.path.expanduser("~/.local/share/katindle"), "state.json")

STATE_PATH = _default_state_path()

def set_wifi(enabled: bool):
    cmd = ["rfkill", "unblock" if enabled else "block", "wifi"]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if enabled:
        subprocess.run(["systemctl", "enable", "--now", "ssh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["systemctl", "disable", "--now", "ssh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def set_bluetooth(enabled: bool):
    cmd = ["rfkill", "unblock" if enabled else "block", "bluetooth"]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def wifi_is_on() -> bool:
    out = subprocess.run(["rfkill", "list", "wifi"], capture_output=True, text=True)
    return "Soft blocked: no" in out.stdout

def bt_is_on() -> bool:
    out = subprocess.run(["rfkill", "list", "bluetooth"], capture_output=True, text=True)
    return "Soft blocked: no" in out.stdout

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"progress": {}}

def save_state(state: Dict[str, Any]) -> None:
    try:
        _ensure_dir(STATE_PATH)
        fd, tmp = tempfile.mkstemp(prefix="katindle_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        shutil.move(tmp, STATE_PATH)
    except Exception:
        pass

# -------------------------- Third-party ------------------------------------
try:
    import pygame  # type: ignore
    HAVE_PYGAME = True
except Exception:
    HAVE_PYGAME = False

from PIL import Image, ImageDraw, ImageFont  # type: ignore
from ebooklib import epub  # type: ignore
from lxml import html, etree  # type: ignore

# -------------------------- Data models ------------------------------------
@dataclass
class Book:
    path: str
    title: str
    author: str = ""

class Library:
    SUPPORTED = (".epub", ".txt")

    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(self.folder, exist_ok=True)
        self.books: List[Book] = []

    def rescan(self) -> None:
        self.books.clear()
        for root, _, files in os.walk(self.folder):
            for f in sorted(files, key=str.lower):
                if os.path.splitext(f)[1].lower() in self.SUPPORTED:
                    path = os.path.join(root, f)
                    if f.lower().endswith(".epub"):
                        title, author = self._epub_meta(path)
                    else:
                        title, author = os.path.splitext(f)[0], ""
                    self.books.append(Book(path, title or f, author))

    def _epub_meta(self, path: str) -> Tuple[str, str]:
        try:
            bk = epub.read_epub(path)
            t = bk.get_metadata("DC", "title")
            a = bk.get_metadata("DC", "creator")
            return (t[0][0] if t else os.path.basename(path), a[0][0] if a else "")
        except Exception:
            return os.path.basename(path), ""

# -------------------------- EPUB text extraction ---------------------------
class EpubText:
    def __init__(self, path: str):
        self.path = path
        self.title = os.path.splitext(os.path.basename(path))[0]
        self.chapters: List[str] = []
        if os.path.isdir(path):
            self._load_unpacked_dir(path)   # NEW
        elif path.lower().endswith(".epub"):
            self._load_epub(path)
        else:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                self.chapters = [f.read()]

    def _load_epub(self, path: str) -> None:
        bk = epub.read_epub(path)
        id_to_item = {it.get_id(): it for it in bk.get_items()}
        texts: List[str] = []
        if bk.spine:
            for sid, *_ in bk.spine:
                it = id_to_item.get(sid)
                if it is not None and getattr(it, "__class__", None).__name__ == "EpubHtml":
                    txt = self._html_to_text(it.get_content())
                    if txt:
                        texts.append(txt)
        else:
            for it in bk.get_items():
                if getattr(it, "__class__", None).__name__ == "EpubHtml":
                    txt = self._html_to_text(it.get_content())
                    if txt:
                        texts.append(txt)
        self.chapters = texts or ["(No readable text found)"]
    def _load_unpacked_dir(self, root: str) -> None:
        # Collect html/xhtml files in a stable order
        candidates = []
        for r, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".xhtml", ".html", ".htm")):
                    candidates.append(os.path.join(r, f))
        candidates.sort()
        texts: List[str] = []
        for fp in candidates:
            try:
                with open(fp, "rb") as fh:
                    txt = self._html_to_text(fh.read())
                    if txt:
                        texts.append(txt)
            except Exception:
                pass
        self.chapters = texts or ["(No readable text found)"]
    def _html_to_text(self, content: bytes) -> str:
        try:
            doc = html.fromstring(content)
            etree.strip_elements(doc, "script", "style", with_tail=False)
            t = doc.text_content()
            lines = [" ".join(line.split()) for line in t.splitlines()]
            t = "\n".join([ln for ln in lines if ln])
            return t
        except Exception:
            return ""

# -------------------------- Pagination -------------------------------------
class Paginator:
    def __init__(self, width: int, height: int, margin: int, line_spacing: int,
                 font_size: int, title_font_size: int, font_path: Optional[str] = None):
        self.width, self.height = width, height
        self.margin, self.line_spacing = margin, line_spacing

        self._wcache: Dict[str, float] = {}

        # pick fonts
        fp = font_path or os.environ.get("FONT_PATH") or (
            "C:/Windows/Fonts/consola.ttf" if os.name == "nt"
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        try:
            self.font = ImageFont.truetype(fp, font_size)
            # bold(er) title: on Linux we can point to the bold DejaVu, on Windows you can pick another
            if os.name == "nt":
                self.title_font = ImageFont.truetype(fp, title_font_size)
            else:
                self.title_font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    title_font_size
                )
        except Exception:
            self.font = ImageFont.load_default()
            self.title_font = ImageFont.load_default()

    def paginate(self, book_title: str, chapters: List[str]) -> List[Tuple[int, int]]:
        
        img = Image.new("L", (self.width, self.height), 255)
        d = ImageDraw.Draw(img)
        y = self.margin
        title_h = self.title_font.size + self.line_spacing * 2
        footer_h = self.font.size + self.line_spacing
        top_y = y + title_h
        bottom_y = self.height - self.margin - footer_h
        max_w = self.width - 2 * self.margin
        max_h = bottom_y - top_y
        sample = "the quick brown fox jumps over the lazy dog"
        avg_char_w = d.textlength(sample, font=self.font) / max(1, len(sample))
        chars_per_line = max(10, int(max_w / max(1e-6, avg_char_w)))
        lines_per_page = max(5, int(max_h / (self.font.size + self.line_spacing)))
        words_per_line_guess = max(5, int(chars_per_line / 5))
        words_per_page_guess = max(50, words_per_line_guess * lines_per_page)

        index: List[Tuple[int, int]] = []
        for ci, chap in enumerate(chapters):
            words = chap.split()
            wi = 0
            while wi < len(words):
                index.append((ci, wi))
                wi += words_per_page_guess
        return index

    def render_page(
        self,
        title: str,
        chapter_text: str,
        start_word: int,
        page_no: int,
        total_pages: int,
    ) -> Tuple[Image.Image, int]:
        img = Image.new("L", (self.width, self.height), color=255)
        d = ImageDraw.Draw(img)
        x = self.margin
        y = self.margin

        # 1) TITLE (bold)
        d.text((x, y), title, fill=0, font=self.title_font)
        y += self.title_font.size + self.line_spacing * 2

        # footer
        footer = f"{page_no+1} / {total_pages}" if total_pages else f"{page_no+1}"
        fw = d.textlength(footer, font=self.font)
        footer_y = self.height - self.margin - self.font.size

        # 2) BODY (no single orphan line)
        max_w = self.width - 2 * self.margin
        max_h = footer_y - y - self.line_spacing
        words = chapter_text.split()
        i = start_word
        lh = self.font.size + self.line_spacing
        used_h = 0
        lines: List[str] = []
        line = ""

        while i < len(words) and used_h + lh <= max_h:
            w = words[i]
            test = (line + " " + w).strip()
            if self._w(d, test) <= max_w:
                line = test
                i += 1
            else:
                lines.append(line)
                line = w
                used_h += lh
                # stop early if adding another line would leave just a tiny orphan
                if used_h + lh * 2 > max_h:
                    break
                i += 1
        if line:
            lines.append(line)

        # 3) DRAW lines (still justified)
        for idx, ln in enumerate(lines):
            words_ln = ln.split()
            is_last = idx == len(lines) - 1
            if len(words_ln) == 1 or is_last:
                # left align
                d.text((x, y), ln, fill=0, font=self.font)
            else:
                total_w = sum(self._w(d, w) for w in words_ln)
                gaps = len(words_ln) - 1
                extra = (max_w - total_w) / gaps if gaps else 0
                cx = x
                for w in words_ln[:-1]:
                    d.text((cx, y), w, fill=0, font=self.font)
                    cx += self._w(d, w) + extra
                # last word flush right
                last_w = words_ln[-1]
                d.text((x + max_w - self._w(d, last_w), y), last_w, fill=0, font=self.font)
            y += lh

        # footer
        d.text((self.width - self.margin - fw, footer_y), footer, fill=0, font=self.font)
        return img, i
    def _w(self, drawer: ImageDraw.ImageDraw, s: str) -> float:
        w = self._wcache.get(s)
        if w is None:
            w = drawer.textlength(s, font=self.font)
            self._wcache[s] = w
        return w


# -------------------------- Renderers --------------------------------------
class Renderer:
    def draw_image(self, img: Image.Image) -> None:
        raise NotImplementedError

class SDLRenderer(Renderer):
    def __init__(self, width: int, height: int):
        if not HAVE_PYGAME:
            raise RuntimeError("pygame not installed; install it or use EInkRenderer later")
        pygame.init()
        self.screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption("Katindle")
        self.clock = pygame.time.Clock()

    def draw_image(self, img: Image.Image) -> None:
        if img.mode != "RGB":
            img = img.convert("RGB")
        data = img.tobytes()
        surf = pygame.image.frombuffer(data, img.size, "RGB")
        self.screen.blit(surf, (0, 0))
        pygame.display.flip()
        self.clock.tick(FPS)

    def poll_key(self) -> Optional[int]:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return ord("q")
            if event.type == pygame.KEYDOWN:
                return event.key
        return None

# Replace your EInkRenderer with this
class EInkRenderer(Renderer):
    def __init__(self, width: int, height: int):
        import epd5in79
        self.epd = epd5in79.EPD()
        if hasattr(self.epd, "init_fast"):
            try: self.epd.init_fast()
            except: self.epd.init()
        else:
            self.epd.init()

        self.epd.Clear()
        self.width = width
        self.height = height
        self.panel_w, self.panel_h = self.epd.width, self.epd.height
        self._thr = 178
        self._lut = [0 if i < self._thr else 255 for i in range(256)]
        print("EPD reports:", self.panel_w, "x", self.panel_h)

    def draw_image(self, img: Image.Image) -> None:
        if img.mode != "L":
            img = img.convert("L")

        if (img.width > img.height) != (self.panel_w > self.panel_h):
            img = img.rotate(90, expand=True)

        if (img.width, img.height) != (self.panel_w, self.panel_h):
            # NEAREST is faster and fine for 1-bit glyphs
            img = img.resize((self.panel_w, self.panel_h), Image.NEAREST)

        # Use LUT (C-accelerated) instead of Python lambda per pixel
        bw = img.point(self._lut, mode="1")
        self.epd.display(self.epd.getbuffer(bw))

    def poll_key(self):
        return None
    def clear_white(self):
        from PIL import Image
        white = Image.new("1", (self.epd.width, self.epd.height), 255)
        self.epd.display(self.epd.getbuffer(white))

    def shutdown(self):
        try:
            self.epd.sleep()   # low power
        except Exception:
            pass

# -------------------------- App / Controller -------------------------------
class KatindleApp:
    STATE_LIBRARY = "library"
    STATE_READER = "reader"
    STATE_LIB_MENU = 'library_menu'
    STATE_READER_MENU = 'reader_menu'
    STATE_SETTINGS = 'settings'
    STATE_DEV_SETTINGS = 'dev_settings'

    def __init__(self, books_folder: str, renderer: Renderer, width: int, height: int):
        self.books_folder = books_folder
        self.renderer = renderer
        self.width, self.height = width, height
        self.library = Library(books_folder)
        self.state = self.STATE_LIBRARY
        self.cursor = 0
        self.current_book: Optional[Book] = None
        self.text: Optional[EpubText] = None
        self.paginator = Paginator(width, height, MARGIN, LINE_SPACING, FONT_SIZE, TITLE_FONT_SIZE)
        self.page_markers: List[Tuple[int, int]] = []
        self.page = 0
        self.state_store: Dict[str, Any] = load_state()
        self.lib_menu_items = ["Sleep", "Settings", "Dev Settings"]
        self.read_menu_items = ["Sleep", "Chapter", "Settings", "Restart"]
        self.lib_menu_cursor = 0
        self.read_menu_cursor = 0
        self.input_queue = deque()
        self.dirty = True
        self.last_press_ts = 0.0

        if "progress" not in self.state_store:
            self.state_store["progress"] = {}

    def _progress_key(self, book_path: str) -> str:
        return os.path.abspath(book_path)

    def load_progress(self, book_path: str) -> int:
        key = self._progress_key(book_path)
        prog = self.state_store.get("progress", {}).get(key)
        if isinstance(prog, dict) and isinstance(prog.get("page"), int):
            return max(0, prog["page"])
        return 0

    def save_progress(self) -> None:
        if not self.current_book:
            return
        key = self._progress_key(self.current_book.path)
        self.state_store["progress"][key] = {"page": int(self.page)}
        save_state(self.state_store)

    def _img_library(self) -> Image.Image:
        img = Image.new("L", (self.width, self.height), 255)
        d = ImageDraw.Draw(img)
        y = MARGIN
        d.text((MARGIN, y), "Library", fill=0, font=self.paginator.title_font)
        y += self.paginator.title_font.size + LINE_SPACING * 2
        f = self.paginator.font
        if not self.library.books:
            d.text((MARGIN, y), "(No books) — Press C to scan", fill=0, font=f)
        else:
            for i, b in enumerate(self.library.books):
                prefix = "➤ " if i == self.cursor else "  "
                line = f"{prefix}{b.title}" + (f" — {b.author}" if b.author else "")
                d.text((MARGIN, y), line, fill=0, font=f)
                y += f.size + LINE_SPACING
                if y > self.height - MARGIN - f.size:
                    break
        help_ = "Z=Up X=Down C=Open/Rescan V=Quit"
        fw = d.textlength(help_, font=f)
        d.text((self.width - MARGIN - fw, self.height - MARGIN - f.size), help_, fill=0, font=f)
        return img

    def _img_reader(self) -> Image.Image:
        assert self.current_book and self.text
        total = len(self.page_markers)
        # try cached bitmap first
        cached = self.page_img_load(self.current_book.path, self.page)
        if cached is not None:
            return cached

        ci, wi = self.page_markers[self.page]
        chapter_text = self.text.chapters[ci]
        page_img, _ = self.paginator.render_page(self.current_book.title, chapter_text, wi, self.page, total)
        # save for next time
        self.page_img_save(self.current_book.path, self.page, page_img)
        return page_img


    
    def frame(self) -> Image.Image:
        if self.state == self.STATE_LIBRARY:
            return self._img_library()
        elif self.state == self.STATE_LIB_MENU:
            return self._img_library_menu()
        elif self.state == self.STATE_READER_MENU:
            return self._img_reader_menu()
        elif self.state == self.STATE_DEV_SETTINGS:
            return self._img_dev_settings()
        else:
            return self._img_reader()
        
    def _cache_dir(self):
        p = os.path.expanduser("~/.cache/katindle")
        os.makedirs(p, exist_ok=True)
        return p

    def _cache_key(self, book_path: str) -> str:
        st = os.stat(book_path)
        # include font + screen, so changes invalidate cache
        sig = f"{book_path}|{int(st.st_mtime)}|{SCREEN_W}x{SCREEN_H}|{self.paginator.font.size}"
        return hashlib.sha1(sig.encode()).hexdigest()

    def load_page_cache(self, book_path: str):
        key = self._cache_key(book_path)
        fp = os.path.join(self._cache_dir(), key + ".json")
        try:
            with open(fp, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def save_page_cache(self, book_path: str, markers):
        key = self._cache_key(book_path)
        fp = os.path.join(self._cache_dir(), key + ".json")
        try:
            with open(fp, "w") as f:
                json.dump(markers, f, separators=(",", ":"))
        except Exception:
            pass


    def up(self):
        if self.state == self.STATE_LIBRARY:
            n = max(1, len(self.library.books))
            self.cursor = (self.cursor - 1) % n
        elif self.state == self.STATE_LIB_MENU:
            self.lib_menu_cursor = (self.lib_menu_cursor - 1) % len(self.lib_menu_items)
        elif self.state == self.STATE_READER_MENU:
            self.read_menu_cursor = (self.read_menu_cursor - 1) % len(self.read_menu_items)
        elif self.state == self.STATE_DEV_SETTINGS:  # 👈 add this
            # 3 items: Wi-Fi, Bluetooth, Back
            self.lib_menu_cursor = (self.lib_menu_cursor - 1) % 3
        else:
            if self.page > 0:
                self.page -= 1
                self.save_progress()
        self.dirty = True


    def down(self):
        if self.state == self.STATE_LIBRARY:
            n = len(self.library.books)
            if n:
                self.cursor = (self.cursor + 1) % n
        elif self.state == self.STATE_LIB_MENU:
            self.lib_menu_cursor = (self.lib_menu_cursor + 1) % len(self.lib_menu_items)
        elif self.state == self.STATE_READER_MENU:
            self.read_menu_cursor = (self.read_menu_cursor + 1) % len(self.read_menu_items)
        elif self.state == self.STATE_DEV_SETTINGS:  # 👈 add this
            self.lib_menu_cursor = (self.lib_menu_cursor + 1) % 3
        else:
            if self.page_markers:
                new_page = min(len(self.page_markers) - 1, self.page + 1)
                if new_page != self.page:
                    self.page = new_page
                    self.save_progress()
        self.dirty = True



    def select(self):
        t_press_to_select = (tnow() - getattr(self, "last_press_ts", tnow()))*1000
        print(f"[T] press→select() start: {t_press_to_select:.1f} ms")
        if self.state == self.STATE_LIB_MENU:
            choice = self.lib_menu_items[self.lib_menu_cursor]
            if choice == "Dev Settings":
                self.state = self.STATE_DEV_SETTINGS
                self.lib_menu_cursor = 0
            elif choice == "Sleep":
                self.renderer.clear_white()   # show pure white
                self.renderer.shutdown()      # panel sleep
                sys.exit(0) 
            else:
                self.state = self.STATE_LIBRARY
            self.dirty = True
            return
        elif self.state == self.STATE_READER_MENU:
            choice = self.read_menu_items[self.read_menu_cursor]
            if choice == "Chapter":
                pass
            elif choice == "Restart":
                self.page = 0
                self.save_progress()
                self.state = self.STATE_READER
            elif choice == "Sleep":
                self.renderer.clear_white()   # show pure white
                self.renderer.shutdown()      # panel sleep
                sys.exit(0) 
            else:
                self.state = self.STATE_READER
            self.dirty = True
            return
        elif self.state == self.STATE_READER:
            # open reader menu
            self.read_menu_cursor = 0
            self.state = self.STATE_READER_MENU
            self.dirty = True
            return
        elif self.state == self.STATE_DEV_SETTINGS:
            if self.lib_menu_cursor == 0:
                set_wifi(not wifi_is_on())
            elif self.lib_menu_cursor == 1:
                set_bluetooth(not bt_is_on())
            else:
                self.state = self.STATE_LIBRARY
            self.dirty = True
            return
        elif self.state == self.STATE_LIBRARY:
                t0 = tnow()
                self.current_book = self.library.books[self.cursor]
                unpacked = self.ensure_epub_unpacked(self.current_book.path)   # NEW
                self.text = EpubText(unpacked) 
                t1 = tnow()

                markers = self.load_page_cache(self.current_book.path)
                cache_hit = markers is not None
                if not cache_hit:
                    markers = self.paginator.paginate(self.current_book.title, self.text.chapters)  # layout
                t2 = tnow()

                if not cache_hit:
                    self.save_page_cache(self.current_book.path, markers)
                self.page_markers = markers

                self.page = min(max(0, self.load_progress(self.current_book.path)),
                                max(0, len(self.page_markers)-1))
                t3 = tnow()

                self.state = self.STATE_READER
                self.dirty = True
                print("[T] EPUB init: {:.1f} ms | paginate: {:.1f} ms | setpage: {:.1f} ms{}".format(
                    (t1 - t0)*1000, (t2 - t1)*1000, (t3 - t2)*1000,
                    " | CACHE" if cache_hit else " | first-time"
                ))
                return    


    def back(self):
        if self.state == self.STATE_READER:
            self.save_progress()
            self.state = self.STATE_LIBRARY
        elif self.state == self.STATE_LIBRARY:
            # open the library menu
            self.lib_menu_cursor = 0
            self.state = self.STATE_LIB_MENU
        elif self.state == self.STATE_LIB_MENU:
            # close menu
            self.state = self.STATE_LIBRARY
        elif self.state == self.STATE_READER_MENU:
            self.state = self.STATE_READER
        elif self.state == self.STATE_DEV_SETTINGS:
            self.state = self.STATE_LIBRARY

        else:
            pass
        self.dirty = True


    def rescan_library(self):
        self.library.rescan()

    def _img_library_menu(self) -> Image.Image:
        base = self._img_library().copy()
        d = ImageDraw.Draw(base)
        font = self.paginator.font

        box_w = int(self.width * 0.6)
        box_h = font.size * len(self.lib_menu_items) + (len(self.lib_menu_items) + 1) * 10 + 40
        x = (self.width - box_w) // 2
        y = (self.height - box_h) // 2

        # outer box (white)
        d.rectangle((x, y, x + box_w, y + box_h), fill=255, outline=0)

        # header bar (black)
        header_h = font.size + 14
        d.rectangle((x, y, x + box_w, y + header_h), fill=0)

        # header text (white)
        d.text((x + 12, y + 7), "Book Menu", fill=255, font=font)

        # start listing items under header
        item_y = y + header_h + 6
        for idx, item in enumerate(self.lib_menu_items):
            line_y = item_y + idx * (font.size + 10)
            if idx == self.lib_menu_cursor:
                d.rectangle((x + 8, line_y - 4, x + box_w - 8, line_y + font.size + 4), fill=200)
            d.text((x + 16, line_y), item, fill=0, font=font)

        return base
    
    def _img_reader_menu(self) -> Image.Image:
        base = self._img_reader().copy()
        d = ImageDraw.Draw(base)
        font = self.paginator.font

        box_w = int(self.width * 0.6)
        box_h = font.size * len(self.read_menu_items) + (len(self.read_menu_items) + 1) * 10 + 40
        x = (self.width - box_w) // 2
        y = (self.height - box_h) // 2

        # outer box (white)
        d.rectangle((x, y, x + box_w, y + box_h), fill=255, outline=0)

        # header bar (black)
        header_h = font.size + 14
        d.rectangle((x, y, x + box_w, y + header_h), fill=0)

        # header text (white)
        d.text((x + 12, y + 7), "Menu", fill=255, font=font)

        # start listing items under header
        item_y = y + header_h + 6
        for idx, item in enumerate(self.read_menu_items):
            line_y = item_y + idx * (font.size + 10)
            if idx == self.read_menu_cursor:

                d.rectangle((x + 8, line_y - 4, x + box_w - 8, line_y + font.size + 4), fill=200)
            d.text((x + 16, line_y), item, fill=0, font=font)

        return base
    def _img_dev_settings(self) -> Image.Image:
        img = Image.new("L", (self.width, self.height), 255)
        d = ImageDraw.Draw(img)
        font = self.paginator.font

        d.rectangle((0, 0, self.width, 40), fill=0)
        d.text((12, 10), "Developer Settings", fill=255, font=font)

        opts = [
            f"Wi-Fi: {'ON' if wifi_is_on() else 'OFF'}",
            f"Bluetooth: {'ON' if bt_is_on() else 'OFF'}",
            "Back",
        ]
        for i, opt in enumerate(opts):
            y = 70 + i * 40
            if i == self.lib_menu_cursor:
                d.rectangle((20, y - 4, self.width - 20, y + 28), fill=200)
            d.text((30, y), opt, fill=0, font=font)

        return img
    def _epub_cache_root(self):
        p = os.path.expanduser("~/.cache/katindle/epub")
        os.makedirs(p, exist_ok=True)
        return p

    def _epub_cache_dir(self, book_path):
        st = os.stat(book_path)
        key = hashlib.sha1(f"{book_path}|{int(st.st_mtime)}".encode()).hexdigest()
        return os.path.join(self._epub_cache_root(), key)

    def ensure_epub_unpacked(self, book_path):
        target = self._epub_cache_dir(book_path)
        marker = os.path.join(target, ".ok")
        if os.path.isdir(target) and os.path.isfile(marker):
            return target
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(book_path, "r") as z:
            z.extractall(target)
        open(marker, "w").close()
        return target
    
    def _page_img_root(self):
        p = os.path.expanduser("~/.cache/katindle/pages")
        os.makedirs(p, exist_ok=True)
        return p

    def _page_key(self, book_path: str, page_idx: int) -> str:
        st = os.stat(book_path)
        font_sz = self.paginator.font.size
        sig = f"{book_path}|{int(st.st_mtime)}|{page_idx}|{SCREEN_W}x{SCREEN_H}|{font_sz}"
        return hashlib.sha1(sig.encode()).hexdigest()

    def page_img_load(self, book_path: str, page_idx: int):
        fp = os.path.join(self._page_img_root(), self._page_key(book_path, page_idx) + ".png")
        if os.path.exists(fp):
            try:
                return Image.open(fp).convert("L")
            except Exception:
                return None
        return None

    def page_img_save(self, book_path: str, page_idx: int, img: Image.Image):
        fp = os.path.join(self._page_img_root(), self._page_key(book_path, page_idx) + ".png")
        try:
            img.save(fp, format="PNG", optimize=True)
        except Exception:
            pass
# -------------------------- Desktop main loop ------------------------------
KEY_Z = ord("z")
KEY_X = ord("x")
KEY_C = ord("c")
KEY_V = ord("v")
def handle_event(self, evt: str):
    if evt == "UP":
        self.up()
    elif evt == "DOWN":
        self.down()
    elif evt == "SELECT":
        self.select()
    elif evt == "BACK":
        self.back()

def run_desktop():
    if not HAVE_PYGAME:
        print("Install pygame: pip install pygame")
        sys.exit(1)

    # 1) make renderer + app
    if sys.platform.startswith("linux"):
        renderer = EInkRenderer(SCREEN_W, SCREEN_H)
    else:
        renderer = SDLRenderer(SCREEN_W, SCREEN_H)

    app = KatindleApp(DEFAULT_BOOKS_FOLDER, renderer, SCREEN_W, SCREEN_H)
    app.library.rescan()
    usb_script = "/home/j/Katindle/usb_watch.sh"
    # 2) start usb watcher (optional)
    try:
        subprocess.Popen(["sudo", usb_script])

        print("[+] usb_watch.sh started")
    except Exception as e:
        print(f"[!] Could not start usb_watch.sh: {e}")

    # 3) set up GPIO buttons from separate file
    try:
        gpio_buttons = gpioB.setup_buttons(app)
        print("[+] GPIO buttons ready")
    except Exception as e:
        print(f"[!] GPIO buttons not started: {e}")

    # 4) notification setup
    NEW_BOOKS_FLAG = "/home/j/Katindle/new-books.flag"
    new_books_msg_timer = 0.0
    last_time = time.time()

    try:
        last_img_bytes = None
        while True:
            # 1) consume any queued GPIO events
            while app.input_queue:
                app.handle_event(app.input_queue.popleft())

            # 2) (optional) keyboard path still works if you use SDL on desktop
            key = renderer.poll_key()
            if key is not None:
                if key in (ord("q"), 27):  # quit
                    break
                elif key == ord("z"):
                    app.up()
                elif key == ord("x"):
                    app.down()
                elif key == ord("c"):
                    app.select()
                elif key == ord("v"):
                    app.back()

            # 3) react to USB flag (kept as-is)
            if os.path.exists(NEW_BOOKS_FLAG):
                app.rescan_library()
                os.remove(NEW_BOOKS_FLAG)
                new_books_msg_timer = 2.0
                app.dirty = True

            # 4) Only render when dirty (no busy full redraws)
            if app.dirty or new_books_msg_timer > 0:
                tF0 = tnow()
                img = app.frame()                # build image
                tF1 = tnow()
                if new_books_msg_timer > 0:
                    d = ImageDraw.Draw(img)
                    msg = "New books imported"
                    font = app.paginator.font
                    w = d.textlength(msg, font=font)
                    d.rectangle((0, 0, w + 24, 40), fill=255)
                    d.text((12, 10), msg, fill=0, font=font)
                    new_books_msg_timer = max(0.0, new_books_msg_timer - 0.02)
                renderer.draw_image(img)
                tF2 = tnow()
                print("[T] press→frameStart={:.1f} ms | frame()={:.1f} ms | display()={:.1f} ms".format(
                    (tF0 - app.last_press_ts)*1000, (tF1 - tF0)*1000, (tF2 - tF1)*1000
                ))
                app.dirty = False

            # Keep loop light but responsive on Zero
            time.sleep(0.005)

            


    finally:
        app.save_progress()
        if HAVE_PYGAME:
            pygame.quit()


if __name__ == "__main__":
    if HAVE_PYGAME:
        run_desktop()
    else:
        print("No renderer available. Install pygame for desktop dev, or implement EInkRenderer for the Pi.")


