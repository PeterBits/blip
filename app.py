"""
Blip: monitor de escritorio para sesiones de Claude Code.

Vigila la carpeta ~/.claude/blip/ donde los hooks escriben el estado
de cada sesion y muestra un semaforo al lado del nombre de cada terminal:

    verde    -> trabajando
    amarillo -> requiere tu atencion (pregunta / permiso)
    rojo     -> termino, espera mas prompts

La ventana esta siempre encima del resto (always-on-top); solo se puede
minimizar.
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime, timezone
from time import monotonic

try:
    import psutil
except ImportError:
    psutil = None


def pid_alive(pid: int) -> bool:
    """True si el proceso 'pid' sigue vivo y es una sesion claude.

    Si no hay psutil o el pid es 0/desconocido, devuelve True (no podemos
    afirmar que este muerto, asi que no lo ocultamos por si acaso).
    """
    if not pid or psutil is None:
        return True
    try:
        p = psutil.Process(pid)
        if not p.is_running():
            return False
        name = (p.name() or "").lower()
        return name in ("claude.exe", "claude")
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return True

# Nombres de proceso que suelen ser la ventana de una terminal.
_TERMINAL_NAMES = {
    "windowsterminal.exe", "wt.exe", "openconsole.exe", "conhost.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "code.exe", "code - insiders.exe",
    "alacritty.exe", "wezterm-gui.exe", "conemu.exe", "conemu64.exe",
    "mintty.exe", "hyper.exe", "tabby.exe",
}


def _related_pids(pid: int) -> set:
    """PID + ancestros + descendientes: donde puede vivir la ventana host.

    La terminal que lanzo 'claude' suele ser un ancestro (p. ej.
    WindowsTerminal -> shell -> claude), y a veces el host es un
    descendiente (conhost). Recogemos ambos para localizar su ventana.
    """
    pids = {pid}
    try:
        import psutil
        p = psutil.Process(pid)
        cur = p
        for _ in range(20):
            cur = cur.parent()
            if cur is None:
                break
            pids.add(cur.pid)
        for ch in p.children(recursive=True):
            pids.add(ch.pid)
    except Exception:
        pass
    return pids


def _pname(pid: int) -> str:
    try:
        import psutil
        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        return ""


# Glyphs de estado que Claude Code antepone al titulo de la terminal
# (spinner, marcas de progreso). Se ignoran al comparar titulos.
_TITLE_JUNK = "◐◑◒◓●○◍◌◉✻✽✶✳✷✦∗*·•–—-‐ \t\r\n"


def _norm_title(s: str) -> str:
    """Normaliza un titulo para comparar: minusculas, sin glyphs de estado
    al principio ni puntos suspensivos/espacios al final, espacios colapsados.
    """
    s = " ".join((s or "").split()).lower()
    s = s.lstrip(_TITLE_JUNK).rstrip("… .")
    return s


def _title_score(win_title: str, sess_title: str) -> int:
    """Puntua cuanto encaja el titulo de una ventana con el de la sesion.

    El titulo de la ventana suele venir con un glyph de spinner delante y
    truncado (WT recorta a ~55 chars), asi que basta con que uno sea prefijo
    del otro (o contenido en el otro). Devuelve la longitud coincidente, 0
    si no encaja o el titulo de sesion es muy corto para fiarse.
    """
    w = _norm_title(win_title)
    t = _norm_title(sess_title)
    if len(t) < 6 or len(w) < 6:
        return 0
    if t.startswith(w) or w.startswith(t) or t in w or w in t:
        return min(len(w), len(t))
    return 0


def focus_terminal(pid: int, title: str = "", repo: str = "") -> bool:
    """Trae al frente la ventana de la terminal de una sesion de Claude Code.

    Universal por terminal, prueba en orden:
      1) Por TITULO: la ventana cuyo titulo coincide con el de la
         conversacion. Es lo que hace Windows Terminal (una ventana/pestana
         por sesion, titulada con la conversacion) y lo mas preciso.
      2) Consola clasica (conhost): AttachConsole(pid) + GetConsoleWindow.
      3) Por proceso: ventana de un proceso emparentado que parece terminal.
      4) Ultimo recurso: una unica ventana de Windows Terminal visible.

    Devuelve True si logro enfocar algo.
    """
    if not pid or sys.platform != "win32":
        return False
    try:
        return _focus_terminal_win(int(pid), title or "", repo or "")
    except Exception:
        return False


def _focus_terminal_win(pid: int, title: str, repo: str) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    k32.AttachConsole.argtypes = [wintypes.DWORD]
    k32.GetConsoleWindow.restype = wintypes.HWND
    k32.GetCurrentThreadId.restype = wintypes.DWORD
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetActiveWindow.argtypes = [wintypes.HWND]
    u32.BringWindowToTop.argtypes = [wintypes.HWND]
    u32.AttachThreadInput.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    SW_RESTORE, SW_SHOW = 9, 5

    def focus(hwnd) -> bool:
        """Trae hwnd al frente de forma fiable (salto entre procesos).

        SetForegroundWindow solo no basta cuando el objetivo es de otro
        proceso: hay que 'engancharse' al hilo de la ventana en primer plano
        y a la del objetivo con AttachThreadInput para saltarnos el bloqueo
        de foco de Windows.
        """
        if not hwnd or not u32.IsWindow(hwnd):
            return False
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, SW_RESTORE)
        fg = u32.GetForegroundWindow()
        t_me = k32.GetCurrentThreadId()
        t_tg = u32.GetWindowThreadProcessId(hwnd, None)
        t_fg = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        if t_fg and t_fg != t_me:
            u32.AttachThreadInput(t_me, t_fg, True)
        if t_tg and t_tg != t_me:
            u32.AttachThreadInput(t_me, t_tg, True)
        u32.BringWindowToTop(hwnd)
        u32.ShowWindow(hwnd, SW_SHOW)
        u32.SetForegroundWindow(hwnd)
        u32.SetActiveWindow(hwnd)
        if t_tg and t_tg != t_me:
            u32.AttachThreadInput(t_me, t_tg, False)
        if t_fg and t_fg != t_me:
            u32.AttachThreadInput(t_me, t_fg, False)
        return True

    # Enumerar ventanas visibles con titulo (hwnd, pid, nombre_proc, titulo).
    windows = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n > 0:
                wpid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                windows.append((hwnd, wpid.value, _pname(wpid.value),
                                buf.value))
        return True

    u32.EnumWindows(WNDENUMPROC(collect), 0)
    own = os.getpid()

    # --- 1) Por titulo de conversacion (lo mas preciso; ideal para WT) ---
    if title:
        best = None
        best_score = 0
        for hwnd, wpid, name, wtitle in windows:
            if wpid == own or name not in _TERMINAL_NAMES:
                continue
            score = _title_score(wtitle, title)
            if score > best_score:
                best_score, best = score, hwnd
        if best is not None:
            return focus(best)

    # --- 2) Consola clasica via AttachConsole ----------------------------
    k32.FreeConsole()  # soltar nuestra consola (si la hay) antes de unirnos
    console_hwnd = 0
    if k32.AttachConsole(pid):
        try:
            console_hwnd = k32.GetConsoleWindow()
        finally:
            k32.FreeConsole()
    if console_hwnd and u32.IsWindowVisible(console_hwnd):
        return focus(console_hwnd)

    # --- 3) Por proceso emparentado --------------------------------------
    related = _related_pids(pid)
    strong = weak = None
    for hwnd, wpid, name, _wtitle in windows:
        if wpid == own:
            continue
        if wpid in related and name in _TERMINAL_NAMES:
            strong = hwnd
            break
        if weak is None and wpid in related and name != "explorer.exe":
            weak = hwnd
    target = strong or weak

    # --- 4) Ultimo recurso: una unica ventana de Windows Terminal --------
    if target is None:
        terms = [h for (h, _p, name, _t) in windows
                 if name in ("windowsterminal.exe", "wt.exe")]
        if len(terms) == 1:
            target = terms[0]

    return focus(target) if target else False


from PySide6.QtCore import Qt, QTimer, QRectF, Signal, QEvent
from PySide6.QtGui import QColor, QPainter, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QFrame,
    QPushButton,
)


def resource_path(rel: str) -> Path:
    """Ruta a un recurso, tanto en desarrollo como dentro del .exe.

    PyInstaller descomprime los datos en sys._MEIPASS al ejecutar el .exe;
    en desarrollo el recurso esta junto a este fichero.
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).parent
    return root / rel


