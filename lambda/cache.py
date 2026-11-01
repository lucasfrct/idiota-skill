"""Cache / memória local em JSON para a skill Idiota.

Armazenamento: arquivos JSON em CACHE_DIR (default /tmp/idiota-cache — o único
diretório gravável em uma Lambda AWS). Memória local = cache do container
quente: sobrevive a invocações enquanto a instância vive; NÃO é armazenamento
durável (cold start/reciclagem perde; containers concorrentes não compartilham).
Persistência durável exigiria S3/DynamoDB.

Mecânica:
- Escrita atômica (arquivo temporário + os.replace) — JSON nunca corrompido.
- Espelho em memória validado por mtime — leitura sem I/O após a 1ª carga,
  e edições externas no arquivo são detectadas e recarregadas.
- Índice por destinatário (dict) para busca O(1) + varredura em memória para
  texto — reconstruído automaticamente a cada escrita/recarga.
- TTL: entradas com mais de CACHE_TTL_DAYS dias (default 7) expiram — filtro
  na leitura (nada expirado aparece) e purga na escrita (arquivo enxuto).
  CACHE_TTL_DAYS=0 desativa. Timestamp ilegível nunca expira.
- Fail-open: nenhuma função lança exceção — cache é auxiliar e nunca derruba
  a skill; em falha retorna valor seguro (default / False / lista vazia).

Coleções:
- chats.json      — memória de turnos por usuário
                   {user_id: {"history", "stamps", "updated"}}; history são
                   mensagens {"role", "content"(, "at")}; stamps é o array
                   paralelo de timestamps (um por turno) para busca por data.
- emails.json     — {"registry" (e-mails disponíveis), "sent" (enviados),
                    "errors" (falhas), "seq"} com metadados (para, assunto,
                    mensagem, data, resultado).
- dictations.json — rascunho de ditado longo por usuário
                    {user_id: {"chunks", "updated"}} — anotações acumuladas
                    entre turnos/sessões até "pronto" sintetizar e enviar.
- notes.json      — notas explícitas ("anote que ...") por usuário
                    {user_id: {"notes": [{"text", "at"}], "updated"}}.
"""
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Configuration (environment-driven)
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/idiota-cache")
MAX_CHAT_TURNS = int(os.getenv("CACHE_MAX_CHAT_TURNS", "300"))
MAX_USERS = int(os.getenv("CACHE_MAX_USERS", "50"))
MAX_SENT = int(os.getenv("CACHE_MAX_SENT", "500"))
MAX_ERRORS = int(os.getenv("CACHE_MAX_ERRORS", "200"))
MAX_EMAILS = int(os.getenv("CACHE_MAX_EMAILS", "200"))
MAX_DICTATION_CHUNKS = int(os.getenv("CACHE_MAX_DICTATION_CHUNKS", "200"))
MAX_NOTES = int(os.getenv("CACHE_MAX_NOTES", "500"))
BODY_MAX = int(os.getenv("CACHE_BODY_MAX", "2000"))
TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "7"))  # 0 desativa a expiração

CHATS = "chats"
EMAILS = "emails"
DICTATIONS = "dictations"
NOTES = "notes"

_lock = threading.RLock()
_mirror = {}   # name -> {"mtime_ns": int, "data": ...}
_gen = {}      # name -> contador de geração (invalida índices)
_email_index = {"gen": -1, "sent": {}, "errors": {}}


def _now() -> str:
    # microssegundos: ordenação correta entre registros do mesmo segundo
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _cutoff() -> str:
    """ISO cutoff: tudo anterior expirou (TTL_DAYS; <= 0 desativa)."""
    if TTL_DAYS <= 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)).isoformat(
        timespec="microseconds")


def _expired(ts, cutoff: str = None) -> bool:
    """True se ts < cutoff. Timestamp ilegível NUNCA expira (não destruir dado)."""
    if cutoff is None:
        cutoff = _cutoff()
    if not cutoff or not ts:
        return False
    try:
        return datetime.fromisoformat(str(ts)) < datetime.fromisoformat(cutoff)
    except (TypeError, ValueError):
        return False


def _path(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)):
        raise ValueError(f"Invalid cache name: {name!r}")
    return os.path.join(CACHE_DIR, f"{name}.json")


def _reset_index(name: str):
    if name == EMAILS:
        _email_index["gen"] = -1


