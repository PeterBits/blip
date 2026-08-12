"""
Genera el icono de Blip: un circulo verde (el color "trabajando") sobre
fondo transparente. Produce assets/blip.ico (multi-resolucion) y
assets/blip.png. Ejecuta este script solo si quieres regenerar el icono.
"""

from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication, QPixmap, QPainter, QColor

GREEN = QColor("#2ecc71")
ASSETS = Path(__file__).parent / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(GREEN)
    p.setPen(Qt.NoPen)
    # Margen del 12% para que el circulo respire dentro del icono.
    m = size * 0.12
    p.drawEllipse(QRectF(m, m, size - 2 * m, size - 2 * m))
    p.end()
    return pm


def main() -> None:
    app = QGuiApplication([])  # necesario para usar QPixmap/QPainter
    ASSETS.mkdir(exist_ok=True)

    # PNG grande (por si se necesita en otro sitio).
    render(256).save(str(ASSETS / "blip.png"), "PNG")

    # ICO multi-resolucion: guardamos todos los tamanos en un solo .ico.
    # Qt no escribe .ico multi-size directamente, asi que usamos Pillow si
    # esta disponible; si no, guardamos un PNG y avisamos.
    try:
        from PIL import Image
        import io

        images = []
        for s in SIZES:
            pm = render(s)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            pm.save(buf, "PNG")
            buf.close()
            images.append(Image.open(io.BytesIO(bytes(ba))).convert("RGBA"))

        images[0].save(
            str(ASSETS / "blip.ico"),
            format="ICO",
            sizes=[(s, s) for s in SIZES],
            append_images=images[1:],
        )
        print("Generado:", ASSETS / "blip.ico")
    except ImportError:
        print("Pillow no instalado: solo se genero blip.png.")
        print("Instala con: pip install pillow  y vuelve a ejecutar.")

    print("Generado:", ASSETS / "blip.png")


if __name__ == "__main__":
    main()
