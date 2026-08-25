"""Coding-Plan desktop floating monitor (multi-provider).

A small always-on-top floating ball showing the PRIMARY provider's usage.
- Left-click cycles the displayed level within the primary provider.
- Hover shows a detail card listing ALL providers with per-level info.
- Right-click: settings / switch primary provider / refresh / quit.

Providers are pluggable (see providers/). Adding one = write a subclass.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint, QRect, Signal, QSize, QThread
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QBrush,
    QIcon,
    QPixmap,
    QAction,
    QRadialGradient,
    QLinearGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QWidget,
    QVBoxLayout,
    QLabel,
    QHBoxLayout,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QTextEdit,
    QLineEdit,
    QSpinBox,
    QPushButton,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QFrame,
    QProgressBar,
    QSizePolicy,
    QGraphicsDropShadowEffect,
    QStackedWidget,
    QSlider,
    QButtonGroup,
    QRadioButton,
)

from providers import build_provider, ProviderSnapshot
from providers.base import ProviderBase

BALL_SIZE = 88
CARD_WIDTH = 300
CARD_HEIGHT = 240


@dataclass
class Theme:
    name: str
    # Ball
    ball_center: str
    ball_edge: str
    ball_glow: tuple  # (r,g,b,a)
    ball_text: str
    ball_subtext: str
    ball_track: str
    ball_inner_gap: int  # padding between ring and text
    pct_upper: bool  # percentage positioned upper (light) vs center (dark)
    # Card
    card_bg: str  # rgba
    card_border: str
    card_text: str
    card_subtext: str
    card_footer: str
    card_translucent: bool  # semi-transparent bg (light) vs opaque (dark)
    nested_cards: bool  # each provider in own bordered box (dark) vs flat panel (light)
    subcard_bg: str
    subcard_border: str
    primary_border: str
    primary_tag_bg: str
    row_divider: str  # divider line between rows inside a provider block
    # accents
    accent_blue: str
    accent_balance: str  # DeepSeek balance text color


LIGHT = Theme(
    name="light",
    ball_center="#e8edf5", ball_edge="#cdd6e8", ball_glow=(59, 139, 255, 70),
    ball_text="#1a2540", ball_subtext="#6b7280", ball_track="#d1d9e6",
    ball_inner_gap=10, pct_upper=True,
    card_bg="rgba(248,250,255,225)", card_border="rgba(255,255,255,0.6)",
    card_text="#1f2937", card_subtext="#6b7280", card_footer="#9ca3af",
    card_translucent=True, nested_cards=False,
    subcard_bg="transparent", subcard_border="transparent",
    primary_border="#3b8bff", primary_tag_bg="#3b8bff",
    row_divider="rgba(0,0,0,0.06)",
    accent_blue="#3b8bff", accent_balance="#2563eb",
)

DARK = Theme(
    name="dark",
    ball_center="#2a3a5c", ball_edge="#1a2540", ball_glow=(59, 139, 255, 90),
    ball_text="#ffffff", ball_subtext="#a0a8b8", ball_track="#2a2f3e",
    ball_inner_gap=8, pct_upper=False,
    card_bg="rgba(20,25,40,250)", card_border="rgba(255,255,255,0.08)",
    card_text="#ffffff", card_subtext="#a0a8b8", card_footer="#6b7280",
    card_translucent=False, nested_cards=True,
    subcard_bg="rgba(30,42,68,180)", subcard_border="rgba(255,255,255,0.06)",
    primary_border="#3b8bff", primary_tag_bg="#3b8bff",
    row_divider="rgba(255,255,255,0.05)",
    accent_blue="#3b8bff", accent_balance="#5b8bff",
)

THEMES = {"light": LIGHT, "dark": DARK}


def current_theme() -> Theme:
    return THEMES.get(_app_config.get("theme", "dark"), DARK)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    """Writable, persistent location for user data (config.json, state.json).

    When frozen this is the exe's own directory so the user's credentials
    survive across launches and can be written back (cookie auto-refresh).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir() -> Path:
    """Read-only bundled resources (icons). When frozen these live in the
    PyInstaller _MEIPASS extraction dir, so the exe stays a single portable
    file. In dev they sit next to the source."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


CONFIG_PATH = _config_dir() / "config.json"


def percent_color(p: float, warn: float, crit: float) -> QColor:
    if p >= crit:
        return QColor("#e74c3c")
    if p >= warn:
        return QColor("#f39c12")
    return QColor("#2ecc71")


def _fmt_pct(v: float) -> str:
    """Integer percent if value is whole, else two decimals."""
    return f"{int(v)}%" if v == int(v) else f"{v:.2f}%"


def _default_label_for(ptype: str) -> str:
    """Stable default display name for a provider type.

    Counts providers of the same type so the Nth one gets `<prefix>-N`.
    Used by the sidebar list, primary combo, detail-card labels, and the
    settings dialog's right-side editor. Single source of truth so the
    list item and the form field always agree.
    """
    prefix_map = {
        "volcengine": "字节模型",
        "minimax": "MiniMax",
        "deepseek": "DeepSeek",
        "qoder": "Qoder",
    }
    count = sum(
        1 for p in _app_config.get("providers", [])
        if p.get("type") == ptype
    )
    prefix = prefix_map.get(ptype, ptype or "平台")
    return f"{prefix}-{count}"


def _auto_labels() -> dict:
    """{provider_id: display_label} for every configured provider.

    Counts same-type providers up to and including this one, saved or not,
    so the Nth provider of a type is always `<prefix>-N` unless the user
    explicitly renamed it. Stable: list order is preserved, labels don't
    shift on reload, and adding a fresh provider never collides with a
    saved sibling because the saved label takes precedence over the
    auto-generated one for that same slot.
    """
    prefix_map = {
        "volcengine": "字节模型",
        "minimax": "MiniMax",
        "deepseek": "DeepSeek",
        "qoder": "Qoder",
    }
    counts: dict = {}
    out: dict = {}
    for p in _app_config.get("providers", []):
        ptype = p.get("type", "")
        counts[ptype] = counts.get(ptype, 0) + 1
        saved = p.get("label", "")
        if saved:
            out[p["id"]] = saved
        else:
            prefix = prefix_map.get(ptype, ptype or "平台")
            out[p["id"]] = f"{prefix}-{counts[ptype]}"
    return out


def _parse_curl_or_cookie(text: str) -> dict:
    """Accept a full `Copy as cURL` blob or a bare cookie string."""
    text = text.strip()
    if not text:
        return {}
    import re

    looks_curl = any(
        tok in text for tok in ("curl ", "curl.exe", "-H ", "--header", "-b ", "--cookie")
    )
    cookie = ""
    csrf = ""
    x_web_id = ""
    if looks_curl:
        m = re.search(r"-b\s+'([^']*)'", text) or re.search(r'-b\s+"([^"]*)"', text)
        if m:
            cookie = m.group(1)
        if not cookie:
            m = re.search(r"-H\s+'[Cc]ookie:\s*([^']+)'", text) or re.search(
                r'-H\s+"[Cc]ookie:\s*([^"]+)"', text
            )
            if m:
                cookie = m.group(1)
        m = re.search(r"-H\s+'[Xx]-[Cc]srf-[Tt]oken:\s*([^']+)'", text) or re.search(
            r'-H\s+"[Xx]-[Cc]srf-[Tt]oken:\s*([^"]+)"', text
        )
        if m:
            csrf = m.group(1).strip()
        m = re.search(r"-H\s+'[Xx]-[Ww]eb-[Ii]d:\s*([^']+)'", text) or re.search(
            r'-H\s+"[Xx]-[Ww]eb-[Ii]d:\s*([^"]+)"', text
        )
        if m:
            x_web_id = m.group(1).strip()
    else:
        cookie = text

    if not csrf:
        for pair in cookie.split(";"):
            pair = pair.strip()
            if pair.startswith("csrfToken="):
                csrf = pair.split("=", 1)[1]
                break
    result = {"cookie": cookie}
    if csrf:
        result["csrf_token"] = csrf
    if x_web_id:
        result["x_web_id"] = x_web_id
    return result


# ---------------------------------------------------------------------------
# config loading / migration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.json, migrating the old single-provider format if needed."""
    if not CONFIG_PATH.exists():
        return {"primary": "", "providers": [], "poll_interval_sec": 10,
                "warning_percent": 80, "critical_percent": 95}
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Old format had top-level "cookie" / "csrf_token" -> migrate.
    if "providers" not in cfg and "cookie" in cfg:
        cfg = {
            "primary": "volcengine",
            "poll_interval_sec": cfg.get("poll_interval_sec", 10),
            "warning_percent": cfg.get("warning_percent", 80),
            "critical_percent": cfg.get("critical_percent", 95),
            "providers": [
                {
                    "id": "volcengine",
                    "type": "volcengine",
                    "label": "火山方舟-coding plan",
                    "credentials": {
                        "cookie": cfg["cookie"],
                        "csrf_token": cfg.get("csrf_token", ""),
                        "x_web_id": cfg.get("x_web_id", ""),
                    },
                }
            ],
        }
    cfg.setdefault("primary", cfg["providers"][0]["id"] if cfg.get("providers") else "")
    cfg.setdefault("providers", [])
    cfg.setdefault("poll_interval_sec", 10)
    cfg.setdefault("warning_percent", 80)
    cfg.setdefault("critical_percent", 95)
    cfg.setdefault("theme", "dark")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# module-level save hook (referenced by providers that need to persist creds)
