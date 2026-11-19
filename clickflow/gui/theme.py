"""Design tokens and the application stylesheet (light theme only, "Claude" design system).

The numbers come from the design hand-off in ``design/round2-result/clickflow-tokens.json``; each token maps 1:1 to a
QSS colour. Custom-painted widgets read colours through :func:`tok` at paint time.

There is no dark theme any more. :func:`mode` always answers ``"light"`` (kept so older callers keep working) and the
log panel uses the design system's *dark code surface*, which is a surface inside the light theme.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

# Albert Sans (assets/fonts, SIL Open Font License) is the UI font. It has no CJK glyphs, so Qt falls back per glyph to a
# system font for Chinese text (and for any symbol the font lacks).
#
# The family lists are kept as SHORT as possible on purpose: Qt resolves every listed family (even one that is not installed)
# the first time the font is used, which cost roughly 100-350 ms each at start-up. English needs only Albert Sans; Chinese
# adds the platform's own CJK UI font.
_CJK_FAMILY = {"win32": "Microsoft YaHei UI", "darwin": "PingFang SC"}.get(sys.platform, "Noto Sans CJK SC")
MONO_FAMILY = {"win32": "Consolas", "darwin": "Menlo"}.get(sys.platform, "monospace")


def ui_families() -> list[str]:
    """The UI font families for the current language, most preferred first."""
    from . import i18n

    return ["Albert Sans", _CJK_FAMILY] if i18n.language() == "zh" else ["Albert Sans"]


def _css_families(names: list[str]) -> str:
    return ", ".join(f'"{n}"' for n in names)

TOKENS: dict[str, dict[str, str]] = {
    "light": dict(
        bg="#faf9f5", pn="#faf9f5", cd="#faf9f5", fl="#faf9f5", inp="#faf9f5", tb="#efe9de",
        bd="#e6dfd8", bd2="#8e8b82", hv="#efe9de", pr="#e8e0d2",
        tx="#141413", tx2="#3d3d3a", tx3="#6c6a64", txi="#faf9f5",
        ac="#cc785c", ac_h="#bb6a4f", ac_p="#a9583e", ac_tx="#a9583e", ac_soft="#efe9de", on_ac="#141413", fc="#cc785c",
        ok="#5db872", ok_bg="#efe9de", wn="#d4a017", wn_bg="#efe9de", er="#c64545", er_bg="#efe9de",
        inf="#3d3d3a", inf_bg="#f5f0e8", dg="#c64545", on_dg="#ffffff",
        sw_off="#8e8b82", sw_th="#faf9f5", sw_on="#141413",
        lg_i="#faf9f5", lg_w="#d4a017", lg_e="#e8837c", lg_c="#5db872", lg_m="#5db8a6", lg_l="#a09d96",
        tp="#141413", tpt="#faf9f5", thumb="#181715",
        on_ac_p="#ffffff", fc_ring="rgba(204,120,92,0.15)", lg_bg="#181715", lg_t="#a09d96",
    ),
}

# Native overlay windows (selection, match boxes, STOP badge) live on somebody else's desktop: fixed colours.
OVERLAY: dict[str, str] = dict(
    ov_dim="rgba(20,20,19,0.60)", ov_ink="#141413", ov_target="#faf9f5", ov_area="#5db8a6", ov_found="#5db872",
    ov_cand="#d4a017", ov_stop="#c64545", ov_bar="#181715", ov_bar_text="#faf9f5", ov_step="#cc785c",
)

_mode = "light"
_listeners: list[Callable[[], None]] = []


def mode() -> str:
    """The theme in force: always ``"light"``."""
    return _mode


def parse_color(value: str) -> QColor:
    """``#rrggbb`` or ``rgba(r,g,b,a)`` (alpha 0..1) -> QColor."""
    v = value.strip()
    if v.startswith("rgba("):
        r, g, b, a = (s.strip() for s in v[5:-1].split(","))
        return QColor(int(float(r)), int(float(g)), int(float(b)), int(round(float(a) * 255)))
    return QColor(v)


