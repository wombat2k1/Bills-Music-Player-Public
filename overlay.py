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
        # spread the cards over the window from "now" to ~90% of the track,
        # leaving the first few seconds for the intro to play out.
        elapsed = time.time() - self._play_t0
        start_frac = clamp((elapsed + 5.0) / self._track_len, 0.05, 0.5)
        end_frac = 0.92
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
        # sweep in from the right and settle at centre — then HOLD there.
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
        # title: shrink-to-fit within the badge (leave room for the eq glyph)
        avail = r.width() - 16 - 42
        f, shown = self._fit_text(self._title, 13, 9, avail)
        p.setFont(f)
        fm = p.fontMetrics()
        draw_glow_text(p, int(r.x() + 16), int(r.y() + 6 + fm.ascent()),
                       shown, f,
                       core_color=QtGui.QColor(255, 255, 255),
                       glow_color=MAGENTA, glow_radius=5, glow_alpha=190)
        p.setPen(QtGui.QColor(CYAN))
        f2 = QtGui.QFont(); f2.setFamilies(["Segoe UI", "Arial"]); f2.setPointSize(9)
        p.setFont(f2)
        afm = QtGui.QFontMetrics(f2)
        artist_shown = afm.elidedText(self._artist.title(),
                                      QtCore.Qt.TextElideMode.ElideRight, int(avail))
        p.drawText(r.adjusted(16, 30, -42, -6),
                   QtCore.Qt.AlignmentFlag.AlignLeft, artist_shown)

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
        cw, ch = 420, 110
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
                   "DID YOU KNOW")
        # body
        p.setPen(QtGui.QColor(235, 242, 255, 245))
        f2 = QtGui.QFont("Georgia", 11); p.setFont(f2)
        p.drawText(rect.adjusted(16, 30, -16, -12),
                   int(QtCore.Qt.TextFlag.TextWordWrap),
                   txt)
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
        cw, ch = 360, 220
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
        # body
        p.setPen(QtGui.QColor(235, 242, 255, 245))
        f2 = QtGui.QFont(); f2.setFamilies(["Segoe UI", "Arial"]); f2.setPointSize(11)
        p.setFont(f2)
        p.drawText(rect.adjusted(20, 38, -20, -16),
                   int(QtCore.Qt.TextFlag.TextWordWrap), text)
        p.setOpacity(1.0)


