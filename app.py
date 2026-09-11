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

# Windows Terminal: un solo proceso para TODAS sus ventanas y pestanas.
_WT_NAMES = {"windowsterminal.exe", "wt.exe"}


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


# --- Windows Terminal: seleccion de pestana via UI Automation (opcional) ---
# En Windows Terminal varias pestanas comparten una ventana y su titulo solo
# refleja la pestana ACTIVA; enfocar la ventana no basta para llegar a una
# pestana en segundo plano. Con UI Automation (comtypes) seleccionamos la
# pestana correcta. Si comtypes/UIA no esta disponible (p. ej. en el .exe
# empaquetado), se omite y solo se enfoca la ventana (comportamiento previo).

# Titulo que Claude Code fija en la pestana antes de que la conversacion tenga
# titulo propio: sirve para localizar una sesion aun sin titulo.
_CLAUDE_DEFAULT_TAB = "claude code"

# Cache perezosa del objeto de UI Automation (COM).
_uia_cache: dict = {"tried": False, "uia": None, "mod": None}


def _get_uia():
    """Devuelve (automation, modulo) de UI Automation, o (None, None)."""
    if _uia_cache["tried"]:
        return _uia_cache["uia"], _uia_cache["mod"]
    _uia_cache["tried"] = True
    if sys.platform == "win32":
        try:
            import comtypes.client
            mod = comtypes.client.GetModule("UIAutomationCore.dll")
            _uia_cache["uia"] = comtypes.client.CreateObject(
                mod.CUIAutomation, interface=mod.IUIAutomation)
            _uia_cache["mod"] = mod
        except Exception:
            pass
    return _uia_cache["uia"], _uia_cache["mod"]


def _tab_selected(elem) -> bool:
    """True si esa pestana es la activa de su ventana."""
    _uia, mod = _get_uia()
    try:
        pat = elem.GetCurrentPattern(mod.UIA_SelectionItemPatternId)
        return bool(pat.QueryInterface(
            mod.IUIAutomationSelectionItemPattern).CurrentIsSelected)
    except Exception:
        return False


def _wt_tabs(hwnd) -> list:
    """Pestanas de una ventana de Windows Terminal: [(nombre, elem, activa)].

    Lista vacia si no hay UIA o la ventana no tiene pestanas (consola
    clasica y otras terminales).
    """
    uia, mod = _get_uia()
    if uia is None or not hwnd:
        return []
    try:
        el = uia.ElementFromHandle(hwnd)
        cond = uia.CreatePropertyCondition(
            mod.UIA_ControlTypePropertyId, mod.UIA_TabItemControlTypeId)
        found = el.FindAll(mod.TreeScope_Descendants, cond)
        elems = [found.GetElement(i) for i in range(found.Length)]
    except Exception:
        return []
    tabs = []
    for elem in elems:
        try:
            name = elem.CurrentName or ""
        except Exception:
            continue
        tabs.append((name, elem, _tab_selected(elem)))
    return tabs


def _wt_active_text(hwnd, limit: int = 600) -> str:
    """Texto del panel ACTIVO de una ventana de Windows Terminal.

    UIA solo expone el panel de la pestana activa. Sirve para reconocer de
    que proyecto es una sesion que aun no tiene titulo: su pantalla de
    bienvenida muestra la carpeta de trabajo.
    """
    uia, mod = _get_uia()
    if uia is None:
        return ""
    try:
        el = uia.ElementFromHandle(hwnd)
        cond = uia.CreatePropertyCondition(
            mod.UIA_IsTextPatternAvailablePropertyId, True)
        found = el.FindAll(mod.TreeScope_Descendants, cond)
        for i in range(found.Length):
            e = found.GetElement(i)
            if (e.CurrentClassName or "") != "TermControl":
                continue
            pat = e.GetCurrentPattern(mod.UIA_TextPatternId).QueryInterface(
                mod.IUIAutomationTextPattern)
            return pat.DocumentRange.GetText(limit) or ""
    except Exception:
        pass
    return ""