def app_icon() -> QIcon:
    """Icono de Blip (circulo verde). Vacio si no se encuentra el fichero."""
    ico = resource_path("assets/blip.ico")
    return QIcon(str(ico)) if ico.exists() else QIcon()

STATE_DIR = Path.home() / ".claude" / "blip"

# Preferencias persistentes de la app (p. ej. si el modo Split esta activo).
# Sibling de STATE_DIR a proposito: dentro de STATE_DIR se leeria como una
# "sesion" mas (refresh() hace glob de *.json en esa carpeta).
SETTINGS_FILE = Path.home() / ".claude" / "blip.settings.json"


def load_settings() -> dict:
    """Lee las preferencias guardadas; {} si no hay o esta corrupto."""
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    """Guarda las preferencias (silencioso si no se puede escribir)."""
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


# Si una sesion no se actualiza en este tiempo, se considera obsoleta.
STALE_SECONDS = 60 * 30  # 30 min

COLORS = {
    "green": QColor("#2ecc71"),
    "yellow": QColor("#e67e22"),  # naranja: "te necesita" (elegido por el usuario)
    "red": QColor("#e74c3c"),
    "gray": QColor("#7f8c8d"),
}

# Prioridad del icono de la barra de tareas: el estado mas urgente manda.
# naranja (te necesita) > rojo (terminado) > verde (trabajando) > gris (nada).
OVERALL_PRIORITY = ["yellow", "red", "green"]

