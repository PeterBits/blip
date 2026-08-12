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

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
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


class SessionRow(QWidget):
    """Una fila: luz + (repo / titulo de conversacion) + estado + tiempo."""

    def __init__(self):
        super().__init__()
        self.setObjectName("row")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.dot = LightDot()

        # Bloque de texto: repo en negrita arriba, titulo debajo.
        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        text_box.setContentsMargins(0, 0, 0, 0)
        self.repo = QLabel("-")
        self.repo.setStyleSheet("color: #ecf0f1; font-size: 13px; font-weight: 600;")
        self.title = QLabel("")
        self.title.setStyleSheet("color: #9aa5ad; font-size: 11px;")
        text_box.addWidget(self.repo)
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

    def update_from(self, data: dict, stale: bool, age_seconds: float) -> None:
        state = data.get("state", "gray")
        if stale:
            state = "gray"
        self.dot.set_color(COLORS.get(state, COLORS["gray"]))

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

        header = QLabel("  Sesiones de Claude Code")
        header.setStyleSheet(
            "color: #dfe6e9; font-size: 13px; font-weight: 700;"
            "padding: 10px; background: #2d3436;"
        )
        root.addWidget(header)

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

        # Ordenar por urgencia (naranja, rojo, verde, gris) y, dentro de
        # cada grupo, la que lleva mas tiempo esperando primero.
        active.sort(key=lambda t: (t[0], -t[4]))

        # Crear/actualizar filas y recolocarlas en el orden calculado.
        for position, (order, sid, data, stale, age) in enumerate(active):
            row = self.rows.get(sid)
            if row is None:
                row = SessionRow()
                self.rows[sid] = row
            row.update_from(data, stale, age)
            # Reubicar en la posicion correcta (quitar y reinsertar).
            self.list_layout.removeWidget(row)
            self.list_layout.insertWidget(position, row)

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
