# Blip

Monitor de escritorio para las sesiones de **Claude Code** abiertas en tus
terminales. Muestra un semáforo por sesión y te avisa cuando alguna requiere
tu actuación.

- 🟢 **verde** — trabajando
- 🟠 **naranja** — te necesita (pregunta, plan por aprobar o permiso)
- 🔴 **rojo** — terminó, espera más prompts

Cada fila muestra el repositorio, el título de la conversación y, cuando la
sesión te espera, cuánto tiempo lleva. La ventana está siempre encima del
resto. Las sesiones cuya terminal se cierra desaparecen solas.

## Cómo funciona

1. Un **hook** de Claude Code (`hooks/report_state.py`) se dispara en cada
   evento de la sesión (inicio, uso de herramienta, notificación, fin...) y
   escribe el estado en `~/.claude/blip/<session_id>.json`.
2. La **app** (`app.py`, PySide6) vigila esa carpeta y pinta el semáforo.

## Instalación

```bash
pip install -r requirements.txt
```

Configura los hooks en tu `~/.claude/settings.json` (global) para que apunten
al script `hooks/report_state.py`. Eventos usados: `SessionStart`,
`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
`SubagentStop`, `SessionEnd`.

Ejemplo de entrada de hook:

```json
{
  "hooks": {
    "Stop": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python \"D:\\DEV\\blip\\hooks\\report_state.py\"", "async": true }
      ]}
    ]
  }
}
```

## Uso

```bash
python app.py
```

(Usa `pythonw app.py` para lanzarla sin ventana de consola.)
