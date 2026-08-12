"""
Hook de Claude Code: reporta el estado de la sesion a Blip.

Se invoca desde ~/.claude/settings.json en los eventos:
  SessionStart / UserPromptSubmit / PreToolUse / Notification / Stop / SessionEnd

Lee el JSON del evento por stdin y escribe un fichero de estado en:
  ~/.claude/blip/<session_id>.json

La app de escritorio vigila esa carpeta y pinta el semaforo:
  green  -> trabajando
  yellow -> requiere tu atencion (te hizo una pregunta / pide permiso)
  red    -> termino el turno, espera mas prompts

Logica clave del amarillo:
  Como con bypassPermissions casi nunca llega el evento Notification, el
  amarillo se decide en el evento Stop: si el ultimo mensaje del asistente
  en el transcript termina en una pregunta, el estado es yellow ("te
  necesita") en vez de red ("terminado").

No imprime nada por stdout ni devuelve codigos que bloqueen a Claude Code:
pase lo que pase, termina con exit 0.
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime, timezone

# Mapeo base de evento -> estado. Stop se refina mas abajo (rojo o amarillo).
EVENT_TO_STATE = {
    "SessionStart": "green",
    "UserPromptSubmit": "green",
    "PreToolUse": "green",
    "PostToolUse": "green",
    "Notification": "yellow",
    "Stop": "red",
    "SubagentStop": "green",  # un subagente termino, pero la sesion sigue trabajando
}

STATE_DIR = Path.home() / ".claude" / "blip"

# Herramientas que, al iniciarse, significan "Claude espera que el usuario
# decida algo" (menus de opciones, elicitaciones). Cuando PreToolUse las
# lanza -> naranja; cuando PostToolUse termina -> el usuario ya respondio.
# Cubre tanto este chat (AskUserQuestion) como equivalentes.
WAITING_TOOLS = {
    "AskUserQuestion",
    "Elicitation",
    "ExitPlanMode",  # espera aprobacion del plan por el usuario
}


def last_assistant_text(transcript_path: str) -> str:
    """Devuelve el texto del ultimo mensaje 'assistant' del transcript JSONL.

    Solo concatena los bloques de tipo 'text' del ultimo turno del asistente.
    Devuelve "" si no se puede leer.
    """
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""

    last_text = ""
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                texts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t).strip()
                # Solo actualizamos si este turno tuvo texto (los turnos que
                # solo ejecutan tools no deben pisar el ultimo texto real).
                if joined:
                    last_text = joined
    except OSError:
        return ""
    return last_text


def conversation_title(transcript_path: str) -> str:
    """Devuelve el titulo de la conversacion desde el transcript JSONL.

    Prioriza 'customTitle' (el que fija/ve el usuario) y cae a 'aiTitle'
    (titulo automatico). Devuelve "" si no hay ninguno.
    """
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    custom = ai = ""
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("customTitle"):
                    custom = obj["customTitle"]
                if obj.get("aiTitle"):
                    ai = obj["aiTitle"]
    except OSError:
        return ""
    return custom or ai


def looks_like_question(text: str) -> bool:
    """¿El mensaje TERMINA con una pregunta directa dirigida al usuario?

    Heuristica ESTRICTA para evitar falsos positivos (preguntas retoricas o
    cierres corteses tipo "¿Seguimos?"). Requiere que la ULTIMA linea no
    vacia sea, en si misma, una pregunta corta que termina en '?'.

    Ante la duda devolvemos False: el estado por defecto de un Stop es rojo
    ("terminado, te toca"), que ya indica que la sesion requiere tu accion.
    Solo elevamos a naranja cuando la senal de pregunta es inequivoca.
    """
    if not text:
        return False

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False

    last = lines[-1]

    # Quitar formato markdown de cierre habitual (negritas, comillas).
    stripped = last.strip("*_`\"' ").rstrip()

    # Debe terminar en signo de interrogacion.
    if not stripped.endswith("?"):
        return False

    # Debe ser una linea razonablemente corta (una pregunta real, no un
    # parrafo largo que casualmente acaba en '?'). ~140 chars margen amplio.
    if len(stripped) > 140:
        return False

    # Evitar lineas que empiezan como enumeracion/encabezado (no son la
    # pregunta final al usuario, sino contenido).
    if stripped.startswith(("#", "-", "*", ">", "|")):
        return False

    return True


# Eventos que reflejan actividad real disparada por el usuario y que SI
# pueden sacar a la sesion de un estado "esperando" (amarillo/rojo) hacia
# verde. El resto de eventos "verdes" se consideran ruido de fondo y no
# deben pisar un amarillo/rojo legitimo.
USER_DRIVEN_GREEN = {"UserPromptSubmit"}

# Estados que significan "la sesion te esta esperando".
WAITING_STATES = {"yellow", "red"}


def read_previous_payload(session_id: str) -> dict:
    """Devuelve el payload anterior guardado para esta sesion, o {}."""
    target = STATE_DIR / f"{session_id}.json"
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def find_claude_pid() -> int:
    """Sube por la cadena de procesos padre hasta encontrar el proceso
    'claude' (claude.exe) que invoco este hook. Devuelve su PID, o 0.

    El hook corre como descendiente del proceso claude de la sesion, asi
    que su PID identifica la terminal viva. Usa WMI via 'wmic'/PowerShell
    solo si hace falta; primero intenta psutil (rapido) si esta instalado.
    """
    try:
        import psutil  # type: ignore
        p = psutil.Process(os.getpid())
        for _ in range(12):  # subir varios niveles por si hay shells intermedios
            p = p.parent()
            if p is None:
                break
            try:
                name = (p.name() or "").lower()
            except Exception:
                continue
            if name in ("claude.exe", "claude"):
                return p.pid
        return 0
    except Exception:
        return 0


def decide_state(event_name: str, event: dict, prev_state: str) -> str:
    tool_name = event.get("tool_name", "")

    if event_name == "Stop":
        text = last_assistant_text(event.get("transcript_path", ""))
        return "yellow" if looks_like_question(text) else "red"

    # Herramienta de espera al usuario (menu de opciones, plan por aprobar):
    #   - al iniciarse (PreToolUse)  -> naranja: "te esta preguntando"
    #   - al terminar  (PostToolUse) -> verde:   ya respondiste, sigue
    if tool_name in WAITING_TOOLS:
        if event_name == "PreToolUse":
            return "yellow"
        if event_name == "PostToolUse":
            return "green"

    proposed = EVENT_TO_STATE.get(event_name, "green")

    # Regla de prioridad: si la sesion esta esperando (amarillo/rojo), solo
    # un evento genuino del usuario puede devolverla a verde. Asi un
    # SubagentStop, SessionStart o cualquier evento de fondo NO pisa el
    # amarillo/rojo que indica "requiere tu actuacion".
    # Excepcion: PostToolUse de un WAITING_TOOL SI vuelve a verde (ya
    # tratado arriba), porque significa que el usuario acaba de responder.
    if proposed == "green" and prev_state in WAITING_STATES:
        if event_name not in USER_DRIVEN_GREEN:
            return prev_state

    return proposed


def main() -> None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}

    event_name = event.get("hook_event_name", "")
    session_id = event.get("session_id", "unknown")
    cwd = event.get("cwd", "")

    # SessionEnd: eliminar el fichero de estado (la sesion desaparece).
    if event_name == "SessionEnd":
        target = STATE_DIR / f"{session_id}.json"
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return

    prev = read_previous_payload(session_id)
    prev_state = prev.get("state", "")
    state = decide_state(event_name, event, prev_state)
    project = Path(cwd).name if cwd else session_id[:8]

    # PID de la terminal claude viva. Se calcula en SessionStart y se
    # arrastra en los eventos siguientes (o se recalcula si falta).
    pid = prev.get("pid", 0)
    if event_name == "SessionStart" or not pid:
        found = find_claude_pid()
        if found:
            pid = found

    # Titulo de la conversacion. El titulo aparece en el transcript algo
    # despues de arrancar, asi que lo refrescamos en cada UserPromptSubmit
    # y Stop (eventos poco frecuentes) y lo arrastramos en el resto.
    title = prev.get("title", "")
    transcript = event.get("transcript_path", "")
    if event_name in ("UserPromptSubmit", "Stop", "SessionStart") or not title:
        found_title = conversation_title(transcript)
        if found_title:
            title = found_title

    now_iso = datetime.now(timezone.utc).isoformat()

    # Momento en que la sesion entro en su estado ACTUAL. Solo se refresca
    # cuando el estado CAMBIA respecto al anterior; si un evento de fondo
    # mantiene el mismo estado (p.ej. SubagentStop tras Stop, ambos rojo),
    # conservamos el instante original. Asi el "hace X" no se reinicia.
    if state != prev_state:
        state_since = now_iso
    else:
        state_since = prev.get("state_since", now_iso)

    payload = {
        "session_id": session_id,
        "project": project,
        "title": title,
        "cwd": cwd,
        "state": state,
        "event": event_name,
        "pid": pid,
        "updated_at": now_iso,
        "state_since": state_since,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    target = STATE_DIR / f"{session_id}.json"
    # Escritura atomica.
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