def _save_config() -> None:
    save_config(_app_config)


_app_config: dict = {}


# ---------------------------------------------------------------------------
# FloatBall
# ---------------------------------------------------------------------------

class FloatBall(QWidget):
    level_cycled = Signal()
    hovered = Signal(bool)
    drag_started = Signal()
    settings_requested = Signal()
    refresh_requested = Signal()
    quit_requested = Signal()

    def __init__(self):
        super().__init__()
        self.snapshot: ProviderSnapshot | None = None
        self.current_level = ""  # level key within the primary provider
        self.warn = 80
        self.crit = 95
        self._error = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFixedSize(BALL_SIZE, BALL_SIZE)
        self._drag_offset: QPoint | None = None
        self._dragged = False
        self._dragging = False

    def refresh_theme(self):
        # repaint() forces immediate synchronous repaint (update() can be
        # suppressed by the graphics effect / event coalescing).
        self.repaint()

    def paintEvent(self, _event):
        th = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Soft outer glow (theme glow color).
        glow_rect = QRect(0, 0, BALL_SIZE, BALL_SIZE)
        gr, gg, gb, ga = th.ball_glow
        glow_grad = QRadialGradient(glow_rect.center(), BALL_SIZE / 2)
        glow_grad.setColorAt(0.45, QColor(gr, gg, gb, ga))
        glow_grad.setColorAt(1.0, QColor(gr, gg, gb, 0))
        painter.setBrush(QBrush(glow_grad))
        painter.setPen(QPen(QColor(0, 0, 0, 0)))
        painter.drawEllipse(glow_rect)

        # Ball body: gradient from theme ball_center -> ball_edge.
        body = QRect(6, 6, BALL_SIZE - 12, BALL_SIZE - 12)
        grad = QRadialGradient(
            body.center().x() - 10, body.center().y() - 10, body.width() / 2 + 10
        )
        grad.setColorAt(0.0, QColor(th.ball_center))
        grad.setColorAt(1.0, QColor(th.ball_edge))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(QColor(0, 0, 0, 0)))
        painter.drawEllipse(body)

        # Subtle inner highlight (top-left, glassy).
        hl_grad = QRadialGradient(
            body.center().x() - 14, body.center().y() - 14, 16
        )
        hl_grad.setColorAt(0.0, QColor(255, 255, 255, 50))
        hl_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(hl_grad))
        painter.drawEllipse(body)

        # Progress ring: thin, subtle track + colored fill.
        ring_rect = body.adjusted(5, 5, -5, -5)
        ring_w = 4

        if self._error or not self.snapshot:
            track = QPen(QColor(th.ball_track), ring_w)
            track.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(track)
            painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
            painter.drawArc(ring_rect, 0, 360 * 16)
            painter.setPen(QColor("#e74c3c"))
            painter.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
            painter.drawText(body, Qt.AlignmentFlag.AlignCenter, "!")
            return

        q = self.snapshot.get(self.current_level) or self.snapshot.levels[0]

        # Track (empty portion) - theme track color.
        track = QPen(QColor(th.ball_track), ring_w)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
        painter.drawArc(ring_rect, 0, 360 * 16)

        ball_text = QColor(th.ball_text)
        ball_subtext = QColor(th.ball_subtext)

        if q.display_text:
            fill = QPen(QColor(th.accent_balance), ring_w)
            fill.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(fill)
            painter.drawArc(ring_rect, 90 * 16, -360 * 16)
            painter.setPen(ball_text)
            painter.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
            painter.drawText(
                QRect(0, 26, BALL_SIZE, 18),
                Qt.AlignmentFlag.AlignCenter, q.display_text,
            )
        else:
            color = percent_color(q.used_percent, self.warn, self.crit)
            fill = QPen(color, ring_w)
            fill.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(fill)
            arc = int(q.used_percent / 100.0 * 360 * 16)
            painter.drawArc(ring_rect, 90 * 16, -arc)

            painter.setPen(ball_text)
            painter.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
            painter.drawText(
                QRect(0, 24, BALL_SIZE, 22),
                Qt.AlignmentFlag.AlignCenter, _fmt_pct(q.used_percent),
            )

        # Label line.
        sub_color = QColor(ball_subtext)
        sub_color.setAlpha(220)
        painter.setPen(sub_color)
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRect(0, 48, BALL_SIZE, 14),
            Qt.AlignmentFlag.AlignCenter, q.label,
        )

        # Countdown line (if available).
        if q.reset_timestamp is not None:
            cd_color = QColor(ball_subtext)
            cd_color.setAlpha(160)
            painter.setPen(cd_color)
            painter.setFont(QFont("Segoe UI", 7))
            painter.drawText(
                QRect(0, 62, BALL_SIZE, 12),
                Qt.AlignmentFlag.AlignCenter, q.countdown(),
            )

    # interaction
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self._dragged = False

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._dragging:
                self._dragging = True
                self.drag_started.emit()
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._dragged = True

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_drag = self._dragged
            self._drag_offset = None
            self._dragged = False
            # Keep _dragging true until next enter so the leave burst is swallowed.
            QTimer.singleShot(0, self._clear_dragging)
            if not was_drag:
                self.level_cycled.emit()

    def _clear_dragging(self):
        self._dragging = False

    def enterEvent(self, event):
        if getattr(self, "_dragging", False):
            return
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if getattr(self, "_dragging", False):
            return
        self.hovered.emit(False)
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        self._popup_menu(event.globalPos())

    def _popup_menu(self, pos):
        # Built dynamically by the app so provider list is current.
        if hasattr(self, "_build_menu"):
            menu = self._build_menu()
        else:
            menu = QMenu(self)
        menu.exec(pos)

    def update_snapshot(self, snap: ProviderSnapshot, level_key: str):
        self.snapshot = snap
        self.current_level = level_key
        self.update()

    def set_error(self, errored: bool):
        self._error = errored
        self.update()

    def has_valid_snapshot(self) -> bool:
        """True when the ball shows real data, not the red '!' error state.

        Used to decide whether hovering pops the detail card: before any
        provider is configured or has fetched successfully we keep it hidden.
        """
        return self.snapshot is not None and not self._error


# ---------------------------------------------------------------------------
# DetailCard
# ---------------------------------------------------------------------------

class _ProgressBar(QProgressBar):
    """Slim colored progress bar; color set via stylesheet per state."""

    def __init__(self):
        super().__init__()
        self.setRange(0, 1000)
        self.setTextVisible(False)
        self.setFixedHeight(6)
        self._color = "#2ecc71"

    def set_value(self, used_percent: float, color: QColor):
        self._color = color.name()
        self.setValue(int(used_percent * 10))
        self.setStyleSheet(
            "QProgressBar { background: rgba(255,255,255,0.08); border: none;"
            " border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {self._color}; border-radius: 3px; }}"
        )