def _load(name: str, default=None):
    """Load JSON under the in-memory mirror (mtime-checked). Never raises."""
    try:
        path = _path(name)
        mtime = os.stat(path).st_mtime_ns
    except (ValueError, OSError):
        return default
    cached = _mirror.get(name)
    if cached and cached["mtime_ns"] == mtime:
        return cached["data"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _mirror[name] = {"mtime_ns": mtime, "data": data}
        _gen[name] = _gen.get(name, 0) + 1
        _reset_index(name)
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.error("cache: failed to load %s: %s", name, e)
        return default


def read_json(name: str, default=None):
    """Read a JSON collection (mirror-backed)."""
    with _lock:
        return _load(name, default)


def write_json(name: str, data) -> bool:
    """Atomically write a JSON collection (tmp file + os.replace)."""
    try:
        path = _path(name)
        os.makedirs(CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            os.unlink(tmp)
            raise
        os.replace(tmp, path)
        mtime = os.stat(path).st_mtime_ns
        with _lock:
            _mirror[name] = {"mtime_ns": mtime, "data": data}
            _gen[name] = _gen.get(name, 0) + 1
            _reset_index(name)
        return True
    except (OSError, ValueError, TypeError) as e:
        logger.error("cache: failed to write %s: %s", name, e)
        return False


def update_json(name: str, fn, default=None):
    """Read-modify-write under one lock. `fn(data) -> new_data`;
    returning None from fn cancels the write."""
    with _lock:
        data = _load(name, default)
        new = fn(data)
        if new is None:
            return False
        return write_json(name, new)


# ---------------------------------------------------------------------------
# Frente 1 — memória de turnos do chat (por usuário; sobrevive a sessões)
# ---------------------------------------------------------------------------

def load_chat(user_id: str) -> list:
    """Chat history previously persisted for this user ([] if none/expired)."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        data = read_json(CHATS, default={}) or {}
        entry = data.get(user_id)
        if not entry or _expired(entry.get("updated")):
            return []
        return list(entry.get("history", []))
    except Exception as e:
        logger.error("cache: load_chat failed: %s", e)
        return []


def save_chat(user_id: str, history: list) -> bool:
    """Persist chat history for a user (bounded; evicts oldest/expired users)."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        turns = list(history or [])[-max(1, MAX_CHAT_TURNS):]
        # Stamp por turno: usa o "at" da própria mensagem (estável entre saves);
        # turnos antigos sem stamp ficam "" (busca por data os ignora, sem apagar).

        def fn(data):
            data = dict(data or {})
            cutoff = _cutoff()
            if cutoff:  # purge de usuários expirados (o atual entra fresco já já)
                data = {k: v for k, v in data.items()
                        if k == user_id or not _expired((v or {}).get("updated"), cutoff)}
            now = _now()
            stamps = [str((m or {}).get("at", "") if isinstance(m, dict) else "") for m in turns]
            data[user_id] = {"history": turns, "stamps": stamps, "updated": now}
            if len(data) > max(1, MAX_USERS):
                oldest = sorted(
                    (k for k in data if k != user_id),
                    key=lambda k: data[k].get("updated", ""),
                )
                for k in oldest[: len(data) - max(1, MAX_USERS)]:
                    data.pop(k, None)
            return data

        return update_json(CHATS, fn, default={})
    except Exception as e:
        logger.error("cache: save_chat failed: %s", e)
        return False


def clear_chat(user_id: str) -> bool:
    """Drop a user's persisted chat memory."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False

        def fn(data):
            data = dict(data or {})
            if user_id not in data:
                return None
            data.pop(user_id, None)
            return data

        return update_json(CHATS, fn, default={})
    except Exception as e:
        logger.error("cache: clear_chat failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Frente 4 — busca em turnos antigos + notas explícitas (memória consultável)
# ---------------------------------------------------------------------------

def search_turns(user_id: str, contains=None, since=None, until=None, limit=100) -> list:
    """Busca turnos persistidos por conteúdo e/ou janela de data.

    Retorna [{"role", "content", "at"}], mais recentes primeiro. Turnos sem
    timestamp legível são consultáveis por conteúdo (nunca excluídos por data).
    """
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        with _lock:
            data = read_json(CHATS, default={}) or {}
            entry = data.get(user_id) or {}
            history = list(entry.get("history", []))
            stamps = list(entry.get("stamps", []))
        turns = []
        for i, m in enumerate(history):
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            role = m.get("role", "user") if isinstance(m, dict) else "user"
            at = str((m or {}).get("at", "") if isinstance(m, dict) else "")
            if not at and i < len(stamps):
                at = str(stamps[i] or "")
            turns.append({"role": role, "content": content, "at": at})
        cutoff = _cutoff()
        if cutoff:
            turns = [t for t in turns if not t["at"] or not _expired(t["at"], cutoff)]
        if since:
            turns = [t for t in turns if t["at"] and t["at"] >= since]
        if until:
            turns = [t for t in turns if t["at"] and t["at"] < until]
        if contains:
            needle = str(contains).strip().lower()
            turns = [t for t in turns if needle in t["content"].lower()]
        return list(reversed(turns[-max(1, int(limit)):]))  # mais recentes primeiro
    except Exception as e:
        logger.error("cache: search_turns failed: %s", e)
        return []


def save_note(user_id: str, text: str) -> bool:
    """Persiste uma nota explícita do usuário ("anote que ...")."""
    try:
        user_id = str(user_id or "").strip()
        text = str(text or "").strip()
        if not user_id or not text:
            return False

        def fn(data):
            data = dict(data or {})
            cutoff = _cutoff()
            if cutoff:
                data = {k: v for k, v in data.items()
                        if k == user_id or not _expired((v or {}).get("updated"), cutoff)}
            now = _now()
            entry = data.get(user_id) or {"notes": [], "updated": ""}
            notes = list(entry.get("notes", []))
            notes.append({"text": text[:2000], "at": now})
            data[user_id] = {"notes": notes[-max(1, MAX_NOTES):], "updated": now}
            return data

        return update_json(NOTES, fn, default={})
    except Exception as e:
        logger.error("cache: save_note failed: %s", e)
        return False


def search_notes(user_id: str, contains=None, since=None, until=None, limit=50) -> list:
    """Busca notas do usuário por conteúdo e/ou janela de data (recentes 1º)."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        with _lock:
            data = read_json(NOTES, default={}) or {}
            notes = list((data.get(user_id) or {}).get("notes", []))
        cutoff = _cutoff()
        if cutoff:
            notes = [n for n in notes if not _expired(n.get("at"), cutoff)]
        if since:
            notes = [n for n in notes if str(n.get("at", "")) >= since]
        if until:
            notes = [n for n in notes if str(n.get("at", "")) < until]
        if contains:
            needle = str(contains).strip().lower()
            notes = [n for n in notes if needle in str(n.get("text", "")).lower()]
        return [dict(n) for n in reversed(notes[-max(1, int(limit)):])]
    except Exception as e:
        logger.error("cache: search_notes failed: %s", e)
        return []


def clear_notes(user_id: str) -> bool:
    """Drop all notes for a user."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False

        def fn(data):
            data = dict(data or {})
            if user_id not in data:
                return None
            data.pop(user_id, None)
            return data

        return update_json(NOTES, fn, default={})
    except Exception as e:
        logger.error("cache: clear_notes failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Frente 3 — ditado longo (rascunho multi-sessão por usuário)
# ---------------------------------------------------------------------------

def load_dictation(user_id: str) -> list:
    """Ditado acumulado do usuário ([] se não houver/expirado)."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        data = read_json(DICTATIONS, default={}) or {}
        entry = data.get(user_id)
        if not entry or _expired(entry.get("updated")):
            return []
        return list(entry.get("chunks", []))
    except Exception as e:
        logger.error("cache: load_dictation failed: %s", e)
        return []


def save_dictation(user_id: str, chunks: list) -> bool:
    """Persist the dictation buffer for a user (bounded; evicts oldest/expired)."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        kept = [str(c) for c in (chunks or [])][-max(1, MAX_DICTATION_CHUNKS):]

        def fn(data):
            data = dict(data or {})
            cutoff = _cutoff()
            if cutoff:
                data = {k: v for k, v in data.items()
                        if k == user_id or not _expired((v or {}).get("updated"), cutoff)}
            data[user_id] = {"chunks": kept, "updated": _now()}
            if len(data) > max(1, MAX_USERS):
                oldest = sorted(
                    (k for k in data if k != user_id),
                    key=lambda k: data[k].get("updated", ""),
                )
                for k in oldest[: len(data) - max(1, MAX_USERS)]:
                    data.pop(k, None)
            return data

        return update_json(DICTATIONS, fn, default={})
    except Exception as e:
        logger.error("cache: save_dictation failed: %s", e)
        return False


def clear_dictation(user_id: str) -> bool:
    """Drop a user's dictation buffer."""
    try:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False

        def fn(data):
            data = dict(data or {})
            if user_id not in data:
                return None
            data.pop(user_id, None)
            return data

        return update_json(DICTATIONS, fn, default={})
    except Exception as e:
        logger.error("cache: clear_dictation failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Frente 2 — e-mails: disponíveis (registry), enviados e com erro
# ---------------------------------------------------------------------------

def _touch_registry(data, email: str, now: str):
    """Registry of available emails — every new email goes to the list."""
    registry = data.setdefault("registry", [])
    entry = next((r for r in registry if r["email"].lower() == email.lower()), None)
    if entry:
        entry["last_used"] = now
        entry["count"] = int(entry.get("count", 0)) + 1
    else:
        registry.append({"email": email, "first_used": now, "last_used": now, "count": 1})
    registry.sort(key=lambda r: r.get("last_used", ""), reverse=True)
    del registry[max(1, MAX_EMAILS):]


def _trim(data):
    """Purge expired (TTL) then trim to caps — keeps the file bounded."""
    cutoff = _cutoff()
    if cutoff:
        data["sent"] = [e for e in data.get("sent", []) if not _expired(e.get("at"), cutoff)]
        data["errors"] = [e for e in data.get("errors", []) if not _expired(e.get("at"), cutoff)]
        data["registry"] = [r for r in data.get("registry", [])
                            if not _expired(r.get("last_used"), cutoff)]
    data["sent"] = data.get("sent", [])[-max(1, MAX_SENT):]
    data["errors"] = data.get("errors", [])[-max(1, MAX_ERRORS):]
    data["registry"] = data.get("registry", [])[-max(1, MAX_EMAILS):]


def record_email(to_email: str, subject: str = "", body: str = "",
                 ok: bool = True, info: str = "") -> bool:
    """Record an email attempt: metadata + status, and register the address."""
    try:
        to_email = str(to_email or "").strip().lower()
        if not to_email or "@" not in to_email:
            return False
        now = _now()

        def fn(data):
            data = dict(data or {})
            seq = int(data.get("seq", 0)) + 1
            data["seq"] = seq
            record = {
                "id": seq,
                "to": to_email,
                "subject": str(subject or ""),
                "body": str(body or "")[:max(0, BODY_MAX)],
                "at": now,
            }
            if ok:
                record["info"] = str(info or "")
                data.setdefault("sent", []).append(record)
            else:
                record["error"] = str(info or "")
                data.setdefault("errors", []).append(record)
            _touch_registry(data, to_email, now)
            _trim(data)
            return data

        return update_json(EMAILS, fn, default={})
    except Exception as e:
        logger.error("cache: record_email failed: %s", e)
        return False


def _index(data):
    """Recipient -> entries index (O(1) lookups); rebuilt per generation."""
    if _email_index["gen"] == _gen.get(EMAILS, 0):
        return _email_index
    sent, errors = {}, {}
    for entry in data.get("sent", []):
        sent.setdefault(entry["to"].lower(), []).append(entry)
    for entry in data.get("errors", []):
        errors.setdefault(entry["to"].lower(), []).append(entry)
    _email_index.update({"gen": _gen.get(EMAILS, 0), "sent": sent, "errors": errors})
    return _email_index


def _search(data, pool: str, recipient=None, contains=None,
            since=None, limit=20) -> list:
    limit = max(0, int(limit or 0))
    if limit == 0:
        return []
    entries = data.get(pool, [])
    idx = _index(data)
    if recipient:
        entries = list(idx[pool].get(str(recipient).strip().lower(), []))
    cutoff = _cutoff()
    if cutoff:  # TTL: expirados nunca aparecem (índice ou varredura)
        entries = [e for e in entries if not _expired(e.get("at"), cutoff)]
    entries = list(reversed(entries))  # newest first
    if since:
        entries = [e for e in entries if e.get("at", "") >= since]
    if contains:
        needle = str(contains).strip().lower()
        entries = [e for e in entries
                   if needle in e.get("subject", "").lower()
                   or needle in e.get("body", "").lower()]
    return [dict(e) for e in entries[:limit]]


def list_sent_emails(recipient=None, contains=None, since=None, limit=20) -> list:
    """Sent emails (newest first), filterable — indexed O(1) by recipient."""
    try:
        with _lock:
            data = read_json(EMAILS, default={}) or {}
            return _search(data, "sent", recipient, contains, since, limit)
    except Exception as e:
        logger.error("cache: list_sent_emails failed: %s", e)
        return []


def list_error_emails(recipient=None, contains=None, since=None, limit=20) -> list:
    """Failed emails (newest first), filterable — indexed O(1) by recipient."""
    try:
        with _lock:
            data = read_json(EMAILS, default={}) or {}
            return _search(data, "errors", recipient, contains, since, limit)
    except Exception as e:
        logger.error("cache: list_error_emails failed: %s", e)
        return []


def list_available_emails(limit=50) -> list:
    """Registered email addresses, most recently used first (TTL-filtered)."""
    try:
        limit = max(0, int(limit or 0))
        if limit == 0:
            return []
        with _lock:
            data = read_json(EMAILS, default={}) or {}
            cutoff = _cutoff()
            registry = data.get("registry", [])
            if cutoff:
                registry = [r for r in registry if not _expired(r.get("last_used"), cutoff)]
            return [r["email"] for r in registry[:limit]]
    except Exception as e:
        logger.error("cache: list_available_emails failed: %s", e)
        return []