# Cache de iconos generados por color, para no redibujar en cada refresco.
_icon_cache: dict = {}


def state_icon(state: str) -> QIcon:
    """Icono de la barra de tareas: un circulo del color del estado.

    Se dibuja en memoria (varios tamanos) y se cachea por color.
    """
    if state in _icon_cache:
        return _icon_cache[state]
    color = COLORS.get(state, COLORS["gray"])
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        m = size * 0.12
        p.drawEllipse(QRectF(m, m, size - 2 * m, size - 2 * m))
        p.end()
        icon.addPixmap(pm)
    _icon_cache[state] = icon
    return icon


def overall_state(states) -> str:
    """Estado global mas urgente de una lista de estados de sesion."""
    present = set(states)
    for s in OVERALL_PRIORITY:
        if s in present:
            return s
    return "gray"

LABELS = {
    "green": "trabajando",
    "yellow": "te necesita",
    "red": "terminado",
    "gray": "inactiva",
}

# Prioridad de ordenacion: primero lo que reclama tu atencion.
STATE_ORDER = {"yellow": 0, "red": 1, "green": 2, "gray": 3}

# Fondo suave para resaltar filas que requieren tu actuacion.
ROW_BG = {
    "yellow": "#3a2a17",  # naranja apagado
    "red": "#3a1e1e",     # rojo apagado
}


def human_age(seconds: float) -> str:
    """Formatea una antiguedad en texto corto, mostrando solo la unidad
    mayor: 'hace 45s', 'hace 1m', 'hace 3h', 'hace 2d'.

    Ejemplos: 1:30 -> 'hace 1m'; 3:34:23 -> 'hace 3h'.
    """
    s = int(max(0, seconds))
    if s < 60:
        return f"hace {s}s"
    m = s // 60
    if m < 60:
        return f"hace {m}m"
    h = m // 60
    if h < 24:
        return f"hace {h}h"
    d = h // 24
    return f"hace {d}d"


class LightDot(QWidget):
    """Pequeno circulo de color: la 'luz' del semaforo."""

    def __init__(self, diameter: int = 16):
        super().__init__()
        self._color = COLORS["gray"]
        self._d = diameter
        self.setFixedSize(diameter, diameter)

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(self._color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(0, 0, self._d, self._d)


class StarButton(QLabel):
    """Estrella clicable para marcar una sesion como favorita.

    - Favorita: estrella dorada rellena (siempre visible).
    - No favorita: estrella vacia y tenue, solo visible al pasar el raton
      por la fila.
    """

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedWidth(18)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self._fav = False
        self._hover_row = False
        self.render()

    def set_favorite(self, fav: bool) -> None:
        self._fav = fav
        self.render()

    def set_row_hover(self, hovering: bool) -> None:
        self._hover_row = hovering
        self.render()

    def render(self) -> None:
        # Tamano de estrella reducido un 20% (15px -> 12px).
        if self._fav:
            self.setText("★")  # estrella rellena
            self.setStyleSheet("color: #f1c40f; font-size: 12px;")
        elif self._hover_row:
            self.setText("☆")  # estrella vacia
            self.setStyleSheet("color: #7f8c8d; font-size: 12px;")
        else:
            self.setText("")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Divider(QWidget):
    """Separador sutil entre las favoritas y el resto de sesiones.

    Una linea fina con un poco de aire arriba y abajo.
    """

    def __init__(self):
        super().__init__()
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 5, 14, 5)
        box.setSpacing(0)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("background: #33414d; border: none;")
        box.addWidget(line)