def _make_provider_card(snap: ProviderSnapshot, is_primary: bool,
                        warn: float, crit: float) -> QFrame:
    """Build one provider's detail block as a QFrame."""
    th = current_theme()
    card = QFrame()
    card.setObjectName("providerCard")
    if th.nested_cards:
        border = th.primary_border if is_primary else th.subcard_border
        bg = th.subcard_bg
        radius = "14px"
    else:
        # Light flat mode: transparent bg, only primary gets a bottom accent.
        border = "transparent"
        bg = "transparent"
        radius = "8px"
    card.setStyleSheet(
        f"#providerCard {{ background: {bg}; border: 1px solid {border};"
        f" border-radius: {radius}; }}"
    )
    layout = QVBoxLayout(card)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(5)

    # Title row.
    title_row = QHBoxLayout()
    title_row.setContentsMargins(0, 0, 0, 0)
    title_row.setSpacing(6)
    name = QLabel(_provider_label(snap.provider_id))
    name.setStyleSheet(
        f"color: {th.card_text}; font-size: 12px; font-weight: 600; background: transparent;"
    )
    title_row.addWidget(name)
    if is_primary:
        tag = QLabel("主")
        tag.setStyleSheet(
            f"background: {th.primary_tag_bg}; color: #ffffff; font-size: 9px; font-weight: 700;"
            " padding: 1px 6px; border-radius: 4px;"
        )
        tag.setFixedHeight(15)
        title_row.addWidget(tag)
    title_row.addStretch()
    if not snap.ok:
        err = QLabel(snap.error[:24])
        err.setStyleSheet("color: #e74c3c; font-size: 10px; background: transparent;")
        err.setToolTip(snap.error)
        title_row.addWidget(err)
    layout.addLayout(title_row)

    if snap.ok:
        for q in snap.levels:
            if q.display_text:
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(8)
                lbl = QLabel(q.label)
                lbl.setStyleSheet(
                    f"color: {th.card_subtext}; font-size: 11px; background: transparent;"
                )
                val = QLabel(q.display_text)
                val.setStyleSheet(
                    f"color: {th.accent_balance}; font-size: 14px; font-weight: 600; background: transparent;"
                )
                row.addWidget(lbl)
                row.addWidget(val, 1)
                layout.addLayout(row)
            else:
                # Label + percentage on top, full-width bar below.
                info_row = QHBoxLayout()
                info_row.setContentsMargins(0, 0, 0, 0)
                info_row.setSpacing(8)
                lbl = QLabel(q.label)
                lbl.setStyleSheet(
                    f"color: {th.card_subtext}; font-size: 11px; background: transparent;"
                )
                right = QLabel(
                    f"{_fmt_pct(q.used_percent)}  剩{_fmt_pct(q.remaining_percent)}"
                )
                right.setStyleSheet(
                    f"color: {th.card_subtext}; font-size: 10px; background: transparent;"
                )
                right.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                info_row.addWidget(lbl)
                info_row.addStretch()
                info_row.addWidget(right)
                layout.addLayout(info_row)
                bar = _ProgressBar()
                bar.set_value(
                    q.used_percent, percent_color(q.used_percent, warn, crit)
                )
                layout.addWidget(bar)
                # Countdown to next reset on its own line.
                if q.reset_timestamp is not None:
                    cd = QLabel(f"距重置 {q.countdown()}")
                    cd.setStyleSheet(
                        f"color: {th.card_footer}; font-size: 9px; background: transparent;"
                    )
                    layout.addWidget(cd)
        for line in snap.extra_lines:
            ex = QLabel(line)
            ex.setStyleSheet(
                f"color: {th.card_footer}; font-size: 10px; background: transparent;"
            )
            layout.addWidget(ex)
    return card


class DetailCard(QWidget):
    hovered = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._container = QWidget(self)
        th = current_theme()
        self._container.setStyleSheet(
            f"background-color: {th.card_bg}; border-radius: 16px;"
            f" border: 1px solid {th.card_border};"
        )
        self._outer = QVBoxLayout(self._container)
        self._outer.setContentsMargins(16, 14, 16, 14)
        self._outer.setSpacing(10)
        self._title = QLabel("Coding Plan 额度")
        self._title.setStyleSheet(
            f"color: {th.card_text}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        self._outer.addWidget(self._title)
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setSpacing(10)
        self._outer.addLayout(self._cards_layout)
        self._updated = QLabel("")
        self._updated.setStyleSheet(
            f"color: {th.card_footer}; font-size: 9px; background: transparent;"
        )
        self._updated.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._outer.addWidget(self._updated)
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        # Soft drop shadow on the card.
        shadow = QGraphicsDropShadowEffect(self._container)
        shadow.setBlurRadius(28)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 6)
        self._container.setGraphicsEffect(shadow)

    def refresh_theme(self):
        th = current_theme()
        self._container.setStyleSheet(
            f"background-color: {th.card_bg}; border-radius: 16px;"
            f" border: 1px solid {th.card_border};"
        )
        self._title.setStyleSheet(
            f"color: {th.card_text}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        self._updated.setStyleSheet(
            f"color: {th.card_footer}; font-size: 9px; background: transparent;"
        )

    def resizeEvent(self, event):
        # Keep the rounded-bg container filling the card.
        self._container.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def enterEvent(self, event):
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered.emit(False)
        super().leaveEvent(event)

    def render_snapshots(self, snapshots: list[ProviderSnapshot], primary_id: str,
                         warn: float, crit: float, updated_at: int):
        # Clear old cards.
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        for snap in snapshots:
            card = _make_provider_card(snap, snap.provider_id == primary_id, warn, crit)
            self._cards_layout.addWidget(card)
        self._title.setText("Coding Plan 额度")
        self._updated.setText(
            f"更新于 {time.strftime('%H:%M:%S', time.localtime(updated_at))}"
        )
        # Compute height on next tick so layout has settled after reparent.
        QTimer.singleShot(0, self._fit_height)

    def _fit_height(self):
        """Compute card height by summing all child widget heights."""
        self._outer.activate()
        # Walk every widget in the container and sum their heights + spacing.
        total = 0
        for i in range(self._outer.count()):
            item = self._outer.itemAt(i)
            if item.widget():
                w = item.widget()
                w.adjustSize()
                total += w.sizeHint().height()
            elif item.layout():
                sub = item.layout()
                sub.activate()
                total += sub.sizeHint().height()
        total += self._outer.spacing() * max(self._outer.count() - 1, 0)
        total += self._outer.contentsMargins().top() + self._outer.contentsMargins().bottom()
        self.setFixedSize(CARD_WIDTH, max(total + 8, 80))


def _provider_label(pid: str) -> str:
    return _auto_labels().get(pid, pid)


# ---------------------------------------------------------------------------
# Settings dialog (multi-provider)
# ---------------------------------------------------------------------------

class _QoderCookieProbe(QThread):
    """Single-shot, non-intrusive: ask Chrome if the qoder session cookie
    is present. Reuses the user's existing tab via Network.getCookies -- no
    new tab, no navigation, no focus theft. Takes ~50 ms.

    Polled every 3 s after the user clicks "打开 Qoder 登录窗口" so the
    settings dialog's status line flips to ✓ as soon as login completes.
    """

    ok = Signal()
    fail = Signal(str)

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self._port = port

    def run(self) -> None:
        from providers.qoder import QoderCdp

        try:
            if QoderCdp(self._port).has_session_cookie():
                self.ok.emit()
            else:
                self.fail.emit("no cookie")
        except Exception as e:
            self.fail.emit(str(e))


