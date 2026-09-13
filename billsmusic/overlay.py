"""Jukebox intro overlay: track-start takeover that shrinks into a corner
badge, plus drifting info/fact cards. Rendered as a click-through overlay on
top of the main window. Extracted from the standalone previewer.
"""
import math, random, time
from PyQt6 import QtCore, QtGui, QtWidgets


MAGENTA = QtGui.QColor(255, 60, 172)
CYAN    = QtGui.QColor(33, 230, 255)
VIOLET  = QtGui.QColor(177, 76, 255)
WHITE   = QtGui.QColor(255, 255, 255)

INTRO_STYLES = ["flyin", "zoombounce", "glitch", "neonsweep"]
LYRIC_STYLES = {
    "clean": "Clean lyric video",
    "neon": "Party neon",
    "visualiser": "Dance visualiser",
    "big": "Big animated text",
}


def make_font(size):
    f = QtGui.QFont()
    f.setFamilies(["Segoe UI Black", "Arial Black", "Segoe UI", "Arial"])
    f.setPointSize(size)
    f.setBold(True)
    if hasattr(QtGui.QFont.Weight, "Black"):
        f.setWeight(QtGui.QFont.Weight.Black)
    return f


def draw_glow_text(painter, x, y, text, font, core_color, glow_color,
                   glow_radius=12, glow_alpha=200):
    """Draw text with a clean soft halo: a real blurred copy behind crisp text.

    Renders the glow into an offscreen image, blurs it with a cheap separable
    box blur, then stamps it under sharp core text. No multi-offset smearing.
    """
    fm = QtGui.QFontMetrics(font)
    tw = fm.horizontalAdvance(text)
    th = fm.height()
    pad = glow_radius * 3 + 6
    iw, ih = tw + pad * 2, th + pad * 2
    if iw <= 0 or ih <= 0:
        return

    img = QtGui.QImage(iw, ih, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    ip = QtGui.QPainter(img)
    ip.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    ip.setFont(font)
    gc = QtGui.QColor(glow_color); gc.setAlpha(glow_alpha)
    ip.setPen(gc)
    ip.drawText(pad, pad + fm.ascent(), text)
    ip.end()

    blurred = _box_blur_argb(img, glow_radius)
    painter.drawImage(int(x - pad), int(y - fm.ascent() - pad), blurred)

    # crisp core
    painter.setPen(core_color)
    painter.setFont(font)
    painter.drawText(int(x), int(y), text)


def _box_blur_argb(img, radius):
    """Small separable box blur on the alpha+rgb of an ARGB image."""
    try:
        import numpy as np
    except Exception:
        return img  # fallback: unblurred (still fine)
    w, h = img.width(), img.height()
    ptr = img.bits(); ptr.setsize(h * w * 4)
    arr = np.frombuffer(ptr, np.uint8).reshape((h, w, 4)).astype(np.float32)
    r = max(1, int(radius))
    k = 2 * r + 1
    # horizontal then vertical moving-average via cumulative sum
    for axis in (0, 1):
        cs = np.cumsum(arr, axis=axis)
        if axis == 1:
            pad = np.zeros((h, 1, 4), np.float32)
            cs = np.concatenate([pad, cs], axis=1)
            lo = np.clip(np.arange(w) - r, 0, w)
            hi = np.clip(np.arange(w) + r + 1, 0, w)
            arr = (cs[:, hi] - cs[:, lo]) / (hi - lo)[None, :, None]
        else:
            pad = np.zeros((1, w, 4), np.float32)
            cs = np.concatenate([pad, cs], axis=0)
            lo = np.clip(np.arange(h) - r, 0, h)
            hi = np.clip(np.arange(h) + r + 1, 0, h)
            arr = (cs[hi] - cs[lo]) / (hi - lo)[:, None, None]
    out = np.ascontiguousarray(arr.clip(0, 255).astype(np.uint8))
    res = QtGui.QImage(out.data, w, h, QtGui.QImage.Format.Format_ARGB32_Premultiplied).copy()
    return res


def lerp(a, b, t): return a + (b - a) * t
def clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))
def ease_out_cubic(t): return 1 - (1 - t) ** 3
def ease_out_back(t):
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
def ease_in_cubic(t): return t ** 3