class SessionRow(QWidget):
    """Una fila: luz + (repo / titulo de conversacion) + estado + tiempo + estrella."""

    fav_toggled = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("row")
        # Seguir el raton para mostrar la estrella al hacer hover.
        self.setAttribute(Qt.WA_Hover, True)
        # Toda la fila es clicable (doble clic = favorita) -> cursor de mano.
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(10)

        self.dot = LightDot()

        # Bloque de texto: (repo + estrella) arriba, titulo debajo.
        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        text_box.setContentsMargins(0, 0, 0, 0)

        # Fila del nombre: nombre de la carpeta + estrella justo a su derecha.
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.setContentsMargins(0, 0, 0, 0)
        self.repo = QLabel("-")
        self.repo.setStyleSheet("color: #ecf0f1; font-size: 13px; font-weight: 600;")
        self.star = StarButton()
        self.star.clicked.connect(self.fav_toggled)
        name_row.addWidget(self.repo)
        name_row.addWidget(self.star)
        name_row.addStretch()

        self.title = QLabel("")
        self.title.setStyleSheet("color: #9aa5ad; font-size: 11px;")
        text_box.addLayout(name_row)
        text_box.addWidget(self.title)

        # Bloque derecho: estado arriba, tiempo debajo.
        right_box = QVBoxLayout()
        right_box.setSpacing(1)
        right_box.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel("-")
        self.status.setStyleSheet("color: #95a5a6; font-size: 12px;")
        self.status.setAlignment(Qt.AlignRight)
        self.age = QLabel("")
        self.age.setStyleSheet("color: #6b7680; font-size: 10px;")
        self.age.setAlignment(Qt.AlignRight)
        right_box.addWidget(self.status)
        right_box.addWidget(self.age)

        layout.addWidget(self.dot)
        layout.addLayout(text_box, 1)
        layout.addLayout(right_box)

        # Propagar el cursor de mano a los hijos, para que el pointer se vea
        # en todo el ancho de la fila (los QLabel no lo heredan por defecto).
        for w in (self.dot, self.repo, self.title, self.status, self.age):
            w.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event) -> None:
        self.star.set_row_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.star.set_row_hover(False)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # Doble clic en cualquier parte de la fila -> marcar/desmarcar favorita.
        if event.button() == Qt.LeftButton:
            self.fav_toggled.emit()
        super().mouseDoubleClickEvent(event)

    def update_from(self, data: dict, stale: bool, age_seconds: float,
                    favorite: bool = False) -> None:
        state = data.get("state", "gray")
        if stale:
            state = "gray"
        self.dot.set_color(COLORS.get(state, COLORS["gray"]))
        self.star.set_favorite(favorite)

        self.repo.setText(data.get("project") or data.get("session_id", "?")[:8])
        title = data.get("title", "") or ""
        self.title.setText(title)
        self.title.setVisible(bool(title))

        self.status.setText(LABELS.get(state, state))
        self.status.setStyleSheet(
            f"color: {COLORS.get(state, COLORS['gray']).name()}; "
            "font-size: 12px; font-weight: 600;"
        )
        # El tiempo solo interesa cuando la sesion te espera (naranja/rojo).
        # Mientras trabaja (verde) o esta inactiva (gris) no se muestra.
        self._show_age = state in ("yellow", "red")
        # Guardar la BASE del contador: cuantos segundos llevaba en el estado
        # en el momento de este refresco. tick_age() ira sumando el tiempo
        # transcurrido desde aqui, de forma suave e independiente del polling.
        self._age_base = age_seconds
        self._age_marker = monotonic()
        self._render_age()

        # Resaltar filas que reclaman atencion con un fondo suave.
        bg = ROW_BG.get(state)
        if bg:
            self.setStyleSheet(f"#row {{ background: {bg}; border-radius: 6px; }}")
        else:
            self.setStyleSheet("#row { background: transparent; }")

    def _render_age(self) -> None:
        if not getattr(self, "_show_age", False):
            self.age.setText("")
            self.age.setVisible(False)
            return
        elapsed = self._age_base + (monotonic() - self._age_marker)
        self.age.setText(human_age(elapsed))
        self.age.setVisible(True)

    def tick_age(self) -> None:
        """Actualiza solo el texto del tiempo (llamado cada segundo)."""
        self._render_age()