class SettingsDialog(QDialog):
    theme_changed = Signal()
    applied = Signal()  # emitted on "保存": persist + take effect WITHOUT closing

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Coding Plan 监控 - 设置")
        self.setModal(True)
        self.resize(1100, 800)
        self.setMinimumSize(1100, 800)
        self._editing_index: int | None = None
        self._original_theme = _app_config.get("theme", "dark")
        self._apply_theme_style()
        self._build()

    def _apply_theme_style(self):
        th = current_theme()
        # Resolve arrow icon path relative to this file (frozen-safe).
        _icon_dir = _resource_dir() / "icon"
        _arrow_path = _icon_dir / "下拉.png"
        if th.name == "dark":
            bg, panel, input_bg, input_border = "#141823", "#1c2230", "#222a3a", "#2a3346"
            text, subtext, accent = "#ffffff", "#8892a6", th.accent_blue
            divider = "#222a3a"
        else:
            bg, panel, input_bg, input_border = "#ffffff", "#f9fafb", "#ffffff", "#e5e7eb"
            text, subtext, accent = "#1f2937", "#6b7280", th.accent_blue
            divider = "#f0f0f0"
        self._th = th
        self._c_bg, self._c_panel, self._c_input, self._c_border = bg, panel, input_bg, input_border
        self._c_text, self._c_sub, self._c_accent, self._c_div = text, subtext, accent, divider
        self.setStyleSheet(f"""
            QDialog {{ background: {bg}; }}
            QLabel {{ color: {text}; font-size: 12px; background: transparent; }}
            QLabel#sectionHeader {{ color: {text}; font-size: 14px; font-weight: 600; }}
            QLabel#cardHeader {{ color: {text}; font-size: 13px; font-weight: 600; background: transparent; }}
            QLabel#pageTitle {{ color: {text}; font-size: 20px; font-weight: 700; background: transparent; }}
            QLabel#pageSub {{ color: {subtext}; font-size: 12px; background: transparent; }}
            QLabel#aboutVal {{ color: {text}; font-size: 12px; background: transparent; }}
            QLabel#fieldLabel {{ color: {subtext}; font-size: 11px; }}
            QLabel#navGroup {{ color: {subtext}; font-size: 10px; font-weight: 700; }}
            QLabel#navItem {{ color: {text}; font-size: 12px; }}
            QLabel#navItemSel {{ color: {accent}; font-size: 12px; font-weight: 600; }}
            QLabel#addHint {{ color: {subtext}; font-size: 10px; font-weight: 600; }}
            QLabel#dot {{ background: {accent}; border-radius: 4px; }}
            QListWidget {{
                background: transparent; border: none; font-size: 12px; outline: none;
            }}
            QListWidget::item {{ padding: 0px; border-radius: 6px; color: {text}; }}
            QListWidget::item:selected {{ background: {accent}; color: #ffffff; }}
            QListWidget::item:selected:!active {{ background: {accent}; color: #ffffff; }}
            QListWidget::item:hover {{ background: {panel}; color: {text}; }}
            QListWidget::item:selected:hover {{ background: {accent}; color: #ffffff; }}
            QComboBox {{
                background: {input_bg}; border: 1px solid {input_border}; border-radius: 8px;
                padding: 7px 10px; font-size: 12px; color: {text};
                min-height: 22px;
            }}
            QComboBox:focus {{ border: 1px solid {accent}; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding; subcontrol-position: center right;
                width: 24px; border: none; background: transparent;
            }}
            QComboBox::down-arrow {{
                image: url({_arrow_path.as_posix()});
                width: 14px; height: 14px;
            }}
            QComboBox QAbstractItemView {{
                background: {input_bg}; border: 1px solid {input_border}; border-radius: 6px;
                padding: 4px; outline: none; color: {text};
                selection-background-color: {accent}; selection-color: #ffffff;
            }}
            QLineEdit, QTextEdit, QSpinBox {{
                background: {input_bg}; border: 1px solid {input_border}; border-radius: 8px;
                padding: 7px 10px; font-size: 12px; color: {text};
                selection-background-color: {accent};
            }}
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus {{
                border: 1px solid {accent};
            }}
            QPushButton {{
                background-color: {input_bg}; border: 1px solid {input_border}; border-radius: 8px;
                padding: 8px 16px; color: {text}; font-size: 12px; min-width: 60px;
            }}
            QPushButton:hover {{ background-color: {panel}; border: 1px solid {accent}; }}
            QPushButton#primary {{
                background-color: {accent}; border: 1px solid {accent}; color: #ffffff; font-weight: 600;
                border-radius: 8px; padding: 8px 24px;
            }}
            QPushButton#primary:hover {{ background-color: {accent}; }}
            QWidget#footer {{ background: {bg}; border-top: 1px solid {divider}; }}
            QWidget#sidebar {{ background: {bg}; border-right: 1px solid {divider}; }}
            QWidget#rightArea {{ background: {bg}; }}
            QWidget#titleBar {{ background: {bg}; border-bottom: 1px solid {divider}; }}
            QFrame#card {{ background: {panel}; border: 1px solid {input_border}; border-radius: 10px; }}
            QSlider::groove:horizontal {{ height: 4px; background: {input_border}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ width: 14px; margin: -6px 0; border-radius: 7px;
                background: {accent}; }}
        """)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Title bar.
        self._title_bar = title_bar = QWidget()
        title_bar.setObjectName("titleBar")
        title_bar.setFixedHeight(42)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(18, 0, 18, 0)
        self._dot = dot = QLabel()
        dot.setObjectName("dot")
        dot.setFixedSize(8, 8)
        tb.addWidget(dot)
        tb.addSpacing(10)
        t = QLabel("Coding Plan 设置")
        t.setObjectName("cardHeader")
        tb.addWidget(t)
        tb.addStretch()
        root.addWidget(title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # ---- Left sidebar ----
        self._side = side = QWidget()
        side.setObjectName("sidebar")
        side.setMinimumWidth(320)
        side.setMaximumWidth(340)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 14, 10, 14)
        sl.setSpacing(4)

        sl.addWidget(self._navgroup("PROVIDERS"))
        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_select_row)
        sl.addWidget(self.list_widget)
        # Add provider area (no label input: name is auto-generated when a
        # provider is selected — see _default_label_for).
        add_hint = QLabel("添加模型")
        add_hint.setObjectName("addHint")
        sl.addWidget(add_hint)
        self.type_combo = QComboBox()
        from providers import PROVIDER_REGISTRY
        _type_labels = {"volcengine": "火山方舟-coding plan", "minimax": "MiniMax",
                        "deepseek": "DeepSeek", "qoder": "Qoder"}
        for tname, cls in PROVIDER_REGISTRY.items():
            self.type_combo.addItem(_type_labels.get(tname, tname), tname)
        # Combo + 添加 button on the same row (saves vertical space and reads
        # as one combined control).
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(8)
        add_row.addWidget(self.type_combo, 1)
        self.add_btn = QPushButton("添加")
        self.add_btn.clicked.connect(self._add_provider)
        add_row.addWidget(self.add_btn)
        sl.addLayout(add_row)

        sl.addSpacing(12)
        sl.addWidget(self._navgroup("通用"))
        # Nav buttons (checkable for active-state styling).
        self._nav_btns: list[QPushButton] = []
        for label, page_idx in [("外观", 1), ("关于", 2)]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; text-align: left;"
                f" padding: 8px 12px; color: {self._c_text}; font-size: 12px; border-radius: 6px; }}"
                f"QPushButton:hover {{ background: {self._c_panel}; }}"
                f"QPushButton:checked {{ background: {self._c_panel};"
                f" color: {self._c_accent}; font-weight: 600; }}"
            )
            btn.clicked.connect(lambda _=False, x=page_idx: self._show_page(x))
            self._nav_btns.append(btn)
            sl.addWidget(btn)
        sl.addStretch()
        body.addWidget(side)

        # ---- Right pane: stack + shared footer ----
        self._right_area = right_area = QWidget()
        right_area.setObjectName("rightArea")
        ra = QVBoxLayout(right_area)
        ra.setContentsMargins(0, 0, 0, 0)
        ra.setSpacing(0)

        self.stack = QStackedWidget()
        # Page 0: provider editor.
        prov_page = QWidget()
        self._build_provider_page(prov_page)
        self.stack.addWidget(prov_page)
        # Page 1: appearance.
        app_page = QWidget()
        self._build_appearance_page(app_page)
        self.stack.addWidget(app_page)
        # Page 2: about.
        about_page = QWidget()
        self._build_about_page(about_page)
        self.stack.addWidget(about_page)
        ra.addWidget(self.stack, 1)

        # Shared footer (always visible regardless of page).
        # Three buttons: 取消 (close, discard changes), 应用 (save and stay
        # open for further tweaking), 保存 (save and close the dialog).
        self._footer_widget = footer_widget = QWidget()
        footer_widget.setObjectName("footer")
        fw = QHBoxLayout(footer_widget)
        fw.setContentsMargins(28, 12, 28, 12)
        fw.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        fw.addWidget(cancel_btn)
        self.apply_btn = apply_btn = QPushButton("应用")
        apply_btn.clicked.connect(self.apply)
        fw.addWidget(apply_btn)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.accept)
        fw.addWidget(save_btn)
        ra.addWidget(footer_widget)

        body.addWidget(right_area, 1)
        root.addLayout(body, 1)
        self._reload_list()

    def _show_page(self, idx: int):
        """Switch the stacked page and update nav button checked states."""
        self._commit_editor()
        self._editing_index = None
        self.list_widget.clearSelection()
        for i, btn in enumerate(self._nav_btns):
            btn.setChecked(i + 1 == idx)
        self.stack.setCurrentIndex(idx)

    def _build_provider_page(self, page):
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 20, 28, 20)
        v.setSpacing(14)
        page.setStyleSheet(f"background: {self._c_bg};")
        self.prov_title = QLabel("")
        self.prov_title.setObjectName("pageTitle")
        v.addWidget(self.prov_title)
        self.prov_sub = QLabel("")
        self.prov_sub.setObjectName("pageSub")
        v.addWidget(self.prov_sub)

        # Basic fields (all providers). ID is auto-generated and not editable.
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("如 火山方舟-coding plan")
        # Wrap the 基本信息 card in a frame so the entire right pane (including
        # the label field) can be hidden when no provider is selected.
        # Two-step flow: 选类型+填名称 -> 添加 -> 点列表项 -> 填凭证 -> 保存.
        self.info_frame = QFrame()
        info_layout = QVBoxLayout(self.info_frame)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        info_layout.addWidget(self._card("基本信息", [
            ("显示名", self.label_edit),
        ]))
        v.addWidget(self.info_frame)

        # Volcengine-specific fields (cURL/cookie/csrf/x_web_id) - hidden for other types.
        self.volc_frame = QFrame()
        volc_layout = QVBoxLayout(self.volc_frame)
        volc_layout.setContentsMargins(0, 0, 0, 0)
        volc_layout.setSpacing(10)
        self.cred_cookie = QTextEdit()
        self.cred_cookie.setAcceptRichText(False)
        self.cred_cookie.setPlaceholderText("粘整段 cURL 或 cookie 字符串...")
        self.cred_cookie.setFixedHeight(180)
        volc_layout.addWidget(self._card("火山方舟凭证", [
            ("cURL / Cookie", self.cred_cookie),
        ]))
        self.parse_btn = QPushButton("从上方解析 csrf_token / x_web_id")
        self.parse_btn.clicked.connect(self._parse_curl)
        volc_layout.addWidget(self.parse_btn)
        self.csrf_edit = QLineEdit()
        self.xwebid_edit = QLineEdit()
        volc_layout.addWidget(self._card("Token", [
            ("csrf_token", self.csrf_edit),
            ("x_web_id", self.xwebid_edit),
        ]))
        v.addWidget(self.volc_frame)

        # API Key field (MiniMax / DeepSeek) - hidden for volcengine.
        self.key_frame = QFrame()
        key_layout = QVBoxLayout(self.key_frame)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(10)
        self.apikey_edit = QLineEdit()
        self.apikey_edit.setPlaceholderText("sk-cp-... / sk-...")
        self.apikey_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_layout.addWidget(self._card("API Key", [
            ("API Key", self.apikey_edit),
        ]))
        v.addWidget(self.key_frame)

        # Qoder: no credential fields - login happens in a browser window
        # (session cookie lives in the dedicated Chrome profile). Hidden for
        # other types.
        self.qoder_frame = QFrame()
        qoder_layout = QVBoxLayout(self.qoder_frame)
        qoder_layout.setContentsMargins(0, 0, 0, 0)
        qoder_layout.setSpacing(10)
        qoder_hint = QLabel(
            "无需粘贴凭证：点击下方按钮打开 Qoder 登录页，在浏览器中登录一次即可。\n"
            "登录态保存在本地专用浏览器配置中，约 8 天过期后重新登录。"
        )
        qoder_hint.setObjectName("pageSub")
        qoder_hint.setWordWrap(True)
        qoder_layout.addWidget(qoder_hint)
        self.qoder_status_label = QLabel()
        self.qoder_status_label.setObjectName("qoderStatus")
        self.qoder_status_label.setWordWrap(True)
        self._set_qoder_status("no_login")
        qoder_layout.addWidget(self.qoder_status_label)
        self.qoder_login_btn = QPushButton("打开 Qoder 登录窗口")
        self.qoder_login_btn.clicked.connect(self._open_qoder_login)
        qoder_layout.addWidget(self.qoder_login_btn)
        v.addWidget(self.qoder_frame)

        # Polling state -- set when "打开 Qoder 登录窗口" is clicked; cleared
        # once a probe confirms login success or the 90 s deadline elapses.
        self._qoder_poll = QTimer(self)
        self._qoder_poll.setInterval(1000)
        self._qoder_poll.timeout.connect(self._poll_qoder_login)
        self._qoder_probe_attempts = 0
        self._qoder_probes: list = []  # keep refs so QThreads aren't GC'd

        v.addStretch()
        # Initial state: hide the entire right pane (info + credential frames).
        # Only shown after the user clicks a provider on the left.
        self.info_frame.hide()
        self.volc_frame.hide()
        self.key_frame.hide()
        self.qoder_frame.hide()

    def _build_appearance_page(self, page):
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 20, 28, 20)
        v.setSpacing(14)
        page.setStyleSheet(f"background: {self._c_bg};")
        title = QLabel("外观")
        title.setObjectName("pageTitle")
        v.addWidget(title)
        sub = QLabel("配置主题与显示偏好")
        sub.setObjectName("pageSub")
        v.addWidget(sub)

        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)
        lbl = QLabel("主题")
        lbl.setObjectName("fieldLabel")
        lbl.setMinimumWidth(100)
        theme_row.addWidget(lbl)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_row.addWidget(self.theme_combo, 1)
        v.addWidget(self._card_layout("主题", theme_row))

        prim_row = QHBoxLayout()
        prim_row.setSpacing(12)
        lbl2 = QLabel("主平台")
        lbl2.setObjectName("fieldLabel")
        lbl2.setMinimumWidth(100)
        prim_row.addWidget(lbl2)
        self.primary_combo = QComboBox()
        prim_row.addWidget(self.primary_combo, 1)
        v.addWidget(self._card_layout("主 Provider", prim_row))

        poll_row = QHBoxLayout()
        poll_row.setSpacing(12)
        lbl3 = QLabel("轮询间隔")
        lbl3.setObjectName("fieldLabel")
        lbl3.setMinimumWidth(100)
        poll_row.addWidget(lbl3)
        self.interval_combo = QComboBox()
        for sec, label in [(10, "10 秒"), (30, "30 秒"), (60, "1 分钟"),
                           (180, "3 分钟"), (300, "5 分钟"), (600, "10 分钟"),
                           (1800, "30 分钟"), (3600, "1 小时")]:
            self.interval_combo.addItem(label, sec)
        poll_row.addWidget(self.interval_combo, 1)
        v.addWidget(self._card_layout("轮询间隔", poll_row))

        th_row = QHBoxLayout()
        th_row.setSpacing(12)
        lbl4 = QLabel("预警 / 告警阈值")
        lbl4.setObjectName("fieldLabel")
        lbl4.setMinimumWidth(100)
        th_row.addWidget(lbl4)
        self.warn_combo = QComboBox()
        for val in [50, 60, 70, 80, 85, 90]:
            self.warn_combo.addItem(f"{val} %", val)
        self.crit_combo = QComboBox()
        for val in [80, 85, 90, 95, 98, 100]:
            self.crit_combo.addItem(f"{val} %", val)
        th_row.addWidget(self.warn_combo)
        th_row.addWidget(self.crit_combo)
        th_row.addStretch()
        v.addWidget(self._card_layout("阈值", th_row))
        v.addStretch()

    def _build_about_page(self, page):
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 20, 28, 20)
        v.setSpacing(14)
        page.setStyleSheet(f"background: {self._c_bg};")
        title = QLabel("关于")
        title.setObjectName("pageTitle")
        v.addWidget(title)

        info_lines = [
            ("版本", "0.1.1"),
        ]
        for label, value in info_lines:
            row = QHBoxLayout()
            l = self._fieldlabel(label)
            row.addWidget(l)
            row.addStretch()
            val = QLabel(value)
            val.setObjectName("aboutVal")
            row.addWidget(val)
            v.addLayout(row)
        v.addStretch()

    def _navgroup(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("navGroup")
        return lbl

    def _fieldlabel(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("fieldLabel")
        return lbl

    def _card(self, header, fields):
        """A rounded card with an optional header and horizontal label/widget rows."""
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(10)
        if header:
            h = QLabel(header)
            h.setObjectName("cardHeader")
            v.addWidget(h)
        for label, widget in fields:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            lbl = QLabel(label)
            lbl.setObjectName("fieldLabel")
            lbl.setMinimumWidth(100)
            row.addWidget(lbl)
            row.addWidget(widget, 1)
            v.addLayout(row)
        return card

    def _card_layout(self, header, hlayout):
        """A card wrapping a single horizontal row (label + control)."""
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        if header:
            h = QLabel(header)
            h.setObjectName("cardHeader")
            v.addWidget(h)
        v.addLayout(hlayout)
        return card
        v.addLayout(hlayout)
        return card

    def _reload_list(self):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        _arrow_dir = _resource_dir() / "icon"
        _del_icon_path = str((_arrow_dir / "删除.png").resolve()).replace("\\", "/")
        _del_pix = QPixmap(_del_icon_path)
        _del_icon_size = QSize(14, 14)
        for p in _app_config.get("providers", []):
            # Custom row widget: label + delete icon button.
            row_widget = QWidget()
            row_widget.setFixedHeight(40)
            rh = QHBoxLayout(row_widget)
            rh.setContentsMargins(12, 4, 8, 4)
            rh.setSpacing(6)
            lbl = QLabel(_auto_labels().get(p["id"], p.get("label", "")))
            lbl.setObjectName("navItem")
            lbl.setStyleSheet("background: transparent;")
            rh.addWidget(lbl)
            rh.addStretch()
            del_btn = QPushButton()
            del_btn.setFixedSize(20, 20)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(
                "QPushButton { border: none; background: transparent; }"
                "QPushButton:hover { background: rgba(231,76,60,0.15); border-radius: 4px; }"
            )
            del_btn.setIcon(QIcon(_del_pix))
            del_btn.setIconSize(_del_icon_size)
            pid = p["id"]
            del_btn.clicked.connect(lambda _=False, x=pid: self._del_provider_by_id(x))
            rh.addWidget(del_btn)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            item.setSizeHint(QSize(0, 40))
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, row_widget)
        self.list_widget.blockSignals(False)
        self.primary_combo.clear()
        for p in _app_config.get("providers", []):
            self.primary_combo.addItem(
                _auto_labels().get(p["id"], p.get("label", "")), p["id"]
            )
        cur = _app_config.get("primary", "")
        idx = self.primary_combo.findData(cur)
        if idx >= 0:
            self.primary_combo.setCurrentIndex(idx)
        self.theme_combo.blockSignals(True)
        tidx = self.theme_combo.findData(_app_config.get("theme", "dark"))
        if tidx >= 0:
            self.theme_combo.setCurrentIndex(tidx)
        self.theme_combo.blockSignals(False)
        iidx = self.interval_combo.findData(_app_config.get("poll_interval_sec", 10))
        if iidx >= 0:
            self.interval_combo.setCurrentIndex(iidx)
        widx = self.warn_combo.findData(int(_app_config.get("warning_percent", 80)))
        if widx >= 0:
            self.warn_combo.setCurrentIndex(widx)
        cidx = self.crit_combo.findData(int(_app_config.get("critical_percent", 95)))
        if cidx >= 0:
            self.crit_combo.setCurrentIndex(cidx)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def _on_select_row(self, row):
        if row < 0 or row >= len(_app_config["providers"]):
            return
        # Commit current editor before switching to avoid losing changes.
        self._commit_editor()
        # Clear nav button checked states since a provider is now selected.
        for btn in self._nav_btns:
            btn.setChecked(False)
        self.stack.setCurrentIndex(0)
        self._editing_index = row
        p = _app_config["providers"][row]
        self._load_editor(p)

    def _load_editor(self, p: dict):
        is_primary = p["id"] == _app_config.get("primary")
        ptype = p.get("type", "")
        # Auto-fill the label if empty so the user always sees a sensible
        # name (and can edit it). The underlying entry stays label="" until
        # commit so we don't persist auto-names the user immediately overrides.
        label = p.get("label", "") or _auto_labels().get(p["id"], _default_label_for(ptype))
        self.info_frame.show()
        self.prov_title.setText(label)
        self.prov_sub.setText(f"{ptype}  {'· 主平台' if is_primary else ''}")
        self.label_edit.setText(label)
        c = p.get("credentials", {})
        # Show only relevant credential fields based on provider type.
        if ptype == "volcengine":
            self.volc_frame.show()
            self.key_frame.hide()
            self.qoder_frame.hide()
            # Prefer the original cURL when present -- that's the only form
            # from which x_web_id can be re-extracted (the bare cookie
            # doesn't carry it). Fall back to the cleaned cookie for legacy
            # configs that pre-date this field.
            self.cred_cookie.setPlainText(
                c.get("original_curl") or c.get("cookie", "")
            )
            self.csrf_edit.setText(c.get("csrf_token", ""))
            self.xwebid_edit.setText(c.get("x_web_id", ""))
            self.apikey_edit.setText("")
        elif ptype == "qoder":
            self.volc_frame.hide()
            self.key_frame.hide()
            self.qoder_frame.show()
            self._qoder_probe_attempts = 0
            self._qoder_probe_async(int(c.get("cdp_port", 9333)))
        else:
            self.volc_frame.hide()
            self.key_frame.show()
            self.qoder_frame.hide()
            self.apikey_edit.setText(c.get("api_key", ""))
            self.cred_cookie.setPlainText("")
            self.csrf_edit.setText("")
            self.xwebid_edit.setText("")

    def _open_qoder_login(self):
        """Open a visible browser window for (re)login to qoder.com."""
        from providers.qoder import open_login_window

        try:
            open_login_window()
        except Exception as e:
            QMessageBox.warning(self, "提示", f"打开登录窗口失败: {e}")
            return
        self._set_qoder_status("waiting")
        self._qoder_probe_attempts = 0
        self._qoder_probes.clear()
        self._qoder_poll.start()

    def _set_qoder_status(self, state: str) -> None:
        """Update the status line. state in: no_login / waiting / logged_in / timeout."""
        states = {
            "no_login": ("⚠ 暂无登录信息,请点击下方按钮打开 Qoder 登录窗口", "#f39c12"),
            "waiting": ("⏳ 已打开登录窗口,请在其中完成登录(检测中…)", "#3b8bff"),
            "logged_in": ("✓ 登录成功,数据将在下次轮询时刷新", "#2ecc71"),
            "timeout": ("⚠ 未检测到登录完成,请重试", "#e74c3c"),
        }
        text, color = states[state]
        self.qoder_status_label.setText(text)
        self.qoder_status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600; background: transparent;"
        )

    def _qoder_probe_async(self, port: int) -> None:
        """Run a single non-blocking cookie check to determine login state.

        Uses Network.getCookies (not fetch()) so we don't open a new tab or
        navigate to qoder.com -- both of those pop the user's login window
        to the foreground on every poll tick.
        """
        probe = _QoderCookieProbe(port, self)
        probe.ok.connect(self._on_qoder_probe_ok)
        probe.fail.connect(self._on_qoder_probe_fail)
        probe.finished.connect(probe.deleteLater)
        self._qoder_probes.append(probe)
        probe.start()

    def _poll_qoder_login(self) -> None:
        """Timer tick: launch a probe, stop polling on success or timeout."""
        self._qoder_probe_attempts += 1
        if self._qoder_probe_attempts > 90:  # 90 × 1s = 90s
            self._qoder_poll.stop()
            self._set_qoder_status("timeout")
            return
        port = self._current_qoder_port()
        if port is None:
            return
        self._qoder_probe_async(port)

    def _on_qoder_probe_ok(self) -> None:
        self._qoder_poll.stop()
        self._set_qoder_status("logged_in")
        # Drop finished probes so they can be GC'd.
        self._qoder_probes = [p for p in self._qoder_probes if p.isRunning()]
        # Nudge MainWindow to refresh the ball right now -- otherwise it
        # would wait up to poll_interval_sec (10 s) before showing data.
        self.applied.emit()

    def _on_qoder_probe_fail(self, msg: str) -> None:
        # Keep status at "waiting" -- the user might still be logging in.
        # Only the 90 s timeout changes the status.
        self._qoder_probes = [p for p in self._qoder_probes if p.isRunning()]

    def _current_qoder_port(self) -> int | None:
        """Return the cdp_port of the currently edited qoder provider, if any."""
        for p in _app_config.get("providers", []):
            if p.get("type") == "qoder":
                return int(p.get("credentials", {}).get("cdp_port", 9333))
        return None

    def _parse_curl(self):
        parsed = _parse_curl_or_cookie(self.cred_cookie.toPlainText())
        if not parsed:
            return
        # NOTE: we intentionally do NOT replace the cookie textbox with the
        # parsed cookie string. Keeping the original cURL in the textbox
        # means the user (and _validate_current_editor / _commit_editor)
        # can re-parse it any number of times -- e.g. after the user
        # clears the x_web_id field and clicks 解析 again to recover it.
        # The cleaned cookie is only extracted at save time by
        # _commit_editor, so what gets persisted is still the bare cookie,
        # never the raw cURL command.
        if parsed.get("csrf_token"):
            self.csrf_edit.setText(parsed["csrf_token"])
        if parsed.get("x_web_id"):
            self.xwebid_edit.setText(parsed["x_web_id"])

    def _add_provider(self):
        ptype = self.type_combo.currentData()
        import time as _t
        new_id = f"{ptype}_{int(_t.time() * 1000) % 100000}"
        entry = {"id": new_id, "type": ptype, "label": "", "credentials": {}}
        _app_config["providers"].append(entry)
        self._reload_list()
        # _reload_list ends with setCurrentRow(0), which fires
        # _on_select_row -> _load_editor and opens the (first) provider's
        # form. For multi-provider lists, that's NOT the new one — the
        # user just added an entry and expects to see ITS form. Force-select
        # the new (last) row. setCurrentRow is a no-op when row matches
        # already-current, so the first-provider case (currentRow=0 after
        # _reload_list, new_index=0) doesn't re-fire, but _reload_list
        # already triggered the load during its own setCurrentRow(0).
        # _load_editor auto-fills the empty label with a default name
        # (see _default_label_for).
        new_index = len(_app_config["providers"]) - 1
        if self.list_widget.currentRow() != new_index:
            self.list_widget.setCurrentRow(new_index)
        # Belt-and-suspenders: if the row didn't actually change (e.g.
        # transient signal suppression), call _load_editor directly so the
        # new provider's form is always visible after add.
        if self._editing_index != new_index:
            self._editing_index = new_index
            self._load_editor(_app_config["providers"][new_index])

    def _del_provider_by_id(self, pid: str):
        if pid == _app_config.get("primary"):
            QMessageBox.warning(self, "提示", "不能删除当前主平台")
            return
        # Confirm before destroying a non-primary provider's config.
        label = next((p.get("label", pid) for p in _app_config["providers"]
                      if p["id"] == pid), pid)
        ans = QMessageBox.question(
            self, "删除 Provider",
            f"确定删除「{label}」？该 Provider 的凭证会从配置中移除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        # Find and remove the provider entry.
        for i, p in enumerate(_app_config["providers"]):
            if p["id"] == pid:
                _app_config["providers"].pop(i)
                if self._editing_index == i:
                    self._editing_index = None
                elif self._editing_index is not None and self._editing_index > i:
                    self._editing_index -= 1
                break
        self._reload_list()

    def _commit_editor(self):
        if self._editing_index is None:
            return
        p = _app_config["providers"][self._editing_index]
        # Fall back to a default name when the user clears the field,
        # so we never persist an empty label.
        p["label"] = self.label_edit.text().strip() or _auto_labels().get(p["id"], _default_label_for(p.get("type", "")))
        # Only save credentials relevant to this provider type.
        ptype = p.get("type", "")
        if ptype == "volcengine":
            cookie_text = self.cred_cookie.toPlainText().strip()
            # Defense-in-depth: if the cookie textbox still holds a raw cURL
            # command (user pasted a cURL but didn't click the 解析 button),
            # parse it here so we never persist the whole cURL as the Cookie
            # header value. The HTTP layer rejects a multi-line cURL command
            # in the Cookie header with InvalidHeader / 401, which surfaces
            # as a red error on the floating ball. We parse whenever the
            # textbox looks like a cURL -- this also catches the "fields are
            # stale from an old provider, user pasted a new cURL" case that
            # pure field-only validation would miss.
            looks_curl = (
                "curl " in cookie_text
                or "-H " in cookie_text
                or "--header" in cookie_text
                or "-b " in cookie_text
                or "--cookie" in cookie_text
            )
            original_curl = ""
            if looks_curl:
                # Snapshot the raw cURL textbox content BEFORE we overwrite
                # cookie_text with the parsed bare cookie below. This is the
                # only form from which x_web_id can be re-extracted on
                # reopen, so we must keep it around verbatim.
                original_curl = cookie_text
                parsed = _parse_curl_or_cookie(cookie_text)
                if parsed.get("cookie"):
                    cookie_text = parsed["cookie"]
                if parsed.get("csrf_token"):
                    self.csrf_edit.setText(parsed["csrf_token"])
                if parsed.get("x_web_id"):
                    self.xwebid_edit.setText(parsed["x_web_id"])
            p["credentials"] = {
                "cookie": cookie_text,
                "csrf_token": self.csrf_edit.text().strip(),
                "x_web_id": self.xwebid_edit.text().strip(),
                "original_curl": original_curl,
            }
        elif ptype == "qoder":
            # No user-entered credentials: login state lives in the dedicated
            # Chrome profile. Keep cdp_port if the user set one.
            p["credentials"] = {
                k: v for k, v in p.get("credentials", {}).items()
                if k == "cdp_port"
            }
        else:
            p["credentials"] = {
                "api_key": self.apikey_edit.text().strip(),
            }

    def apply(self) -> bool:
        """Persist current settings and apply them to the live app.

        Returns True when saved successfully, False when validation
        failed. Both `apply()` and `accept()` share the same save logic
        so the two buttons stay in sync.
        """
        # Per-provider validation runs BEFORE commit. For volcengine it
        # also re-parses the cookie text and auto-fills the csrf_token /
        # x_web_id fields, so the values written by _commit_editor are
        # coherent (cookie + token pair, not stale empty fields).
        if not self._validate_current_editor():
            return False
        self._commit_editor()
        _app_config["primary"] = self.primary_combo.currentData() or ""
        _app_config["theme"] = self.theme_combo.currentData() or "dark"
        _app_config["poll_interval_sec"] = self.interval_combo.currentData() or 10
        _app_config["warning_percent"] = self.warn_combo.currentData() or 80
        _app_config["critical_percent"] = self.crit_combo.currentData() or 95
        if not _app_config.get("providers"):
            QMessageBox.warning(self, "提示", "至少需要配置一个平台")
            return False
        if not _app_config.get("primary"):
            _app_config["primary"] = _app_config["providers"][0]["id"]
        self._original_theme = _app_config["theme"]
        self.applied.emit()
        # Flash whichever button the user actually clicked: apply_btn gets
        # "已应用 ✓", save_btn (and any other trigger) gets "已保存 ✓".
        # When apply() is called programmatically (e.g. from tests) sender()
        # is None, so skip the flash.
        btn = self.sender()
        if btn is self.apply_btn:
            self._flash_button(btn, "已应用 ✓")
        elif btn is not None:
            self._flash_button(btn, "已保存 ✓")
        return True

    def _validate_current_editor(self) -> bool:
        """Check the currently-edited provider's required credentials.

        For volcengine there are two shapes of input in the cookie textbox:
          1. A fresh cURL blob (has `curl `/`-H `/`-b ` markers) -> auto-parse
             it, fill csrf_token / x_web_id, and clean the cookie textbox to
             the bare cookie string. This means the user does NOT have to
             click the "解析" button before 应用/保存.
          2. A plain cookie string (already cleaned by a previous apply, or
             loaded from config) -> the x_web_id HTTP header value is NOT
             recoverable from a bare cookie, so we just trust the cached
             csrf_token / x_web_id fields and check they are non-empty.

        This split is what makes "应用 then 保存" work: after 应用 cleans the
        textbox, the second 保存 takes path 2 and trusts the fields instead
        of failing to re-extract x_web_id.
        """
        if self._editing_index is None:
            return True
        p = _app_config["providers"][self._editing_index]
        ptype = p.get("type", "")
        if ptype == "volcengine":
            cookie_text = self.cred_cookie.toPlainText().strip()
            looks_curl = (
                "curl " in cookie_text
                or "-H " in cookie_text
                or "--header" in cookie_text
                or "-b " in cookie_text
                or "--cookie" in cookie_text
            )
            if looks_curl:
                # Fresh cURL pasted: parse it, fill fields. We do NOT clean
                # the cookie textbox here -- keeping the original cURL lets
                # the user re-click 解析 (or re-apply) to recover a field
                # they accidentally cleared. The bare cookie is only
                # extracted at commit time by _commit_editor.
                parsed = _parse_curl_or_cookie(cookie_text)
                if not parsed.get("csrf_token") or not parsed.get("x_web_id"):
                    QMessageBox.warning(self, "提示", "cURL/Cookie解析失败")
                    return False
                self.csrf_edit.setText(parsed["csrf_token"])
                self.xwebid_edit.setText(parsed["x_web_id"])
                return True
            # Plain cookie: trust the cached token fields.
            if (not self.csrf_edit.text().strip()
                    or not self.xwebid_edit.text().strip()):
                QMessageBox.warning(
                    self, "提示",
                    "csrf_token / x_web_id 不能为空，请粘贴 cURL 后点击"
                    "\"从上方解析\"按钮",
                )
                return False
            return True
        # MiniMax / DeepSeek: api_key is the only required credential.
        if ptype not in ("volcengine", "qoder"):
            if not self.apikey_edit.text().strip():
                QMessageBox.warning(self, "提示", "API Key 不能为空")
                return False
        # Qoder: no required fields (browser-login based).
        return True

    def accept(self):
        """Save settings AND close the dialog."""
        if self.apply():
            super().accept()

    def reject(self):
        # Revert theme on cancel/close if user changed it during preview.
        if _app_config.get("theme") != self._original_theme:
            _app_config["theme"] = self._original_theme
            self.theme_changed.emit()
        super().reject()

    def _flash_button(self, btn: QPushButton, success_text: str):
        """Briefly turn `btn` green with `success_text` to confirm the apply.

        Works for both the primary (保存) button and the secondary (应用)
        button: captures its current text + whether it was the primary
        styled one, then restores the right theme-aware style after 1.2s.
        """
        if btn is None:
            return
        orig_text = btn.text()
        orig_was_primary = btn.objectName() == "primary"
        btn.setText(success_text)
        btn.setStyleSheet(
            "QPushButton { background-color: #2ecc71; border: 1px solid #2ecc71;"
            " color: #ffffff; font-weight: 600; border-radius: 8px;"
            " padding: 8px 24px; min-width: 60px; font-size: 12px; }"
        )

        def restore():
            if btn is None or not btn.isVisible():
                return  # dialog closed before the timer fired
            btn.setText(orig_text)
            if orig_was_primary:
                btn.setStyleSheet(
                    f"QPushButton#primary {{ background-color: {self._c_accent};"
                    f" border: 1px solid {self._c_accent}; color: #ffffff;"
                    f" font-weight: 600; border-radius: 8px; padding: 8px 24px; }}"
                )
            else:
                btn.setStyleSheet("")  # revert to global QPushButton QSS

        QTimer.singleShot(1200, restore)

    def _on_theme_changed(self):
        """Live-preview: apply theme immediately to dialog + ball + card."""
        _app_config["theme"] = self.theme_combo.currentData() or "dark"
        self._apply_theme_style()
        self._refresh_dialog_styles()
        self.theme_changed.emit()

    def _refresh_dialog_styles(self):
        """Re-apply inline styles that QSS can't cascade to."""
        # Page backgrounds.
        for i in range(self.stack.count()):
            self.stack.widget(i).setStyleSheet(f"background: {self._c_bg};")
        # Nav buttons.
        for btn in self._nav_btns:
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; text-align: left;"
                f" padding: 8px 12px; color: {self._c_text}; font-size: 12px; border-radius: 6px; }}"
                f"QPushButton:hover {{ background: {self._c_panel}; }}"
                f"QPushButton:checked {{ background: {self._c_panel};"
                f" color: {self._c_accent}; font-weight: 600; }}"
            )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class MonitorApp:
    def __init__(self):
        global _app_config
        _app_config = load_config()
        save_config(_app_config)  # persist migrated form
        self.app = QApplication(sys.argv)
        self.app.setWindowIcon(QIcon(str(_resource_dir() / "image" / "cpw_icon.ico")))
        self.app.setQuitOnLastWindowClosed(False)

        self.providers: dict[str, ProviderBase] = {}
        self._rebuild_providers()

        self.snapshots: dict[str, ProviderSnapshot] = {}
        self.ball = FloatBall()
        self.ball.warn = _app_config.get("warning_percent", 80)
        self.ball.crit = _app_config.get("critical_percent", 95)
        self.ball._build_menu = self._build_ball_menu
        self.card = DetailCard()
        self.card.hide()

        self._ball_hover = False
        self._card_hover = False
        self._hide_timer = QTimer(self.ball)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(120)
        self._hide_timer.timeout.connect(self._maybe_hide_card)

        self.ball.hovered.connect(self._on_ball_hover)
        self.card.hovered.connect(self._on_card_hover)
        self.ball.level_cycled.connect(self._on_level_cycled)
        self.ball.drag_started.connect(self._on_drag_started)

        self.tray = QSystemTrayIcon(self._make_icon())
        self.tray.setToolTip("Coding Plan 监控")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self._update_tray_menu()

        self.timer = QTimer(self.ball)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(_app_config.get("poll_interval_sec", 10) * 1000)

        self.ball.show()
        screen = self.app.primaryScreen().geometry()
        self.ball.move(screen.width() - BALL_SIZE - 30, screen.height() - BALL_SIZE - 80)
        self.refresh()

    def _rebuild_providers(self):
        self.providers = {}
        for entry in _app_config.get("providers", []):
            try:
                self.providers[entry["id"]] = build_provider(entry)
            except Exception as e:
                print(f"[provider] failed to build {entry.get('id')}: {e}", file=sys.stderr)

    def _make_icon(self) -> QIcon:
        pix = QPixmap(16, 16)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor("#2ecc71")))
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.drawEllipse(2, 2, 12, 12)
        p.end()
        return QIcon(pix)

    def _build_ball_menu(self) -> QMenu:
        menu = QMenu(self.ball)
        # Switch primary provider submenu.
        sw = menu.addMenu("切换主 Provider")
        for pid, p in self.providers.items():
            label = p.label
            act = sw.addAction(f"{label}{'  [当前]' if pid == _app_config.get('primary') else ''}")
            act.triggered.connect(lambda _=False, x=pid: self._switch_primary(x))
        menu.addSeparator()
        act_settings = menu.addAction("设置...")
        act_settings.triggered.connect(self.open_settings)
        act_refresh = menu.addAction("立即刷新")
        act_refresh.triggered.connect(self.refresh)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self.app.quit)
        return menu

    def _update_tray_menu(self):
        """Rebuild the tray context menu (provider list may change)."""
        self.tray.setContextMenu(self._build_ball_menu())

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    # hover
    def _on_ball_hover(self, entered: bool):
        self._ball_hover = entered
        if entered:
            self._hide_timer.stop()
            # Don't pop the card before any provider is configured or has
            # fetched successfully (fresh install / still initializing).
            if not self.ball.has_valid_snapshot():
                return
            self._show_card()
        else:
            self._hide_timer.start()

    def _on_drag_started(self):
        # Hide card immediately and suppress hover bursts during drag.
        self._hide_timer.stop()
        self._ball_hover = False
        self.card.hide()

    def _on_card_hover(self, entered: bool):
        self._card_hover = entered
        if entered:
            self._hide_timer.stop()
        else:
            self._hide_timer.start()

    def _maybe_hide_card(self):
        if not self._ball_hover and not self._card_hover:
            self.card.hide()

    def _show_card(self):
        bp = self.ball.pos()
        screen = self.app.primaryScreen().geometry()
        x = bp.x() - CARD_WIDTH - 10
        if x < 0:
            x = bp.x() + BALL_SIZE + 10
        y = bp.y()
        if y + CARD_HEIGHT > screen.height():
            y = screen.height() - CARD_HEIGHT - 10
        self.card.move(x, y)
        self.card.show()
        self.card.raise_()

    def _switch_primary(self, pid: str):
        _app_config["primary"] = pid
        save_config(_app_config)
        self._update_tray_menu()
        self.refresh()

    def _on_level_cycled(self):
        primary = _app_config.get("primary")
        snap = self.snapshots.get(primary)
        if not snap or not snap.levels:
            return
        keys = [q.level for q in snap.levels]
        cur = self.ball.current_level
        if cur in keys:
            idx = keys.index(cur)
            self.ball.current_level = keys[(idx + 1) % len(keys)]
        else:
            self.ball.current_level = keys[0]
        self.ball.update()

    def open_settings(self):
        dlg = SettingsDialog(parent=None)
        # Center on screen (not relative to ball position).
        screen = self.app.primaryScreen().geometry()
        dlg.move(
            screen.center().x() - dlg.width() // 2,
            screen.center().y() - dlg.height() // 2,
        )
        dlg.theme_changed.connect(self._on_live_theme_change)
        # "保存" now applies (persist + take effect) without closing, so the
        # user can keep configuring. Closing the dialog (X / ESC / 取消)
        # ends exec().
        dlg.applied.connect(self._on_settings_applied)
        dlg.exec()
        # No final save here: every apply already persists via _on_settings_applied.

    def _on_settings_applied(self):
        save_config(_app_config)
        self._rebuild_providers()
        self.ball.warn = _app_config.get("warning_percent", 80)
        self.ball.crit = _app_config.get("critical_percent", 95)
        self.ball.refresh_theme()
        self.card.refresh_theme()
        self.timer.start(_app_config.get("poll_interval_sec", 10) * 1000)
        self._update_tray_menu()
        self.tray.showMessage("Coding Plan", "设置已保存，正在刷新...",
                              QSystemTrayIcon.MessageIcon.Information, 2000)
        self.refresh()

    def _on_live_theme_change(self):
        """Called when user changes theme in settings dialog (live preview)."""
        self.ball.refresh_theme()
        self.card.refresh_theme()

    def refresh(self):
        # Ensure ball/card use the latest theme before rendering.
        self.ball.refresh_theme()
        self.card.refresh_theme()
        primary = _app_config.get("primary")
        any_ok = False
        for pid, p in self.providers.items():
            try:
                snap = p.fetch()
                self.snapshots[pid] = snap
                any_ok = True
            except Exception as e:
                self.snapshots[pid] = ProviderSnapshot(
                    provider_id=pid, ok=False, error=str(e)
                )
        # Update ball with primary snapshot.
        primary_snap = self.snapshots.get(primary)
        if primary_snap and primary_snap.ok and primary_snap.levels:
            self.ball.set_error(False)
            if self.ball.current_level not in [q.level for q in primary_snap.levels]:
                self.ball.current_level = primary_snap.levels[0].level
            self.ball.update_snapshot(primary_snap, self.ball.current_level)
        else:
            self.ball.set_error(True)

        # Update detail card: only show primary provider.
        primary_snap = self.snapshots.get(primary)
        if primary_snap:
            self.card.render_snapshots(
                [primary_snap], primary,
                _app_config.get("warning_percent", 80),
                _app_config.get("critical_percent", 95),
                int(time.time()),
            )
        # Tray tooltip.
        if primary_snap and primary_snap.ok:
            q = primary_snap.get(self.ball.current_level) or primary_snap.levels[0]
            self.tray.setToolTip(f"Coding Plan · {_provider_label(primary)} · {q.label} {q.used_percent:.1f}%")
        else:
            err = primary_snap.error if primary_snap else "无主 Provider"
            self.tray.setToolTip(f"Coding Plan · 失败: {err}")

    def run(self):
        sys.exit(self.app.exec())


def main():
    MonitorApp().run()


if __name__ == "__main__":
    main()