class JukeboxOverlay(QtWidgets.QWidget):
    """Transparent overlay painted on top of the player. Owns the intro
    takeover, the persistent corner badge, and the drifting info cards."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._artist = ""
        self._title = ""
        self._style = "flyin"

        # intro state
        self._intro_active = False
        self._intro_t0 = 0.0
        self._intro_dur = 4.0
        self._settled = False          # badge parked in corner

        # badge geometry (animated from full-screen to corner)
        self._badge_progress = 0.0     # 0 = full takeover, 1 = parked

        # info cards
        self._cards = []               # scheduled (time_frac, text)
        self._card_idx = 0
        self._active_card = None       # (text, t0, life)
        self._info_panel = None        # (label, text, t0, life) on-demand card
        self._track_len = 60.0         # mock seconds
        self._play_t0 = None
        self._time_scale = 1.0

        # glitch frame cache
        self._glitch_seed = 0

        # timed lyrics
        self._lyric_lines = ("", "", "")
        self._lyric_t0 = 0.0
        self._lyric_fade_out = False
        # Change this through set_lyric_style() or the saved app config. The
        # presets below are intentionally isolated from LRC parsing/playback.
        self._lyric_style = "neon"
        # Rendering big outlined lyric text is the expensive part. Cache the
        # completed transparent text image and animate that cached image each
        # frame, so the equaliser and other UI painting stay smooth.
        self._lyric_text_cache = {}

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    # -- public driving API --------------------------------------------------
    def start_track(self, artist, title, cards=None, track_len=180.0, style=None):
        self._artist = (artist or "Unknown Artist").upper()
        self._title = title or "Unknown"
        self._style = style or random.choice(INTRO_STYLES)
        self._intro_dur = {"flyin": 4.0, "zoombounce": 4.5,
                           "glitch": 3.0, "neonsweep": 3.6}[self._style]
        self._intro_active = True
        self._settled = False
        self._intro_t0 = time.time()
        self._badge_progress = 0.0
        self._glitch_seed = random.randint(0, 9999)

        self._cards = cards or []
        self._card_idx = 0
        self._active_card = None
        self._track_len = max(20.0, track_len)
        self._play_t0 = time.time()
        self.update()

    def set_cards(self, texts):
        """Schedule fact/bio chunks across the remaining track time. Safe to
        call after start_track once the (async) bio has arrived."""
        texts = [t.strip() for t in (texts or []) if t and t.strip()]
        if not texts or self._play_t0 is None:
            return
        # Show cards sooner and closer together; they are short music facts, not credits.
        elapsed = time.time() - self._play_t0
        start_frac = clamp((elapsed + 2.0) / self._track_len, 0.03, 0.35)
        end_frac = min(0.62, start_frac + max(0.18, 5.0 * max(1, len(texts)) / self._track_len))
        n = len(texts)
        span = max(0.01, end_frac - start_frac)
        cards = []
        for i, txt in enumerate(texts):
            frac = start_frac + span * (i / max(1, n))
            cards.append((frac, txt))
        self._cards = cards
        self._card_idx = 0

    def clear(self):
        """Stop all overlay activity (e.g. playback stopped)."""
        self._intro_active = False
        self._settled = False
        self._active_card = None
        self._cards = []
        self._play_t0 = None
        self.update()

    def show_info_panel(self, label, text, life=8.0):
        """Show an on-demand info card (e.g. track info) that auto-dismisses."""
        self._info_panel = (label, text, time.time(), life)
        self.update()

    def set_lyric(self, text):
        self.set_lyrics("", text, "")

    def set_lyrics(self, previous, current, next_line):
        lines = (
            previous or "",
            current or "",
            next_line or "",
        )
        if lines == self._lyric_lines and not self._lyric_fade_out:
            return
        self._lyric_lines = lines
        self._lyric_t0 = time.time()
        self._lyric_fade_out = False
        self._lyric_text_cache.clear()
        self.update()

    def clear_lyric(self):
        if not any(self._lyric_lines):
            return
        self._lyric_t0 = time.time()
        self._lyric_fade_out = True
        self.update()

    def set_lyric_style(self, style):
        """Switch the visual treatment without changing lyric timing."""
        if style not in LYRIC_STYLES:
            style = "neon"
        if style == self._lyric_style:
            return
        self._lyric_style = style
        self._lyric_text_cache.clear()
        self.update()

    def lyric_style(self):
        return self._lyric_style

    # -- clock ---------------------------------------------------------------
    def _tick(self):
        now = time.time()
        if self._intro_active:
            elapsed = now - self._intro_t0
            if elapsed >= self._intro_dur:
                self._intro_active = False
                self._settled = True
                self._badge_progress = 1.0
        # card scheduling
        if self._play_t0 is not None and self._cards:
            play_elapsed = (now - self._play_t0) * self._time_scale
            frac = clamp(play_elapsed / max(1.0, self._track_len))
            if self._card_idx < len(self._cards):
                ct, _ = self._cards[self._card_idx]
                if frac >= ct:
                    txt = self._cards[self._card_idx][1]
                    self._active_card = (txt, now, 12.0)  # 12s on screen
                    self._card_idx += 1
            if self._active_card:
                _, c0, life = self._active_card
                if now - c0 > life:
                    self._active_card = None
        if self._info_panel:
            _, _, i0, ilife = self._info_panel
            if now - i0 > ilife:
                self._info_panel = None
        if self._lyric_fade_out and now - self._lyric_t0 > 0.35:
            self._lyric_lines = ("", "", "")
            self._lyric_fade_out = False
        self.update()

    # -- paint ---------------------------------------------------------------
    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()

        if self._intro_active:
            elapsed = time.time() - self._intro_t0
            frac = clamp(elapsed / self._intro_dur)
            self._paint_intro(p, w, h, frac)
        elif self._settled:
            self._paint_badge(p, w, h, 1.0)

        if self._active_card:
            self._paint_card(p, w, h)
        if self._info_panel:
            self._paint_info_panel(p, w, h)
        if any(self._lyric_lines) and not self._intro_active:
            self._paint_lyric(p, w, h)
        p.end()

    # -- intro styles --------------------------------------------------------
    def _paint_intro(self, p, w, h, frac):
        # dim backdrop that fades in then out at the very end
        if frac < 0.85:
            bg_a = int(150 * ease_out_cubic(clamp(frac / 0.2)))
        else:
            bg_a = int(150 * (1 - clamp((frac - 0.82) / 0.18)))
        p.fillRect(self.rect(), QtGui.QColor(8, 5, 18, bg_a))

        SHRINK_START = 0.80
        if frac < SHRINK_START:
            # phase 1: artist sweep + title detonation
            self._paint_artist(p, w, h, frac)
            if frac > 0.42:
                tf = clamp((frac - 0.42) / 0.45)
                self._paint_title(p, w, h, tf)
        else:
            # phase 2: everything morphs/shrinks into the corner badge
            mt = ease_in_cubic(clamp((frac - SHRINK_START) / (1.0 - SHRINK_START)))
            self._paint_shrink_morph(p, w, h, mt)

    def _paint_shrink_morph(self, p, w, h, mt):
        """Shrink the big centred artist + title together into the corner badge."""
        target = self._badge_rect(w, h)
        start_w, start_h = w * 0.7, 150
        start = QtCore.QRectF((w - start_w) / 2, h * 0.40, start_w, start_h)

        cur = QtCore.QRectF(
            lerp(start.x(), target.x(), mt),
            lerp(start.y(), target.y(), mt),
            lerp(start.width(), target.width(), mt),
            lerp(start.height(), target.height(), mt),
        )
        self._paint_badge_at(p, cur, panel_alpha=mt, eq=mt > 0.95)

        # --- ARTIST: shrinks from its big centred position (46pt, high on
        # screen) down to the small subtitle line inside the badge. Runs
        # slightly AHEAD of the frame (mt_a) so it tucks into the subtitle row
        # before the title arrives, avoiding overlap at the seam. ---
        mt_a = ease_out_cubic(clamp(mt * 1.35))
        a_pt = lerp(46, 9, mt_a)
        af = make_font(int(round(a_pt)))
        p.setFont(af)
        afm = p.fontMetrics()
        a_w = afm.horizontalAdvance(self._artist.title() if mt_a > 0.5 else self._artist)
        a_start_x = (w - a_w) / 2
        a_end_x = cur.x() + 16
        ax = lerp(a_start_x, a_end_x, mt_a)
        a_start_y = h * 0.30
        a_end_y = target.y() + 30 + 10
        ay = lerp(a_start_y, a_end_y, mt_a)
        artist_text = self._artist if mt_a < 0.5 else self._artist.title()
        draw_glow_text(p, int(ax), int(ay), artist_text, af,
                       core_color=(QtGui.QColor(CYAN) if mt_a > 0.7
                                   else QtGui.QColor(235, 252, 255)),
                       glow_color=CYAN,
                       glow_radius=int(lerp(14, 2, mt_a)),
                       glow_alpha=int(lerp(220, 110, mt_a)))

        # --- TITLE: scales from big to badge size, lands on settled spot. ---
        # End size is whatever fits the badge width (so long titles don't spill).
        avail = self._badge_rect(w, h).width() - 16 - 42
        end_f, end_text = self._fit_text(self._title, 13, 9, avail)
        end_pt = end_f.pointSize()
        font_pt = lerp(40, end_pt, mt)
        f = make_font(int(round(font_pt)))
        p.setFont(f)
        fm = p.fontMetrics()
        # use elided text only once we're near the badge; full text while big
        shown = self._title if mt < 0.85 else end_text
        title_w = fm.horizontalAdvance(shown)
        t_start_x = (w - title_w) / 2
        t_end_x = cur.x() + 16
        tx = lerp(t_start_x, t_end_x, mt)
        t_start_y = h * 0.58           # exactly where the title detonated
        t_end_y = cur.y() + 6 + fm.ascent()   # badge title row
        ty = lerp(t_start_y, t_end_y, mt)
        draw_glow_text(p, int(tx), int(ty), shown, f,
                       core_color=QtGui.QColor(255, 255, 255),
                       glow_color=MAGENTA,
                       glow_radius=int(lerp(16, 5, mt)), glow_alpha=190)

    def _paint_artist(self, p, w, h, frac):
        f = make_font(46)
        p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self._artist)
        y = int(h * 0.30)
        # sweep in from the right and settle at centre -- then HOLD there.
        # (the shrink phase carries it to the corner; it never scrolls off.)
        x = lerp(w, (w - tw) / 2, ease_out_cubic(clamp(frac / 0.45)))
        draw_glow_text(p, int(x), int(y), self._artist, f,
                       core_color=QtGui.QColor(235, 252, 255),
                       glow_color=CYAN, glow_radius=14, glow_alpha=220)

    def _paint_title(self, p, w, h, tf):
        style = self._style
        text = self._title
        f = make_font(40)
        p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        cx, cy = w / 2, h * 0.58

        if style == "flyin":
            self._title_flyin(p, text, cx, cy, tw, fm, tf)
        elif style == "zoombounce":
            self._title_zoombounce(p, text, cx, cy, tw, fm, tf)
        elif style == "glitch":
            self._title_glitch(p, text, cx, cy, tw, fm, tf)
        else:  # neonsweep
            self._title_neonsweep(p, text, cx, cy, tw, fm, tf, w)

    def _shockwave(self, p, cx, cy, tf):
        if tf < 0.5:
            r = lerp(10, 260, ease_out_cubic(tf / 0.5))
            a = int(180 * (1 - tf / 0.5))
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255, a), max(1, int(8 * (1 - tf / 0.5))))
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, cy), r, r)

    def _title_flyin(self, p, text, cx, cy, tw, fm, tf):
        self._shockwave(p, cx, cy, tf)
        x0 = cx - tw / 2
        n = len(text)
        for i, ch in enumerate(text):
            chw = fm.horizontalAdvance(text[:i])
            # each letter flies in from a random direction, staggered
            lt = clamp((tf - i / max(1, n) * 0.4) / 0.6)
            e = ease_out_back(lt)
            ang = (i * 47) % 360
            dist = (1 - e) * 400
            dx = math.cos(math.radians(ang)) * dist
            dy = math.sin(math.radians(ang)) * dist
            a = int(255 * clamp(lt * 1.5))
            col = QtGui.QColor(MAGENTA if i % 2 else CYAN); col.setAlpha(a)
            p.setPen(col)
            p.drawText(int(x0 + chw + dx), int(cy + dy), ch)

    def _title_zoombounce(self, p, text, cx, cy, tw, fm, tf):
        scale = ease_out_back(tf)
        p.save()
        p.translate(cx, cy)
        p.scale(scale, scale)
        # glow pulse
        pulse = 0.5 + 0.5 * math.sin(tf * math.pi)
        for blur, base_a in [(6, 70), (3, 120), (0, 255)]:
            col = QtGui.QColor(VIOLET if blur else WHITE)
            col.setAlpha(int(base_a * (0.6 + 0.4 * pulse)))
            p.setPen(col)
            for dx, dy in ([(0, 0)] if blur == 0 else
                           [(-blur, 0), (blur, 0), (0, -blur), (0, blur)]):
                p.drawText(int(-tw / 2 + dx), int(fm.height() / 4 + dy), text)
        p.restore()

    def _title_glitch(self, p, text, cx, cy, tw, fm, tf):
        rnd = random.Random(self._glitch_seed + int(time.time() * 30))
        x0 = cx - tw / 2
        settle = ease_out_cubic(tf)
        # RGB-split offset that shrinks as it settles
        split = (1 - settle) * 18 + 2 * (1 - settle)
        for col, off in [(QtGui.QColor(255, 0, 90, 200), -split),
                         (QtGui.QColor(0, 230, 255, 200), split)]:
            p.setPen(col)
            jitter = 0 if tf > 0.8 else rnd.randint(-3, 3)
            p.drawText(int(x0 + off), int(cy + jitter), text)
        # main white text with occasional block scramble before settle
        p.setPen(WHITE)
        if tf < 0.75 and rnd.random() < 0.3:
            scrambled = "".join(
                chr(rnd.randint(33, 90)) if rnd.random() < 0.25 else c
                for c in text)
            p.drawText(int(x0), int(cy), scrambled)
        else:
            p.drawText(int(x0), int(cy), text)

    def _title_neonsweep(self, p, text, cx, cy, tw, fm, tf, w):
        x0 = cx - tw / 2
        # base dim text
        p.setPen(QtGui.QColor(120, 80, 200, 200))
        p.drawText(int(x0), int(cy), text)
        # a bright sweep reveals it left-to-right
        sweep_x = lerp(x0 - 40, x0 + tw + 40, ease_out_cubic(tf))
        path = QtGui.QPainterPath()
        path.addText(float(x0), float(cy), p.font(), text)
        grad = QtGui.QLinearGradient(sweep_x - 80, 0, sweep_x + 20, 0)
        grad.setColorAt(0.0, QtGui.QColor(33, 230, 255, 0))
        grad.setColorAt(0.7, CYAN)
        grad.setColorAt(1.0, WHITE)
        p.fillPath(path, grad)

    # -- corner badge --------------------------------------------------------
    def _badge_rect(self, w, h):
        bw, bh = 360, 64
        margin = 16
        top_inset = 60   # clear the header row (search / buttons)
        return QtCore.QRectF(w - bw - margin, margin + top_inset, bw, bh)

    def _paint_badge(self, p, w, h, alpha):
        r = self._badge_rect(w, h)
        self._paint_badge_at(p, r, panel_alpha=1.0, eq=True)
        avail = r.width() - 16 - 42
        self._paint_badge_title(p, r, avail)
        p.setPen(QtGui.QColor(CYAN))
        f2 = QtGui.QFont(); f2.setFamilies(["Segoe UI", "Arial"]); f2.setPointSize(9)
        p.setFont(f2)
        afm = QtGui.QFontMetrics(f2)
        artist_text = self._artist.title()
        artist_w = afm.horizontalAdvance(artist_text)
        artist_overflow = max(0.0, artist_w - avail)
        artist_offset = self._badge_marquee_offset(artist_overflow)
        artist_clip = QtCore.QRectF(r.x() + 16, r.y() + 30, avail, 22)
        p.save()
        p.setClipRect(artist_clip)
        p.drawText(
            QtCore.QRectF(artist_clip.x() - artist_offset, artist_clip.y(), artist_w + 4, artist_clip.height()),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            artist_text,
        )
        p.restore()

    def _paint_badge_title(self, p, r, avail):
        max_font = make_font(13)
        max_fm = QtGui.QFontMetrics(max_font)
        if max_fm.horizontalAdvance(self._title) <= avail:
            draw_glow_text(p, int(r.x() + 16), int(r.y() + 6 + max_fm.ascent()),
                           self._title, max_font,
                           core_color=QtGui.QColor(255, 255, 255),
                           glow_color=MAGENTA, glow_radius=5, glow_alpha=190)
            return

        # Long titles gently pan left/right inside the badge instead of being elided.
        f = make_font(10)
        fm = QtGui.QFontMetrics(f)
        title_w = fm.horizontalAdvance(self._title)
        overflow = max(0.0, title_w - avail)
        offset = self._badge_marquee_offset(overflow)
        clip = QtCore.QRectF(r.x() + 16, r.y() + 2, avail, 26)
        p.save()
        p.setClipRect(clip)
        draw_glow_text(p, int(clip.x() - offset), int(r.y() + 6 + fm.ascent()),
                       self._title, f,
                       core_color=QtGui.QColor(255, 255, 255),
                       glow_color=MAGENTA, glow_radius=5, glow_alpha=190)
        p.restore()

    def _badge_marquee_offset(self, overflow: float) -> float:
        if overflow <= 0:
            return 0.0
        pause = 1.2
        speed = 34.0
        travel_time = max(1.0, overflow / speed)
        cycle = pause + travel_time + pause + travel_time
        phase = (time.time() % cycle)
        if phase < pause:
            return 0.0
        phase -= pause
        if phase < travel_time:
            return overflow * (phase / travel_time)
        phase -= travel_time
        if phase < pause:
            return overflow
        phase -= pause
        return overflow * (1.0 - phase / travel_time)

    def _fit_text(self, text, max_pt, min_pt, avail_w):
        """Return (font, text) where the font is the largest size in
        [min_pt, max_pt] that fits avail_w; if even min_pt overflows, the text
        is elided with an ellipsis at min_pt."""
        for pt in range(int(max_pt), int(min_pt) - 1, -1):
            f = make_font(pt)
            fm = QtGui.QFontMetrics(f)
            if fm.horizontalAdvance(text) <= avail_w:
                return f, text
        f = make_font(int(min_pt))
        fm = QtGui.QFontMetrics(f)
        return f, fm.elidedText(text, QtCore.Qt.TextElideMode.ElideRight, int(avail_w))

    def _paint_badge_at(self, p, r, panel_alpha=1.0, eq=True):
        # glow frame
        for grow, a in [(10, 50), (5, 90), (0, 230)]:
            rr = r.adjusted(-grow, -grow, grow, grow)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            grad = QtGui.QLinearGradient(rr.left(), 0, rr.right(), 0)
            c0 = QtGui.QColor(MAGENTA); c0.setAlpha(int(a * panel_alpha))
            c1 = QtGui.QColor(CYAN); c1.setAlpha(int(a * panel_alpha))
            grad.setColorAt(0, c0); grad.setColorAt(1, c1)
            p.setBrush(grad)
            p.drawRoundedRect(rr, 14, 14)
        # inner panel
        p.setBrush(QtGui.QColor(14, 9, 28, int(235 * panel_alpha)))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, int(40 * panel_alpha)), 1))
        p.drawRoundedRect(r, 12, 12)
        # equaliser glyph
        if eq:
            bx = r.right() - 34
            for i in range(3):
                bh2 = 8 + 14 * (0.5 + 0.5 * math.sin(time.time() * 4 + i))
                p.fillRect(QtCore.QRectF(bx + i * 8, r.center().y() - bh2 / 2 + 4, 5, bh2),
                           QtGui.QColor(CYAN))

    # -- info / fact cards ---------------------------------------------------
    def _paint_card(self, p, w, h):
        txt, c0, life = self._active_card
        age = time.time() - c0
        # slide-in from right, hold, slide-down out
        if age < 0.5:
            t = ease_out_cubic(age / 0.5); x_off = lerp(60, 0, t); a = t
        elif age > life - 0.6:
            t = clamp((age - (life - 0.6)) / 0.6); x_off = 0; a = 1 - t
        else:
            x_off = 0; a = 1.0
        card_label = "DID YOU KNOW"
        card_labels = ("Why they matter", "More about them", "Known for", "Artist facts", "Style", "Sound", "This track")
        if ": " in txt:
            maybe_label, maybe_text = txt.split(": ", 1)
            if maybe_label in card_labels:
                card_label = maybe_label.upper()
                txt = maybe_text
        cw = min(max(420, int(w * 0.42)), 520)
        ch = min(max(118, int(h * 0.18)), 150)
        margin = 18
        bottom_inset = 70   # clear the controls row + queue strip
        x = w - cw - margin + x_off
        y = h - ch - margin - bottom_inset
        rect = QtCore.QRectF(x, y, cw, ch)
        p.setOpacity(a)
        # glow edge
        for grow, ga in [(8, 40), (3, 80)]:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(VIOLET.red(), VIOLET.green(), VIOLET.blue(), ga))
            p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), 16, 16)
        p.setBrush(QtGui.QColor(16, 10, 30, 240))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 60, 172, 120), 1.5))
        p.drawRoundedRect(rect, 14, 14)
        # label
        p.setPen(QtGui.QColor(MAGENTA))
        f = QtGui.QFont("Segoe UI", 9); f.setBold(True); p.setFont(f)
        p.drawText(rect.adjusted(16, 8, -16, 0),
                   QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
                   card_label)
        # body: fit richer bio cards without clipping; trim only as a last resort.
        p.setPen(QtGui.QColor(235, 242, 255, 245))
        body_rect = rect.adjusted(16, 30, -16, -12)
        flags = int(QtCore.Qt.TextFlag.TextWordWrap)
        fitted_text = txt
        chosen_font = None
        for pt in (11, 10, 9, 8):
            f2 = QtGui.QFont("Georgia", pt)
            needed = QtGui.QFontMetrics(f2).boundingRect(body_rect.toRect(), flags, fitted_text)
            if needed.height() <= body_rect.height() or pt == 8:
                chosen_font = f2
                break
        fm = QtGui.QFontMetrics(chosen_font)
        while fitted_text:
            needed = fm.boundingRect(body_rect.toRect(), flags, fitted_text)
            if needed.height() <= body_rect.height():
                break
            words = fitted_text.split()
            if len(words) <= 8:
                break
            fitted_text = " ".join(words[:-3]).rstrip(" ,;:") + "..."
        p.setFont(chosen_font)
        p.drawText(body_rect, flags, fitted_text)
        p.setOpacity(1.0)

    def _paint_info_panel(self, p, w, h):
        label, text, t0, life = self._info_panel
        age = time.time() - t0
        if age < 0.35:
            a = ease_out_cubic(age / 0.35); sc = lerp(0.92, 1.0, a)
        elif age > life - 0.5:
            t = clamp((age - (life - 0.5)) / 0.5); a = 1 - t; sc = 1.0
        else:
            a = 1.0; sc = 1.0
        cw = min(460, max(360, int(w * 0.42)))
        ch = min(300, max(240, int(h * 0.46)))
        cx, cy = w / 2, h / 2
        rect = QtCore.QRectF(cx - cw / 2 * sc, cy - ch / 2 * sc, cw * sc, ch * sc)
        p.setOpacity(a)
        # glow frame
        for grow, ga in [(10, 50), (4, 90)]:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            grad = QtGui.QLinearGradient(rect.left() - grow, 0, rect.right() + grow, 0)
            c0 = QtGui.QColor(MAGENTA); c0.setAlpha(ga)
            c1 = QtGui.QColor(CYAN); c1.setAlpha(ga)
            grad.setColorAt(0, c0); grad.setColorAt(1, c1)
            p.setBrush(grad)
            p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), 18, 18)
        p.setBrush(QtGui.QColor(14, 9, 28, 244))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 50), 1.5))
        p.drawRoundedRect(rect, 16, 16)
        # label
        p.setPen(QtGui.QColor(CYAN))
        f = QtGui.QFont(); f.setFamilies(["Segoe UI", "Arial"]); f.setPointSize(9); f.setBold(True)
        p.setFont(f)
        p.drawText(rect.adjusted(20, 12, -20, 0),
                   QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop, label)
        # body: choose the largest font that fits the available panel space.
        p.setPen(QtGui.QColor(235, 242, 255, 245))
        body_rect = rect.adjusted(20, 38, -20, -16)
        flags = int(QtCore.Qt.TextFlag.TextWordWrap)
        chosen_font = None
        for pt in (10, 9, 8):
            f2 = QtGui.QFont(); f2.setFamilies(["Segoe UI", "Arial"]); f2.setPointSize(pt)
            needed = QtGui.QFontMetrics(f2).boundingRect(body_rect.toRect(), flags, text)
            if needed.height() <= body_rect.height() or pt == 8:
                chosen_font = f2
                break
        p.setFont(chosen_font)
        p.drawText(body_rect, flags, text)
        p.setOpacity(1.0)

    def _paint_lyric(self, p, w, h):
        _, current_text, _ = self._lyric_lines
        if not current_text:
            return
        age = time.time() - self._lyric_t0
        if self._lyric_fade_out:
            a = 1.0 - clamp(age / 0.55)
        else:
            a = 1.0 if age > 0.45 else ease_out_cubic(age / 0.45)

        style = self._lyric_style if self._lyric_style in LYRIC_STYLES else "neon"
        preset = {
            "clean": {
                "font": 34, "width": 0.78, "y": 0.70,
                "glow": QtGui.QColor(33, 230, 255), "panel": False,
                "cycle": False, "bars": False, "pulse": 0.025,
            },
            "neon": {
                "font": 38, "width": 0.96, "y": 0.64,
                "glow": QtGui.QColor(255, 60, 172), "panel": True,
                "cycle": True, "bars": False, "pulse": 0.045,
            },
            "visualiser": {
                "font": 36, "width": 0.94, "y": 0.62,
                "glow": QtGui.QColor(33, 230, 255), "panel": False,
                "cycle": True, "bars": True, "pulse": 0.055,
            },
            "big": {
                "font": 48, "width": 0.98, "y": 0.56,
                "glow": QtGui.QColor(177, 76, 255), "panel": False,
                "cycle": True, "bars": False, "pulse": 0.065,
            },
        }[style]

        # Future lyric visual tuning lives in the preset values above: font,
        # placement, colour cycling, and pulse strength.
        max_w = min(max(360, int(w * preset["width"])), max(320, w - 24))
        lyric_h = min(max(182, int(h * 0.38)), 260)
        rect = QtCore.QRectF((w - max_w) / 2, h * preset["y"] - lyric_h / 2, max_w, lyric_h)
        rect = rect.translated(0, math.sin(time.time() * 2.1) * 3.0)

        beat = 0.5 + 0.5 * math.sin(time.time() * 5.1)
        quick = max(0.0, 1.0 - age / 0.7)
        pulse = 1.0 + preset["pulse"] * (0.45 * beat + 0.55 * quick)

        p.setOpacity(a)
        if preset["panel"]:
            self._paint_lyric_panel(p, rect, preset["glow"], beat)
        if preset["bars"]:
            self._paint_lyric_visualiser_bars(p, rect, beat)

        p.save()
        p.translate(rect.center())
        p.scale(pulse, pulse)
        p.translate(-rect.center())

        current_area = rect.adjusted(22, 18, -22, -18)
        core, glow = self._lyric_colours(style, self._lyric_t0)
        self._draw_lyric_text(
            p, current_text, current_area, int(preset["font"]), core, glow,
            glow_alpha=150 if style != "clean" else 90,
            outline_alpha=210 if style in ("neon", "big") else 155,
        )

        p.restore()
        p.setOpacity(1.0)

    def _paint_lyric_panel(self, p, rect, glow, beat):
        glow = QtGui.QColor(glow)
        for grow, ga in [(18, 30), (8, 58), (2, 110)]:
            c = QtGui.QColor(glow)
            c.setAlpha(int(ga * (0.75 + 0.25 * beat)))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), 24, 24)
        p.setBrush(QtGui.QColor(7, 5, 18, 130))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), 1))
        p.drawRoundedRect(rect, 20, 20)

    def _paint_lyric_visualiser_bars(self, p, rect, beat):
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        bar_count = 18
        gap = 8
        bar_w = max(4, (rect.width() - gap * (bar_count - 1)) / bar_count)
        y = rect.bottom() + 18
        for i in range(bar_count):
            phase = time.time() * 4.5 + i * 0.7
            height = 10 + 38 * (0.25 + 0.75 * (0.5 + 0.5 * math.sin(phase)) * (0.6 + 0.4 * beat))
            col = QtGui.QColor(CYAN if i % 2 else MAGENTA)
            col.setAlpha(85)
            x = rect.x() + i * (bar_w + gap)
            p.setBrush(col)
            p.drawRoundedRect(QtCore.QRectF(x, y - height, bar_w, height), 4, 4)

    def _lyric_colours(self, style, now):
        if style == "clean":
            return QtGui.QColor(255, 255, 255), QtGui.QColor(33, 230, 255)
        colours = [MAGENTA, CYAN, VIOLET, QtGui.QColor(255, 235, 90)]
        pos = (now * 0.45) % len(colours)
        i = int(pos)
        t = pos - i
        return self._mix_colour(colours[i], colours[(i + 1) % len(colours)], t), colours[(i + 1) % len(colours)]

    def _mix_colour(self, a, b, t):
        return QtGui.QColor(
            int(lerp(a.red(), b.red(), t)),
            int(lerp(a.green(), b.green(), t)),
            int(lerp(a.blue(), b.blue(), t)),
        )

    def _draw_lyric_text(self, p, text, area, point_size, core, glow, glow_alpha=120, outline_alpha=180):
        if not text or point_size <= 0:
            return
        flags = int(
            QtCore.Qt.AlignmentFlag.AlignHCenter
            | QtCore.Qt.AlignmentFlag.AlignVCenter
            | QtCore.Qt.TextFlag.TextWordWrap
        )
        # Large lyric-video fonts need breathing room for wrap, pulse, glow and
        # outline. Measure the all-caps version we actually draw, then fit down
        # instead of letting Qt squeeze or clip the baseline.
        point_size = self._fit_lyric_point_size(text, area, point_size, flags)
        image, offset = self._cached_lyric_text_image(
            text, area, point_size, core, glow, glow_alpha, outline_alpha, flags
        )
        p.drawImage(QtCore.QPointF(area.x() - offset, area.y() - offset), image)

    def _cached_lyric_text_image(self, text, area, point_size, core, glow, glow_alpha, outline_alpha, flags):
        pad = 10
        width = max(1, int(math.ceil(area.width())) + pad * 2)
        height = max(1, int(math.ceil(area.height())) + pad * 2)
        key = (
            text, width, height, int(point_size),
            int(core.rgba()), int(glow.rgba()), int(glow_alpha), int(outline_alpha),
        )
        cached = self._lyric_text_cache.get(key)
        if cached is not None:
            return cached, pad
        if len(self._lyric_text_cache) > 8:
            self._lyric_text_cache.clear()

        img = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        ip = QtGui.QPainter(img)
        ip.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ip.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        f = make_font(point_size)
        f.setCapitalization(QtGui.QFont.Capitalization.AllUppercase)
        ip.setFont(f)
        local_area = QtCore.QRectF(pad, pad, area.width(), area.height())
        if glow_alpha > 0:
            c = QtGui.QColor(glow)
            c.setAlpha(glow_alpha)
            ip.setPen(c)
            for dx, dy in ((-5, 0), (5, 0), (0, -5), (0, 5), (-3, -3), (3, 3), (-3, 3), (3, -3)):
                ip.drawText(local_area.translated(dx, dy), flags, text)
        if outline_alpha > 0:
            outline = QtGui.QColor(4, 2, 13, outline_alpha)
            ip.setPen(outline)
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                ip.drawText(local_area.translated(dx, dy), flags, text)
        ip.setPen(core)
        ip.drawText(local_area, flags, text)
        ip.end()

        self._lyric_text_cache[key] = img
        return img, pad

    def _fit_lyric_point_size(self, text, area, point_size, flags):
        min_size = max(16, int(point_size * 0.48))
        measure_text = (text or "").upper()
        probe = QtGui.QFont()
        probe.setFamilies(["Segoe UI Black", "Arial Black", "Segoe UI", "Arial"])
        probe.setBold(True)
        probe.setCapitalization(QtGui.QFont.Capitalization.AllUppercase)
        if hasattr(QtGui.QFont.Weight, "Black"):
            probe.setWeight(QtGui.QFont.Weight.Black)
        for size in range(int(point_size), min_size - 1, -2):
            probe.setPointSize(size)
            fm = QtGui.QFontMetrics(probe)
            bounds = fm.boundingRect(area.toRect(), flags, measure_text)
            if bounds.width() <= area.width() and bounds.height() <= area.height() - 22:
                return size
        return min_size