def _pick_wt_tab(hwnds: list, title: str, repo: str):
    """Localiza la pestana de una sesion entre TODAS las ventanas de WT.

    Devuelve (hwnd, elem_pestana) o None. El orden de pistas es:
      1) El titulo de la conversacion contra el nombre de cada pestana de
         cada ventana (tambien las que estan en segundo plano, que no
         aparecen en el titulo de la ventana).
      2) Sin titulo aun, la pestana se llama "Claude Code": si hay una
         sola en todo el escritorio es esa. Si hay varias, se desempata
         con el panel activo de cada ventana (lo unico que UIA deja leer),
         que muestra la carpeta de trabajo: vale para reconocer la nuestra
         o para descartar las que son de otra sesion.

    Si no se puede identificar con seguridad devuelve None: es mejor no
    hacer nada que cambiar de pestana en una sesion ajena.
    """
    cands = [(hwnd, name, elem, sel)
             for hwnd in hwnds for (name, elem, sel) in _wt_tabs(hwnd)]
    if not cands:
        return None

    if _norm_title(title):
        best, best_score = None, 0
        for hwnd, name, elem, _sel in cands:
            score = _title_score(name, title)
            if score > best_score:
                best_score, best = score, (hwnd, elem)
        if best is not None:
            return best

    defaults = [(hwnd, elem, sel) for hwnd, name, elem, sel in cands
                if _norm_title(name) == _CLAUDE_DEFAULT_TAB]
    if len(defaults) == 1:
        hwnd, elem, _sel = defaults[0]
        return hwnd, elem
    if len(defaults) > 1 and repo:
        needle = repo.lower()
        texts: dict = {}

        def active_text(hwnd) -> str:
            if hwnd not in texts:
                texts[hwnd] = _wt_active_text(hwnd).lower()
            return texts[hwnd]

        hits = [(hwnd, elem) for hwnd, elem, sel in defaults
                if sel and needle in active_text(hwnd)]
        if len(hits) == 1:
            return hits[0]
        # Descarte: una pestana activa que no habla de nuestro proyecto es
        # de otra sesion. Si al quitarlas queda una sola, esa es la nuestra
        # (tipico de una sesion recien abierta en una pestana de fondo, cuyo
        # panel no se puede leer).
        rest = [(hwnd, elem) for hwnd, elem, sel in defaults
                if not sel or needle in active_text(hwnd)]
        if len(rest) == 1:
            return rest[0]
    return None


def _select_tab(elem) -> bool:
    """Activa una pestana (la trae al frente dentro de su ventana)."""
    _uia, mod = _get_uia()
    try:
        pat = elem.GetCurrentPattern(mod.UIA_SelectionItemPatternId)
        pat.QueryInterface(mod.IUIAutomationSelectionItemPattern).Select()
        return True
    except Exception:
        return False