def tok(name: str) -> str:
    return TOKENS[_mode][name]


def qcolor(name: str) -> QColor:
    return parse_color(tok(name))


def overlay(name: str) -> QColor:
    return parse_color(OVERLAY[name])


def rgba(hex_color: str, alpha: float) -> str:
    c = parse_color(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{int(round(alpha * 255))})"


def on_change(fn: Callable[[], None]) -> None:
    _listeners.append(fn)


def semibold() -> "QFont.Weight":
    """Weight for emphasised text: Medium (500), the heaviest the interface uses - nothing is bold. YaHei UI only has Regular
    and Bold, so Chinese text stays Regular."""
    from . import i18n

    return QFont.Weight.Normal if i18n.language() == "zh" else QFont.Weight.Medium


def semi_css() -> str:
    """``semibold()`` as a CSS font-weight value."""
    from . import i18n

    return "400" if i18n.language() == "zh" else "500"


_semi_css = semi_css  # (old name)


# ---------------------------------------------------------------- fonts

_FONT_IDS: list[int] = []
_LOADED_FAMILIES: list[str] | None = None


def load_fonts() -> list[str]:
    """Register the bundled font files (``assets/fonts/*.ttf|otf``), e.g. Albert Sans. Returns the families found.

    Nothing is required to be there: without the files Qt uses its default font. The files are registered once per
    process (registering them again on every restyle made each language switch slower).
    """
    global _LOADED_FAMILIES
    if _LOADED_FAMILIES is not None:
        return list(_LOADED_FAMILIES)
    try:
        from ..app import resource_root

        folder = resource_root() / "assets" / "fonts"
    except Exception:
        return []
    families: list[str] = []
    if folder.is_dir():
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() in (".ttf", ".otf"):
                fid = QFontDatabase.addApplicationFont(str(f))
                if fid >= 0:
                    _FONT_IDS.append(fid)
                    families.extend(QFontDatabase.applicationFontFamilies(fid))
    _LOADED_FAMILIES = families
    return list(families)


# ---------------------------------------------------------------- tiny image assets referenced from QSS

def _asset_dir() -> Path:
    try:
        d = Path(tempfile.gettempdir()) / "clickflow-ui"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return Path(tempfile.gettempdir())


def _arrow(path: Path, color: str, up: bool, w: int = 10, h: int = 6) -> None:
    px = QPixmap(w * 2, h * 2)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(parse_color(color))
    pts = [QPointF(1, h * 2 - 1), QPointF(w, 1), QPointF(w * 2 - 1, h * 2 - 1)] if up else \
          [QPointF(1, 1), QPointF(w, h * 2 - 1), QPointF(w * 2 - 1, 1)]
    p.drawPolygon(QPolygonF(pts))
    p.end()
    px.save(str(path))


def _chevron(path: Path, color: str) -> None:
    px = QPixmap(28, 28)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(parse_color(color), 3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(7, 10), QPointF(14, 17), QPointF(21, 10)]))
    p.end()
    px.save(str(path))


def _check(path: Path, color: str) -> None:
    px = QPixmap(36, 36)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(parse_color(color), 4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(9, 18.5), QPointF(15, 24.5), QPointF(27, 11)]))
    p.end()
    px.save(str(path))


_ASSET_CACHE: dict[str, dict[str, str]] = {}


def _assets(t: dict[str, str], name: str) -> dict[str, str]:
    hit = _ASSET_CACHE.get(name)
    if hit is not None and all(Path(v).exists() for v in hit.values()):  # (the temp folder may have been cleaned)
        return hit
    d = _asset_dir()
    files = {"up": d / f"{name}-up.png", "down": d / f"{name}-down.png", "chev": d / f"{name}-chev.png",
             "check": d / f"{name}-check.png"}
    _arrow(files["up"], t["tx3"], True)
    _arrow(files["down"], t["tx3"], False)
    _chevron(files["chev"], t["tx3"])
    _check(files["check"], t["txi"])
    _ASSET_CACHE[name] = {k: v.as_posix() for k, v in files.items()}
    return _ASSET_CACHE[name]


