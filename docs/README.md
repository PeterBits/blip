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
5. Publica.

El botón "Descargar" de la web apunta a
`https://github.com/PeterBits/blip/releases/latest`, así que siempre
enlaza a la última versión publicada sin tocar el HTML.