def focus_terminal(pid: int, title: str = "", repo: str = "") -> bool:
    """Trae al frente la ventana de la terminal de una sesion de Claude Code.

    Universal por terminal, prueba en orden:
      1) Windows Terminal: la PESTANA exacta de la sesion (via UIA), en
         cualquiera de sus ventanas, incluso si esta en segundo plano.
      2) Por TITULO de ventana (sin UIA disponible): la ventana cuyo
         titulo coincide con el de la conversacion.
      3) Consola clasica (conhost): AttachConsole(pid) + GetConsoleWindow.
      4) Por proceso: ventana de un proceso emparentado que parece terminal.
      5) Ultimo recurso: una unica ventana de Windows Terminal visible.

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

    wt_hwnds = [h for (h, _p, name, _t) in windows if name in _WT_NAMES]

    # --- 1) Windows Terminal: la pestana exacta de la sesion (UIA) -------
    picked = _pick_wt_tab(wt_hwnds, title, repo)
    if picked is not None:
        hwnd, tab = picked
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, SW_RESTORE)
        _select_tab(tab)
        return focus(hwnd)

    # --- 2) Por titulo de ventana (sin UIA solo alcanza pestanas activas)
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

    # --- 3) Consola clasica via AttachConsole ----------------------------
    k32.FreeConsole()  # soltar nuestra consola (si la hay) antes de unirnos
    console_hwnd = 0
    if k32.AttachConsole(pid):
        try:
            console_hwnd = k32.GetConsoleWindow()
        finally:
            k32.FreeConsole()
    if console_hwnd and u32.IsWindowVisible(console_hwnd):
        return focus(console_hwnd)

    # --- 4) Por proceso emparentado --------------------------------------
    # Ojo: todas las ventanas de Windows Terminal comparten un mismo
    # proceso, asi que si hay varias el PID no dice cual es la de esta
    # sesion; se dejan fuera para no saltar a una ventana ajena.
    related = _related_pids(pid)
    ambiguous_wt = len(wt_hwnds) > 1
    strong = weak = None
    for hwnd, wpid, name, _wtitle in windows:
        if wpid == own or (ambiguous_wt and name in _WT_NAMES):
            continue
        if wpid in related and name in _TERMINAL_NAMES:
            strong = hwnd
            break
        if weak is None and wpid in related and name != "explorer.exe":
            weak = hwnd
    target = strong or weak

    # --- 5) Ultimo recurso: una unica ventana de Windows Terminal --------
    if target is None and len(wt_hwnds) == 1:
        target = wt_hwnds[0]

    if target:
        return focus(target)
    return False


from PySide6.QtCore import Qt, QTimer, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QFrame,
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
            # Consumir el evento: no debe propagarse a la fila (que abriria la
            # terminal). La estrella solo alterna la favorita.
            event.accept()
            return
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
    activated = Signal()  # clic simple: ir a la terminal de esta sesion

    def __init__(self):
        super().__init__()
        self.setObjectName("row")
        # Seguir el raton para mostrar la estrella al hacer hover.
        self.setAttribute(Qt.WA_Hover, True)
        # Toda la fila es clicable (clic = terminal, doble clic = favorita).
        self.setCursor(Qt.PointingHandCursor)

        # Datos de la sesion para localizar su terminal al hacer clic.
        self._pid = 0
        self._title = ""
        self._repo = ""
        # Distinguir clic simple de doble: al soltar arrancamos un temporizador
        # con el intervalo de doble clic del sistema; si llega un doble clic lo
        # cancelamos. Si expira, fue un clic simple de verdad -> abrir terminal.
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.activated)
        self._suppress_next_release = False

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

    def mouseReleaseEvent(self, event) -> None:
        # Candidato a clic simple: esperar el intervalo de doble clic antes de
        # dar la accion por buena, por si es la primera mitad de un doble clic.
        if event.button() == Qt.LeftButton:
            if self._suppress_next_release:
                # Este release es la segunda mitad de un doble clic ya tratado.
                self._suppress_next_release = False
            else:
                self._click_timer.start(QApplication.doubleClickInterval())
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # Doble clic en cualquier parte de la fila -> marcar/desmarcar favorita.
        if event.button() == Qt.LeftButton:
            # Cancelar el clic simple pendiente y no abrir la terminal.
            self._click_timer.stop()
            self._suppress_next_release = True
            self.fav_toggled.emit()
        super().mouseDoubleClickEvent(event)

    def update_from(self, data: dict, stale: bool, age_seconds: float,
                    favorite: bool = False) -> None:
        state = data.get("state", "gray")
        if stale:
            state = "gray"
        try:
            self._pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            self._pid = 0
        self.dot.set_color(COLORS.get(state, COLORS["gray"]))
        self.star.set_favorite(favorite)

        repo = data.get("project") or data.get("session_id", "?")[:8]
        self.repo.setText(repo)
        title = data.get("title", "") or ""
        # Pistas para localizar la terminal al hacer clic (la terminal titula
        # la pestana/ventana con el titulo de la conversacion).
        self._repo = repo
        self._title = title
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


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blip")
        self.setWindowIcon(app_icon())
        # Always-on-top; ventana normal (se puede minimizar).
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.resize(340, 400)
        self.setStyleSheet("background: #1e272e;")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

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
                # Clic simple en la fila -> saltar a la terminal de la sesion.
                row.activated.connect(lambda s=sid: self.focus_session(s))
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

    def focus_session(self, session_id: str) -> None:
        """Clic en una fila: trae al frente la terminal (y pestana) de la sesion."""
        row = self.rows.get(session_id)
        if row is not None:
            focus_terminal(row._pid, row._title, row._repo)

    def toggle_favorite(self, session_id: str) -> None:
        """Marca/desmarca una sesion como favorita y reordena al instante."""
        if session_id in self.favorites:
            self.favorites.discard(session_id)
        else:
            self.favorites.add(session_id)
        self.refresh()

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
