# Web de Blip (GitHub Pages)

Landing estática para promocionar Blip y enlazar la descarga del ejecutable.

- `index.html` — la página (HTML/CSS, sin build ni dependencias).
- `assets/` — captura de la app y favicon.

## Publicar la web (una vez)

1. Sube el repo a GitHub (`git push`).
2. En GitHub: **Settings → Pages**.
3. En "Build and deployment", Source = **Deploy from a branch**.
4. Branch = **main**, carpeta = **/docs**. Guardar.
5. En un par de minutos la web estará en:
   `https://peterbits.github.io/blip/`

## Publicar el ejecutable (en cada versión)

El `.exe` NO se versiona; se distribuye como **GitHub Release**:

1. Genera el ejecutable con `build.bat` (queda en `dist/Blip.exe`).
2. En GitHub: **Releases → Draft a new release**.
3. Crea un tag (p. ej. `v1.0.0`), pon un título y descripción.
4. Arrastra `dist/Blip.exe` a la zona de "Attach binaries".
   **El fichero debe llamarse exactamente `Blip.exe`** (así lo genera
   `build.bat`); si no, el botón de descarga directa no lo encontrará.
5. Publica.

Los botones "Descargar" de la web apuntan a
`https://github.com/PeterBits/blip/releases/latest/download/Blip.exe`.
Esa URL descarga el `Blip.exe` del último Release **directamente**, sin
pasar por la página de releases, y siempre sirve la última versión sin
tocar el HTML.