# ---------------------------------------------------------------- stylesheet

def build_qss(t: dict[str, str], a: dict[str, str]) -> str:
    dis = lambda c: rgba(c, 0.5)  # noqa: E731 - "disabled" = the same colour at 50 % opacity
    sb = _semi_css()
    SANS = DISPLAY = _css_families(ui_families())  # (one family for every size)
    MONO = f'"{MONO_FAMILY}"' if MONO_FAMILY != "monospace" else "monospace"
    return f"""
* {{ font-family: {SANS}; font-size: 14px; color: {t['tx']}; outline: 0; }}
QMainWindow, QDialog, QWidget#root {{ background: {t['bg']}; }}
QWidget {{ background: transparent; }}
QDialog {{ background: {t['fl']}; }}
QLabel {{ background: transparent; }}
QLabel:disabled {{ color: {dis(t['tx'])}; }}
QLabel[role="muted"] {{ color: {t['tx2']}; }}
QLabel[role="hint"] {{ color: {t['tx3']}; font-size: 13px; }}
QLabel[role="field"] {{ color: {t['tx2']}; font-size: 13px; }}
QLabel[role="section"] {{ font-size: 14px; font-weight: {sb}; }}
QLabel[role="title"] {{ font-family: {DISPLAY}; font-size: 24px; font-weight: {sb}; }}
QLabel[role="h1"] {{ font-family: {DISPLAY}; font-size: 36px; font-weight: {sb}; }}
QLabel[role="h3"] {{ font-family: {DISPLAY}; font-size: 18px; font-weight: {sb}; }}
QLabel[role="num"] {{ font-family: {DISPLAY}; font-size: 32px; font-weight: {sb}; }}
QLabel[role="status"] {{ font-family: {DISPLAY}; font-size: 40px; font-weight: {sb}; }}
QLabel[role="mono"] {{ font-family: {MONO}; font-size: 13px; }}
QLabel[role="count"] {{ background: {t['tb']}; border-radius: 9px; padding: 0 8px; color: {t['tx2']}; font-size: 13px;
    min-height: 18px; max-height: 18px; }}
QLabel[role="kbd"] {{ background: {t['cd']}; border: 1px solid {t['bd2']}; border-radius: 6px; padding: 0 7px;
    font-family: {MONO}; font-size: 13px; min-height: 22px; max-height: 22px; }}
QLabel[tag] {{ border-radius: 9px; padding: 0 8px; font-size: 13px; min-height: 18px; max-height: 18px;
    background: {t['tb']}; color: {t['tx2']}; }}
QLabel[tag="ok"], QLabel[tag="wn"], QLabel[tag="er"], QLabel[tag="if"], QLabel[tag="ac"] {{ color: {t['tx']}; }}
QLabel[tag="er"] {{ background: {t['er_bg']}; border: 1px solid {t['er']}; }}
QLabel[tag="wn"] {{ background: {t['wn_bg']}; border: 1px solid {t['wn']}; }}

QFrame[card="true"] {{ background: {t['cd']}; border: 1px solid {t['bd']}; border-radius: 12px; }}
QFrame[card="true"] QFrame[card="true"] {{ border-radius: 8px; }}
QFrame[bare="true"] {{ background: transparent; border: 0; }}
QFrame#topbar {{ background: {t['pn']}; border: 0; border-bottom: 1px solid {t['bd']}; }}
QFrame#dock {{ background: {t['pn']}; border: 0; border-top: 1px solid {t['bd']}; }}
QFrame#logdrawer {{ background: {t['lg_bg']}; border: 0; }}
QFrame[alert] {{ border-radius: 8px; border: 0; }}
QFrame[alert="info"] {{ background: {t['inf_bg']}; }}
QFrame[alert="ok"] {{ background: {t['ok_bg']}; }}
QFrame[alert="wn"] {{ background: {t['wn_bg']}; }}
QFrame[alert="er"] {{ background: {t['er_bg']}; }}
QFrame[alert][plain="true"] {{ background: transparent; }}
QFrame[seg="true"] {{ background: {t['tb']}; border: 0; border-radius: 8px; }}
QFrame[sep="true"] {{ background: {t['bd']}; border: 0; max-height: 1px; min-height: 1px; }}
QFrame[tile="true"] {{ background: transparent; border: 0; }}
QFrame[dlgfoot="true"] {{ background: {t['pn']}; border: 0; border-top: 1px solid {t['bd']}; }}
QFrame[choice="true"] {{ background: {t['cd']}; border: 1px solid {t['bd']}; border-radius: 8px; }}
QFrame[choice="true"][on="true"] {{ background: {t['ac_soft']}; border: 1px solid {t['fc']}; }}
QFrame[gi] {{ border: 0; border-radius: 8px; }}
QFrame[gi="ac"], QFrame[gi="ok"], QFrame[gi="if"] {{ background: {t['tb']}; }}
QFrame[gi="wn"] {{ background: {t['wn_bg']}; }}
QFrame[gi="er"] {{ background: {t['er_bg']}; }}
QFrame[stepno="true"] {{ background: {t['tb']}; border: 0; border-radius: 8px; }}

QPushButton, QToolButton[btn="true"] {{ min-height: 36px; max-height: 36px; padding: 0 16px; border-radius: 8px;
    border: 1px solid {t['bd2']}; background: {t['cd']}; color: {t['tx']}; font-weight: {sb}; }}
QPushButton:hover, QToolButton[btn="true"]:hover {{ background: {t['hv']}; }}
QPushButton:pressed, QToolButton[btn="true"]:pressed {{ background: {t['pr']}; }}
QPushButton:focus, QToolButton[btn="true"]:focus {{ border: 1px solid {t['ac']}; }}
QPushButton:disabled, QToolButton[btn="true"]:disabled {{ color: {dis(t['tx'])}; border-color: {rgba(t['bd2'], .5)};
    background: {t['cd']}; }}
QPushButton[variant="primary"], QToolButton[variant="primary"] {{ background: {t['ac']}; border: 1px solid transparent;
    color: {t['on_ac']}; font-weight: {sb}; }}
QPushButton[variant="primary"]:hover, QToolButton[variant="primary"]:hover {{ background: {t['ac_h']}; }}
QPushButton[variant="primary"]:pressed, QToolButton[variant="primary"]:pressed {{ background: {t['ac_p']}; color: {t['on_ac_p']}; }}
QPushButton[variant="primary"]:disabled, QToolButton[variant="primary"]:disabled {{ background: {rgba(t['ac'], .5)};
    color: {rgba(t['on_ac'], .5)}; border-color: transparent; }}
QPushButton[variant="danger"], QToolButton[variant="danger"] {{ background: {t['dg']}; border: 1px solid transparent;
    color: {t['on_dg']}; font-weight: {sb}; }}
QPushButton[variant="danger"]:hover, QToolButton[variant="danger"]:hover {{ background: {rgba(t['dg'], .88)}; }}
QPushButton[variant="danger"]:pressed {{ background: {rgba(t['dg'], .75)}; }}
QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent;
    color: {t['tx2']}; }}
QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{ background: {t['hv']}; color: {t['tx']}; }}
QPushButton[variant="ghost"]:pressed, QToolButton[variant="ghost"]:pressed {{ background: {t['pr']}; }}
QPushButton[variant="link"] {{ background: transparent; border: 0; color: {t['ac_tx']}; padding: 0 6px; min-height: 24px;
    max-height: 24px; }}
QPushButton[variant="link"]:hover {{ background: transparent; text-decoration: underline; }}
QPushButton[add="true"] {{ background: transparent; border: 1px solid transparent; color: {t['tx2']}; text-align: left;
    padding: 0 12px; }}
QPushButton[add="true"]:hover {{ background: {t['hv']}; color: {t['tx']}; }}
QPushButton[add="true"]:disabled {{ color: {dis(t['tx2'])}; background: transparent; border-color: transparent; }}
QPushButton[size="lg"], QToolButton[size="lg"] {{ min-height: 44px; max-height: 44px; padding: 0 24px; font-size: 15px; }}
QPushButton[size="sm"], QToolButton[size="sm"] {{ min-height: 30px; max-height: 30px; padding: 0 12px; font-size: 13px; }}
QToolButton[icononly="true"] {{ padding: 0; min-width: 36px; max-width: 36px; }}
QToolButton[icononly="true"][size="sm"] {{ min-width: 30px; max-width: 30px; }}
QToolButton[btn="true"][variant="ghost"][icononly="true"] {{ border: 1px solid transparent; }}

QToolButton[seg="true"] {{ min-height: 28px; max-height: 28px; padding: 0 14px; border: 1px solid transparent;
    border-radius: 6px; background: transparent; color: {t['tx2']}; font-weight: {sb}; font-size: 13px; }}
QToolButton[seg="true"]:hover {{ color: {t['tx']}; }}
QToolButton[seg="true"]:checked {{ background: {t['fl']}; color: {t['tx']}; border: 1px solid {t['bd2']}; }}
QToolButton[seg="true"]:focus {{ border: 1px solid {t['ac']}; }}
QToolButton[seg="true"]:disabled {{ color: {dis(t['tx'])}; }}
QToolButton[tab="true"] {{ min-height: 36px; max-height: 36px; padding: 0 14px; border: 0; border-bottom: 2px solid transparent;
    background: transparent; color: {t['tx3']}; font-weight: {sb}; border-radius: 0; }}
QToolButton[tab="true"]:hover {{ color: {t['tx']}; }}
QToolButton[tab="true"]:checked {{ color: {t['tx']}; border-bottom: 2px solid {t['tx']}; }}
QToolButton[grouphead="true"] {{ border: 0; background: transparent; font-weight: {sb}; font-size: 14px; text-align: left;
    padding: 0 6px; min-height: 36px; max-height: 36px; border-radius: 8px; }}
QToolButton[grouphead="true"]:hover {{ background: {t['hv']}; }}
QToolButton[chip="true"] {{ border: 0; background: transparent; padding: 4px; border-radius: 8px; }}
QToolButton[chip="true"]:hover {{ background: {t['hv']}; }}
QToolButton[chip="true"]:disabled {{ background: transparent; }}
QToolButton[topbtn="true"] {{ min-height: 36px; max-height: 36px; padding: 0 10px; border: 1px solid transparent;
    background: transparent; color: {t['tx2']}; border-radius: 8px; font-weight: {sb}; }}
QToolButton[topbtn="true"]:hover {{ background: {t['hv']}; color: {t['tx']}; }}
QToolButton[topbtn="true"]:pressed {{ background: {t['pr']}; }}
QToolButton[topbtn="true"]:disabled {{ color: {dis(t['tx2'])}; }}
QToolButton[profile="true"] {{ min-height: 36px; max-height: 36px; padding: 0 10px 0 10px; border: 1px solid transparent;
    border-radius: 8px; background: transparent; font-weight: {sb}; font-size: 15px; text-align: left; }}
QToolButton[profile="true"]:hover {{ background: {t['hv']}; }}
QToolButton[profile="true"]:disabled {{ color: {dis(t['tx'])}; }}
QToolButton[profile="true"]::menu-indicator {{ image: none; width: 0; }}
QToolButton[more="true"]::menu-indicator {{ image: none; width: 0; }}
QToolButton[logtoggle="true"] {{ border: 1px solid transparent; background: transparent; font-weight: {sb}; font-size: 13px;
    color: {t['tx2']}; padding: 0 10px; min-height: 36px; max-height: 36px; border-radius: 8px; }}
QToolButton[logtoggle="true"]:hover {{ background: {t['hv']}; color: {t['tx']}; }}
QToolButton[logtoggle="true"]:checked {{ background: {t['tb']}; color: {t['tx']}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {t['inp']}; border: 1px solid {t['bd2']}; border-radius: 8px;
    min-height: 34px; max-height: 34px; padding: 0 12px; selection-background-color: {t['ac']}; selection-color: {t['on_ac']}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {t['tx']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {t['ac']}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ background: {t['bg']};
    color: {dis(t['tx'])}; border-color: {rgba(t['bd2'], .5)}; }}
QLineEdit[hotkey="true"] {{ font-family: {MONO}; font-weight: {sb}; }}
QLineEdit[title="true"] {{ background: transparent; border: 0; border-bottom: 1px solid {t['bd']}; border-radius: 0;
    font-family: {DISPLAY}; font-size: 24px; font-weight: {sb}; padding: 0; min-height: 32px; max-height: 32px; }}
QLineEdit[title="true"]:hover {{ border-bottom: 1px solid {t['bd2']}; }}
QLineEdit[title="true"]:focus {{ border: 0; border-bottom: 1px solid {t['ac']}; }}
QLineEdit[title="true"]:disabled {{ background: transparent; border-bottom: 1px solid {t['bd']}; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: 0; background: transparent; margin: 2px 2px 0 0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: 0; background: transparent; margin: 0 2px 2px 0; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({a['up']}); width: 9px; height: 5px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({a['down']}); width: 9px; height: 5px; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ subcontrol-origin: border; subcontrol-position: center right; width: 28px; border: 0; }}
QComboBox::down-arrow {{ image: url({a['chev']}); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{ background: {t['fl']}; border: 1px solid {t['bd2']}; border-radius: 0; padding: 4px;
    selection-background-color: {t['hv']}; selection-color: {t['tx']}; outline: 0; }}
QComboBox QAbstractItemView::item {{ min-height: 32px; padding: 0 8px; border-radius: 0; }}

QPlainTextEdit, QTextEdit {{ background: {t['inp']}; border: 1px solid {t['bd2']}; border-radius: 8px; padding: 6px 10px;
    selection-background-color: {t['ac']}; selection-color: {t['on_ac']}; }}
QPlainTextEdit:focus {{ border: 1px solid {t['ac']}; }}
QPlainTextEdit[role="log"] {{ background: {t['lg_bg']}; color: {t['lg_i']}; border: 0; border-radius: 0;
    font-family: {MONO}; font-size: 13px; padding: 8px 24px; selection-background-color: {t['ac']}; }}

QCheckBox {{ spacing: 10px; color: {t['tx']}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1.5px solid {t['bd2']}; background: transparent; }}
QCheckBox::indicator:hover {{ border-color: {t['tx']}; }}
QCheckBox::indicator:checked {{ background: {t['sw_on']}; border-color: {t['sw_on']}; image: url({a['check']}); }}
QCheckBox::indicator:disabled {{ border-color: {dis(t['bd2'])}; }}
QCheckBox:disabled {{ color: {dis(t['tx'])}; }}
QRadioButton::indicator {{ width: 18px; height: 18px; border-radius: 9px; border: 1.5px solid {t['bd2']}; }}
QRadioButton::indicator:checked {{ border: 5px solid {t['tx']}; background: {t['on_dg']}; }}

QSlider:horizontal {{ min-height: 24px; }}
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {t['pr']}; margin: 0; }}
QSlider::add-page:horizontal {{ background: {t['pr']}; border-radius: 2px; height: 4px; }}
QSlider::sub-page:horizontal {{ background: {t['tx']}; border-radius: 2px; height: 4px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -8px 0; border-radius: 9px; background: {t['cd']};
    border: 2px solid {t['tx']}; }}
QSlider::handle:horizontal:hover {{ background: {t['hv']}; }}
QSlider::handle:horizontal:focus {{ border: 2px solid {t['ac']}; }}
QSlider::sub-page:horizontal:disabled {{ background: {t['tx3']}; }}
QSlider::handle:horizontal:disabled {{ border-color: {t['tx3']}; background: {t['cd']}; }}
QProgressBar {{ height: 6px; max-height: 6px; border-radius: 3px; background: {t['pr']}; border: 0; text-align: center; }}
QProgressBar::chunk {{ background: {t['tx']}; border-radius: 3px; }}

QScrollArea {{ border: 0; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['bd']}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {t['bd2']}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {t['bd']}; border-radius: 4px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; background: transparent; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QListWidget#steps {{ background: transparent; border: 0; padding: 0; }}
QToolTip {{ background: {t['tp']}; color: {t['tpt']}; border: 0; border-radius: 6px; padding: 6px 10px; font-size: 13px; }}
QMenu {{ background: {t['fl']}; border: 1px solid {t['bd2']}; border-radius: 0; padding: 4px; }}
QMenu::item {{ padding: 0 28px 0 12px; min-height: 32px; border-radius: 0; color: {t['tx']}; }}
QMenu::item:selected {{ background: {t['hv']}; }}
QMenu::item:disabled {{ color: {t['tx3']}; }}
QMenu::separator {{ height: 1px; background: {t['bd']}; margin: 4px 6px; }}
QSplitter::handle {{ background: transparent; }}
QMessageBox {{ background: {t['fl']}; }}
QStatusBar {{ background: {t['pn']}; color: {t['tx3']}; }}
"""