class PilotWindow(QWidget):
    """Piloto por sesion en la barra de tareas (modo Split).

    - Boton de la barra de tareas: el icono es el circulo del color del
      estado (la 'lucecita').
    - Preview al pasar el raton: el TITULO de la ventana (repo + estado +
      tiempo) aparece como cabecera de texto, y la miniatura muestra una
      tarjeta oscura con repo, titulo de la conversacion, estado y tiempo.

    Al clicar su boton en la barra de tareas Windows lo restaura y emite
    'clicked', que Blip usa para saltar a la terminal de esa sesion.
    """

    clicked = Signal()

    def __init__(self, info: dict):
        super().__init__()
        self._closing = False
        self._state = None
        self._caption = None
        self.pid = info.get("pid", 0)
        self.setWindowIcon(state_icon(info.get("state", "gray")))
        # Tarjeta pequena; su contenido es lo que se ve en la miniatura.
        self.setFixedSize(240, 74)
        self.setStyleSheet("background: #232f38; border-radius: 8px;")

        box = QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.setContentsMargins(0, 0, 0, 0)
        self.dot = LightDot(12)
        self.repo = QLabel("-")
        self.repo.setStyleSheet(
            "color: #ecf0f1; font-size: 13px; font-weight: 600;")
        top.addWidget(self.dot)
        top.addWidget(self.repo, 1)

        self.title = QLabel("")
        self.title.setStyleSheet("color: #9aa5ad; font-size: 11px;")
        self.status = QLabel("-")
        self.status.setStyleSheet("color: #95a5a6; font-size: 11px;")

        box.addLayout(top)
        box.addWidget(self.title)
        box.addWidget(self.status)

        self.apply(info)

        # Pintar una vez FUERA de pantalla y minimizar: asi la miniatura de
        # la barra de tareas muestra la tarjeta (no un cuadro en blanco) y no
        # hay parpadeo al clicar (la ventanita vive fuera de la vista).
        self.move(-20000, -20000)
        self.show()
        self.showMinimized()

    def apply(self, info: dict) -> None:
        """Refresca contenido/estado/pid del piloto sin recrearlo."""
        self.pid = info.get("pid", 0) or self.pid
        state = info.get("state", "gray")
        repo = info.get("repo", "-")
        conv = info.get("title") or ""
        age = info.get("age", 0.0)
        # Guardados para saltar a la terminal correcta al clicar.
        self.conv_title = conv
        self.repo_name = repo
        label = LABELS.get(state, state)
        waiting = state in ("yellow", "red")
        color = COLORS.get(state, COLORS["gray"]).name()

        self.repo.setText(repo)
        self.dot.set_color(COLORS.get(state, COLORS["gray"]))
        self.title.setText(conv)
        self.title.setVisible(bool(conv))
        self.status.setText(f"{label}  ·  {human_age(age)}" if waiting else label)
        self.status.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;")

        if state != self._state:
            self._state = state
            self.setWindowIcon(state_icon(state))

        # Cabecera de texto del preview (titulo de la ventana).
        caption = repo
        if conv:
            caption += f" — {conv}"
        caption += f"  ·  {label}"
        if waiting:
            caption += f"  ·  {human_age(age)}"
        if caption != self._caption:
            self._caption = caption
            self.setWindowTitle(caption)

    def close_silently(self) -> None:
        """Cierra el piloto sin que dispare 'clicked'."""
        self._closing = True
        self.close()

    def changeEvent(self, event) -> None:
        # Al quitarse el estado 'minimizado' (el usuario clico su boton en la
        # barra de tareas) avisamos para saltar a la terminal de la sesion.
        if event.type() == QEvent.WindowStateChange:
            if not self._closing and not (self.windowState() & Qt.WindowMinimized):
                self.clicked.emit()
        super().changeEvent(event)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blip")
        self.setWindowIcon(app_icon())
        # Always-on-top; ventana normal (se puede minimizar).
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.resize(340, 400)
        self.setStyleSheet("background: #1e272e;")

        # Modo Split (persistente): al minimizar, una lucecita por sesion en
        # la barra de tareas en vez de un unico icono.
        self.split_enabled = bool(load_settings().get("split", False))
        # Pilotos vivos (session_id -> PilotWindow) y si estamos en esa vista.
        self.pilots: dict[str, PilotWindow] = {}
        self._in_split_view = False
        # Ultima instantanea de sesiones (dicts) para los pilotos.
        self._session_snapshot: list[dict] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Cabecera con el boton Split alineado a la derecha.
        header = QHBoxLayout()
        header.setContentsMargins(10, 8, 8, 4)
        header.setSpacing(6)
        header.addStretch()
        self.split_btn = QPushButton("Split")
        self.split_btn.setCheckable(True)
        self.split_btn.setCursor(Qt.PointingHandCursor)
        self.split_btn.setChecked(self.split_enabled)
        self.split_btn.clicked.connect(self.toggle_split)
        self._style_split_btn()
        header.addWidget(self.split_btn)
        root.addLayout(header)

        self.empty = QLabel("  Sin sesiones activas")
        self.empty.setStyleSheet("color: #636e72; font-size: 12px; padding: 16px;")
        root.addWidget(self.empty)

        # Contenedor con scroll para las filas.
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.list_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list_container)
        scroll.setStyleSheet("border: none;")
        root.addWidget(scroll)

        # session_id -> SessionRow
        self.rows: dict[str, SessionRow] = {}

        # session_ids marcados como favoritos (persisten mientras la app
        # este abierta). Las favoritas van siempre arriba de la lista.
        self.favorites: set[str] = set()

        # Separador entre favoritas y el resto (se muestra solo si hay ambos).
        self.divider = Divider()
        self.divider.hide()
        self.list_layout.insertWidget(0, self.divider)

        # Timer de estado: relee los ficheros y actualiza colores/orden.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.refresh()

        # Timer del contador de tiempo: cada segundo exacto refresca solo el
        # texto "hace X", de forma suave e independiente del polling de 700ms.
        self.age_timer = QTimer(self)
        self.age_timer.timeout.connect(self.tick_ages)
        self.age_timer.start(1000)

    def tick_ages(self) -> None:
        for row in self.rows.values():
            row.tick_age()

    def refresh(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        seen: set[str] = set()
        active: list[tuple] = []  # (orden, sid, data, stale, age)

        for f in STATE_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            sid = data.get("session_id", f.stem)

            # Antiguedad del ULTIMO EVENTO (para detectar fantasma/obsoleta).
            last_event_age = 0.0
            ts_raw = data.get("updated_at")
            if ts_raw:
                try:
                    last_event_age = (now - datetime.fromisoformat(ts_raw)).total_seconds()
                except ValueError:
                    last_event_age = 0.0
            recent = last_event_age < 5

            # Sesion fantasma: su proceso claude ya no existe (terminal
            # cerrada de golpe sin SessionEnd). Borrar el fichero huerfano
            # y no mostrarla. Solo si el fichero ya no es recentisimo, para
            # dar margen a que el PID se escriba en el primer SessionStart.
            pid = data.get("pid", 0)
            if not recent and not pid_alive(pid):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
                continue

            # Tiempo en el ESTADO actual (desde state_since), para el "hace X".
            # No se reinicia por eventos de fondo que mantienen el estado.
            age = 0.0
            since_raw = data.get("state_since") or ts_raw
            if since_raw:
                try:
                    age = (now - datetime.fromisoformat(since_raw)).total_seconds()
                except ValueError:
                    age = 0.0

            seen.add(sid)
            stale = last_event_age > STALE_SECONDS
            state = "gray" if stale else data.get("state", "gray")
            order = STATE_ORDER.get(state, 9)
            active.append((order, sid, data, stale, age))

        # Ordenar: primero las favoritas, y dentro de cada grupo por urgencia
        # (naranja, rojo, verde, gris) y, a igualdad, la que lleva mas tiempo
        # esperando primero. 'is not fav' -> False(0) ordena antes que True(1).
        active.sort(key=lambda t: (t[1] not in self.favorites, t[0], -t[4]))

        # ¿Cuantas favoritas hay al principio? (active ya viene ordenado con
        # las favoritas delante). El separador ira tras la ultima favorita.
        n_fav = sum(1 for (_o, sid, *_r) in active if sid in self.favorites)
        n_total = len(active)
        show_divider = 0 < n_fav < n_total  # solo si hay favoritas Y no favoritas

        # Sacar el divisor del layout para recolocarlo (o esconderlo).
        self.list_layout.removeWidget(self.divider)

        # Crear/actualizar filas y recolocarlas en el orden calculado.
        pos = 0
        for idx, (order, sid, data, stale, age) in enumerate(active):
            row = self.rows.get(sid)
            if row is None:
                row = SessionRow()
                self.rows[sid] = row
                # Al pulsar la estrella, alternar el favorito de ESTA sesion.
                row.fav_toggled.connect(lambda s=sid: self.toggle_favorite(s))
            row.update_from(data, stale, age, favorite=sid in self.favorites)
            # Reubicar en la posicion correcta (quitar y reinsertar).
            self.list_layout.removeWidget(row)
            self.list_layout.insertWidget(pos, row)
            pos += 1
            # Tras la ultima favorita, colocar el separador.
            if show_divider and idx == n_fav - 1:
                self.list_layout.insertWidget(pos, self.divider)
                pos += 1

        self.divider.setVisible(show_divider)

        # Quitar filas de sesiones cuyo fichero ya no existe.
        for sid in list(self.rows.keys()):
            if sid not in seen:
                row = self.rows.pop(sid)
                row.setParent(None)
                row.deleteLater()

        self.empty.setVisible(not self.rows)

        # Icono de la barra de tareas segun el estado global mas urgente.
        # active = lista de (order, sid, data, stale, age); el estado ya
        # tiene en cuenta 'stale' (que lo convierte en gris).
        states = [
            ("gray" if stale else data.get("state", "gray"))
            for (_o, _sid, data, stale, _age) in active
        ]
        self.apply_overall_icon(overall_state(states))

        # Instantanea para los pilotos del modo Split. Si estamos en esa
        # vista (ventana minimizada), sincronizarlos con las sesiones actuales.
        self._session_snapshot = [
            {
                "sid": sid,
                "repo": data.get("project") or sid[:8],
                "title": data.get("title") or "",
                "state": ("gray" if stale else data.get("state", "gray")),
                "pid": data.get("pid", 0),
                "age": age,
            }
            for (_o, sid, data, stale, age) in active
        ]
        if self._in_split_view:
            self._sync_pilots()

    def toggle_favorite(self, session_id: str) -> None:
        """Marca/desmarca una sesion como favorita y reordena al instante."""
        if session_id in self.favorites:
            self.favorites.discard(session_id)
        else:
            self.favorites.add(session_id)
        self.refresh()

    # ---- Modo Split ----------------------------------------------------

    def _style_split_btn(self) -> None:
        """Estilo del boton segun este activo (verde) o no (apagado)."""
        if self.split_enabled:
            css = (
                "QPushButton { color: #1e272e; background: #2ecc71; border: none;"
                " border-radius: 6px; padding: 4px 12px; font-size: 12px;"
                " font-weight: 600; }"
                "QPushButton:hover { background: #43d67f; }"
            )
            tip = ("Split activo: al minimizar veras una lucecita por sesion "
                   "en la barra de tareas.")
        else:
            css = (
                "QPushButton { color: #9aa5ad; background: #2c3a45; border: none;"
                " border-radius: 6px; padding: 4px 12px; font-size: 12px;"
                " font-weight: 600; }"
                "QPushButton:hover { background: #35454f; color: #ecf0f1; }"
            )
            tip = ("Split: al minimizar, muestra una lucecita por sesion "
                   "en la barra de tareas.")
        self.split_btn.setStyleSheet(css)
        self.split_btn.setToolTip(tip)

    def toggle_split(self) -> None:
        """Activa/desactiva el modo Split y lo guarda."""
        self.split_enabled = self.split_btn.isChecked()
        save_settings({"split": self.split_enabled})
        self._style_split_btn()
        # Si se apaga mientras estabamos en la vista de pilotos, retirarlos.
        if not self.split_enabled and self._in_split_view:
            self._exit_split_view()

    def changeEvent(self, event) -> None:
        # Con Split activo: al minimizar desplegamos los pilotos; al restaurar
        # la ventana principal los retiramos. Diferido con singleShot para no
        # manipular ventanas dentro del propio evento de cambio de estado.
        if event.type() == QEvent.WindowStateChange:
            minimized = bool(self.windowState() & Qt.WindowMinimized)
            if minimized and self.split_enabled and not self._in_split_view:
                QTimer.singleShot(0, self._enter_split_view)
            elif not minimized and self._in_split_view:
                self._exit_split_view()
        super().changeEvent(event)

    def _enter_split_view(self) -> None:
        """Despliega un piloto (punto) por sesion en la barra de tareas.

        No ocultamos la ventana principal: su boton minimizado sigue en la
        barra y sirve para volver a la vista completa. Los puntos se suman.
        """
        if self._in_split_view or not self.split_enabled:
            return
        self._in_split_view = True
        self._sync_pilots()

    def _sync_pilots(self) -> None:
        """Crea/actualiza/elimina pilotos para que coincidan con las sesiones."""
        snapshot = self._session_snapshot
        ids = {info["sid"] for info in snapshot}

        for sid in list(self.pilots):
            if sid not in ids:
                self.pilots.pop(sid).close_silently()

        for info in snapshot:
            pilot = self.pilots.get(info["sid"])
            if pilot is None:
                pilot = PilotWindow(info)
                pilot.clicked.connect(lambda p=pilot: self._on_pilot_clicked(p))
                self.pilots[info["sid"]] = pilot
            else:
                pilot.apply(info)

    def _on_pilot_clicked(self, pilot: "PilotWindow") -> None:
        """Clic en un punto: salta a la terminal de esa sesion y deja el
        punto de nuevo minimizado en la barra de tareas."""
        focus_terminal(pilot.pid, getattr(pilot, "conv_title", ""),
                       getattr(pilot, "repo_name", ""))
        # Windows acaba de restaurar la ventanita (fuera de pantalla);
        # devolverla a la barra de tareas.
        QTimer.singleShot(
            0, lambda: pilot.showMinimized() if not pilot._closing else None)

    def _exit_split_view(self) -> None:
        """Retira todos los pilotos (vuelta a la vista normal)."""
        self._in_split_view = False
        self._destroy_pilots()

    def _destroy_pilots(self) -> None:
        for pilot in self.pilots.values():
            pilot.close_silently()
        self.pilots.clear()

    def apply_overall_icon(self, state: str) -> None:
        """Actualiza el icono de la ventana/barra de tareas si cambio."""
        if getattr(self, "_current_icon_state", None) == state:
            return
        self._current_icon_state = state
        self.setWindowIcon(state_icon(state))

    def bring_to_front(self) -> None:
        """Restaura la ventana (si estaba minimizada) y la trae al frente.

        Se invoca cuando una segunda instancia intenta abrirse: en vez de
        crear otra ventana, reactivamos esta.
        """
        # Si estabamos en la vista de pilotos, cerrarlos antes de restaurar.
        self._in_split_view = False
        self._destroy_pilots()
        # Quitar el flag de minimizada conservando los demas estados.
        self.setWindowState(
            (self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
        )
        self.show()
        self.raise_()
        self.activateWindow()


# Nombre unico del socket local que actua de candado de instancia unica.
SINGLE_INSTANCE_KEY = "blip-single-instance-pparra"


def main() -> None:
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())

    # ¿Ya hay una instancia de Blip corriendo? Intentamos conectar al socket.
    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(300):
        # Hay otra instancia viva: le pedimos que se muestre y salimos.
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return

    # No habia servidor (o quedo huerfano de un cierre sucio). Limpiamos un
    # posible socket residual y creamos el servidor de esta instancia.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)

    win = MainWindow()
    win.show()

    def on_new_connection() -> None:
        # Otra instancia nos pidio mostrarnos.
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(lambda: (conn.readAll(), win.bring_to_front()))
            # Por si el dato ya llego, forzamos igualmente.
            win.bring_to_front()

    server.newConnection.connect(on_new_connection)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
