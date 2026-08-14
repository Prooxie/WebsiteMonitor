"""Visual theme: a flat, modern Qt stylesheet in dark and light variants.

Qt's default widget style looks like Windows 2000. Everything here exists to
avoid that: flat surfaces, 8px corner radii, a single accent colour, generous
padding and a type scale that does not fight the OS font.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DARK", "LIGHT", "Palette", "build_stylesheet", "palette_for"]


@dataclass(frozen=True, slots=True)
class Palette:
    """A colour set for one theme variant.

    Attributes:
        bg: Window background.
        surface: Panel and card background.
        surface_alt: Slightly raised surface for inputs and hovered rows.
        border: Hairline separators.
        text: Primary text.
        text_dim: Secondary text and captions.
        accent: Interactive accent for primary buttons and selection.
        accent_hover: Accent under the cursor.
        success: "Up to date" indicator.
        warning: "Selector missed" indicator.
        danger: Error indicator and destructive actions.
        is_dark: Whether this palette is the dark variant.
    """

    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_dim: str
    accent: str
    accent_hover: str
    success: str
    warning: str
    danger: str
    is_dark: bool


DARK = Palette(
    bg="#0f1115",
    surface="#161920",
    surface_alt="#1d212b",
    border="#272c38",
    text="#e6e8eb",
    text_dim="#8b94a6",
    accent="#3b82f6",
    accent_hover="#60a5fa",
    success="#22c55e",
    warning="#f59e0b",
    danger="#ef4444",
    is_dark=True,
)

LIGHT = Palette(
    bg="#f5f6f8",
    surface="#ffffff",
    surface_alt="#eef0f4",
    border="#d8dce4",
    text="#151821",
    text_dim="#5c6577",
    accent="#2563eb",
    accent_hover="#3b82f6",
    success="#16a34a",
    warning="#d97706",
    danger="#dc2626",
    is_dark=False,
)


def palette_for(theme: str) -> Palette:
    """Resolve a theme name to a palette.

    Args:
        theme: ``"dark"``, ``"light"`` or ``"system"``. ``"system"`` follows the
            OS colour scheme reported by Qt, falling back to dark.

    Returns:
        The matching palette.
    """
    if theme == "light":
        return LIGHT
    if theme == "system":
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QGuiApplication

            scheme = QGuiApplication.styleHints().colorScheme()
            return LIGHT if scheme == Qt.ColorScheme.Light else DARK
        except Exception:  # pragma: no cover - depends on Qt version
            return DARK
    return DARK


def build_stylesheet(palette: Palette) -> str:
    """Render the application stylesheet for a palette.

    Args:
        palette: Colours to substitute into the stylesheet.

    Returns:
        A Qt Style Sheet string ready for ``QApplication.setStyleSheet``.
    """
    p = palette
    # Selection colour needs alpha; Qt stylesheets accept rgba().
    accent_soft = "rgba(59,130,246,0.18)" if p.is_dark else "rgba(37,99,235,0.12)"

    return f"""
* {{
    font-family: "Segoe UI Variable Display", "Segoe UI", -apple-system, "Inter", sans-serif;
    font-size: 13px;
    outline: none;
}}

QWidget {{
    background: {p.bg};
    color: {p.text};
}}

QMainWindow, QDialog {{
    background: {p.bg};
}}

/* ---------- Panels ---------- */
QFrame#Card, QGroupBox {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
}}

QGroupBox {{
    margin-top: 18px;
    padding: 16px 14px 14px 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {p.text_dim};
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}}

/* ---------- Buttons ---------- */
QPushButton {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 7px 16px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {p.border};
    border-color: {p.accent};
}}
QPushButton:pressed {{
    background: {p.surface};
}}
QPushButton:disabled {{
    color: {p.text_dim};
    background: {p.surface};
    border-color: {p.border};
}}
QPushButton[accent="true"] {{
    background: {p.accent};
    border-color: {p.accent};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QPushButton[danger="true"] {{
    background: transparent;
    border-color: {p.danger};
    color: {p.danger};
}}
QPushButton[danger="true"]:hover {{
    background: {p.danger};
    color: #ffffff;
}}
QPushButton[flat="true"] {{
    background: transparent;
    border: none;
    padding: 6px 10px;
    color: {p.text_dim};
    font-size: 15px;
}}
QPushButton[flat="true"]:hover {{
    background: {p.surface_alt};
    color: {p.text};
}}

/* ---------- Inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 7px 10px;
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: #ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent};
    background: {p.surface};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {p.text_dim};
}}
QLineEdit[monospace="true"], QPlainTextEdit[monospace="true"] {{
    font-family: "Cascadia Code", "Consolas", "SF Mono", monospace;
    font-size: 12px;
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_dim};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {p.accent};
    selection-color: #ffffff;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    width: 16px;
    border: none;
    background: transparent;
}}

/* ---------- Checkboxes ---------- */
QCheckBox {{
    spacing: 8px;
    padding: 3px 0;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid {p.border};
    background: {p.surface_alt};
}}
QCheckBox::indicator:hover {{
    border-color: {p.accent};
}}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: none;
}}

/* ---------- Lists ---------- */
QListWidget, QTreeWidget, QTableWidget {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 5px;
}}
QListWidget::item {{
    border-radius: 8px;
    padding: 2px;
    margin: 2px 3px;
}}
QListWidget::item:hover {{
    background: {p.surface_alt};
}}
QListWidget::item:selected {{
    background: {accent_soft};
    border: 1px solid {p.accent};
}}

/* ---------- Tabs ---------- */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 10px;
    background: {p.surface};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 8px 18px;
    margin-right: 3px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 500;
}}
QTabBar::tab:hover {{
    color: {p.text};
    background: {p.surface_alt};
}}
QTabBar::tab:selected {{
    background: {p.surface};
    color: {p.text};
    border-color: {p.border};
    border-bottom-color: {p.surface};
}}

/* ---------- Toolbar ---------- */
QToolBar {{
    background: {p.surface};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 7px 10px;
    spacing: 7px;
}}
QToolBar QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 6px 12px;
    color: {p.text};
    font-weight: 500;
}}
QToolBar QToolButton:hover {{
    background: {p.surface_alt};
    border-color: {p.border};
}}
QToolBar QToolButton:disabled {{
    color: {p.text_dim};
}}

/* ---------- Status bar ---------- */
QStatusBar {{
    background: {p.surface};
    border-top: 1px solid {p.border};
    color: {p.text_dim};
}}
QStatusBar::item {{ border: none; }}

/* ---------- Scrollbars ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.text_dim}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- Splitter ---------- */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:hover {{ background: {p.accent}; }}

/* ---------- Misc ---------- */
QLabel[dim="true"] {{ color: {p.text_dim}; }}
QLabel[heading="true"] {{ font-size: 17px; font-weight: 600; }}
QLabel[mono="true"] {{
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
    color: {p.accent_hover};
}}
QProgressBar {{
    background: {p.surface_alt};
    border: none;
    border-radius: 3px;
    height: 5px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 3px;
}}
QToolTip {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 6px 9px;
}}
QMenu {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 22px 7px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{ background: {p.accent}; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}
"""