def _palette(t: dict[str, str]) -> QPalette:
    p = QPalette()
    role = QPalette.ColorRole
    for r, c in ((role.Window, t["bg"]), (role.WindowText, t["tx"]), (role.Base, t["inp"]), (role.AlternateBase, t["cd"]),
                 (role.Text, t["tx"]), (role.Button, t["cd"]), (role.ButtonText, t["tx"]), (role.Highlight, t["ac"]),
                 (role.HighlightedText, t["on_ac"]), (role.ToolTipBase, t["tp"]), (role.ToolTipText, t["tpt"]),
                 (role.PlaceholderText, t["tx3"]), (role.Link, t["ac_tx"]), (role.BrightText, t["tx"])):
        p.setColor(r, parse_color(c))
    for r in (role.WindowText, role.Text, role.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, r, parse_color(t["tx3"]))
    return p


_applied: dict = {"app": None, "qss": None}


def clear_stylesheet(app: QApplication | None = None) -> None:
    """Remove the application stylesheet (the next :func:`apply` sets it again). Used while the window is rebuilt: building
    widgets without a stylesheet and styling the finished tree once is much cheaper than styling every widget as it is made."""
    app = app or QApplication.instance()
    if app is not None:
        _applied["qss"] = None
        app.setStyleSheet("")


def apply(app: QApplication | None = None, pref: str | None = None, *, stylesheet: bool = True) -> str:
    """(Re)apply the stylesheet to the whole application (``pref`` is ignored: there is only the light theme).

    Cheap to call again: the fonts, the style and the palette are set once, and an identical stylesheet is not set twice
    (setting a stylesheet makes Qt restyle every widget of the application). ``stylesheet=False`` sets up only the fonts,
    style and palette: at start-up the window is built first and the stylesheet applied to the finished tree afterwards.
    """
    app = app or QApplication.instance()
    t = TOKENS[_mode]
    if app is not None:
        load_fonts()
        if _applied["app"] is not app:  # first time for this QApplication
            _applied["app"], _applied["qss"] = app, None
            app.setStyle("Fusion")  # QSS draws on top of a predictable base on every OS
            app.setPalette(_palette(t))
        qss = build_qss(t, _assets(t, _mode)) if stylesheet else None
        if qss is not None and qss != _applied["qss"]:
            _applied["qss"] = qss
            app.setStyleSheet(qss)
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass
    return _mode
