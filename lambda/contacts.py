"""
Agenda de contatos do Idiota — Google People API + cache local.

Fontes ("cache primeiro"):
1. cache local (contacts.json — endereços aprendidos do e-mail)
2. Google People API (contatos da conta Google: nomes, telefones, e-mails) —
   ativa quando o trio GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN está no
   .env (escopo https://www.googleapis.com/auth/contacts, cliente GCP próprio
   em modo Testing com o dono como test user).

Envelope uniforme: {"ok": True, "data": ...} | {"ok": False, "error": "..."}
— nunca lança (mesmo contrato de mail.py).

CLI (standalone usage):
    python contacts.py list [termo] [--limit 50] [--json]
    python contacts.py phones <nome> [--json]
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))  # envs do próprio módulo
load_dotenv()                                   # fallback: .env do cwd

import cache  # noqa: E402  (envs já carregados)

TIMEOUT = 10
PEOPLE_API = "https://people.googleapis.com/v1/people:connections/list"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

_token_cache = {}

logger = logging.getLogger(__name__)


def _ok(data):
    return {"ok": True, "data": data}


def _err(message):
    logger.error("contacts.py: %s", message)
    return {"ok": False, "error": str(message)}


def _google_enabled() -> bool:
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID")
                and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
                and os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"))


def _google_token() -> str:
    """Access token via refresh grant (escopo contacts — People API)."""
    if _token_cache.get("exp", 0) > time.time() + 60:
        return _token_cache["token"]
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh):
        raise ValueError(
            "Google Contacts não configurado: defina GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET e GOOGLE_OAUTH_REFRESH_TOKEN "
            "(escopo https://www.googleapis.com/auth/contacts)")
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise ValueError(f"Refresh Google falhou (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Refresh Google falhou: {e.reason}") from e
    if "access_token" not in data:
        raise ValueError(f"Refresh Google falhou: {data.get('error', data)}")
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def google_contacts(limit=200):
    """Contatos da conta Google via People API.

    Retorna [{"nome", "telefones": [..], "emails": [..]}]. Lança ValueError/
    OSError em falha — as operações públicas convertem para envelope.
    """
    if not _google_enabled():
        raise ValueError(
            "Google Contacts não configurado: defina GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET e GOOGLE_OAUTH_REFRESH_TOKEN "
            "(escopo https://www.googleapis.com/auth/contacts)")
    token = _google_token()
    contacts, page_token = [], ""
    while len(contacts) < limit:
        params = {"pageSize": "200",
                  "personFields": "names,phoneNumbers,emailAddresses",
                  "access_token": token}
        if page_token:
            params["pageToken"] = page_token
        req = urllib.request.Request(PEOPLE_API + "?" + urllib.parse.urlencode(params),
                                     method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            data = json.loads(response.read())
        for person in data.get("connections", []):
            names = person.get("names", [])
            phones = [n.get("value", "") for n in person.get("phoneNumbers", []) if n.get("value")]
            emails = [v.get("value", "") for v in person.get("emailAddresses", []) if v.get("value")]
            if names or phones or emails:
                contacts.append({"nome": names[0].get("displayName", "") if names else "",
                                 "telefones": phones, "emails": emails})
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return contacts[:max(0, int(limit))]


def list_contacts(user_id: str, term=None, limit=50):
    """Agenda unificada: cache local primeiro; Google completa quando ativo.

    Retorna [{"nome", "email", "telefones", "emails", "fonte"}] — envelope.
    """
    try:
        limit = max(1, int(limit))
        needle = (term or "").strip()
        local = cache.search_contacts(user_id, needle, limit=limit)
        items = [{"nome": c.get("nome", ""), "email": c.get("email", ""),
                  "telefones": [], "emails": [c.get("email", "")] if c.get("email") else [],
                  "fonte": "cache"} for c in local]
        if _google_enabled() and len(items) < limit:
            try:
                google = google_contacts(limit=limit * 3)
                hay_needle = needle.lower()
                seen = {i["email"].lower() for i in items if i.get("email")}
                for c in google:
                    if len(items) >= limit:
                        break
                    emails = c.get("emails", [])
                    key = emails[0].lower() if emails else c.get("nome", "").lower()
                    if not key or key in seen:
                        continue
                    if needle and needle.lower() not in (
                            c.get("nome", "") + " " + " ".join(emails)).lower():
                        continue
                    seen.add(key)
                    items.append({"nome": c.get("nome", ""), "email": emails[0] if emails else "",
                                  "telefones": c.get("telefones", []), "emails": emails,
                                  "fonte": "google"})
            except (ValueError, OSError) as e:
                logger.warning("contacts: Google indisponível — respondendo só com o cache: %s", e)
        return _ok(items[:limit])
    except ValueError as e:
        return _err(e)
    except OSError as e:
        return _err(f"Falha ao listar contatos: {e}")


def contact_phones(user_id: str, name: str):
    """Telefones + e-mails de um contato pelo nome (cache → Google). Envelope."""
    if not name or not str(name).strip():
        return _err("Informe o nome do contato")
    result = list_contacts(user_id, name, limit=5)
    if not result.get("ok"):
        return result
    items = result.get("data", [])
    if not items:
        return _err(f"Contato '{name}' não encontrado na agenda")
    best = items[0]
    return _ok({
        "nome": best.get("nome", ""),
        "telefones": best.get("telefones", []),
        "emails": best.get("emails", []) or ([best["email"]] if best.get("email") else []),
        "fonte": best.get("fonte", ""),
    })


def _cli():
    import argparse
    parser = argparse.ArgumentParser(
        description="contacts.py: agenda unificada (cache local + Google People API)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cmd_list = sub.add_parser("list", help="lista contatos (filtro opcional)")
    cmd_list.add_argument("term", nargs="?", default=None)
    cmd_list.add_argument("--limit", type=int, default=50)
    cmd_list.add_argument("--json", action="store_true")
    cmd_phone = sub.add_parser("phones", help="telefones + e-mails de um contato")
    cmd_phone.add_argument("name")
    cmd_phone.add_argument("--json", action="store_true")
    args = parser.parse_args()
    user = os.getenv("EMAIL_SENDER", "local")
    if args.cmd == "list":
        result = list_contacts(user, args.term, args.limit)
        if result.get("ok") and not args.json:
            lines = []
            for c in result["data"]:
                tel = ", ".join(c.get("telefones", [])) or "—"
                em = ", ".join(c.get("emails", [])) or "—"
                lines.append(f"• {c.get('nome') or em} | tel: {tel} | e-mail: {em} | ({c.get('fonte')})")
            print("\n".join(lines) or "Nenhum contato encontrado.")
            return
    else:
        result = contact_phones(user, args.name)
        if result.get("ok") and not args.json:
            d = result["data"]
            print(f"{d.get('nome') or '(sem nome)'}\nTelefones: {', '.join(d.get('telefones', [])) or '—'}"
                  f"\nE-mails: {', '.join(d.get('emails', [])) or '—'}")
            return
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()
