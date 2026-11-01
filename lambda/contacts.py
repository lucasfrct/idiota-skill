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
import re
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
# CardDAV do Google (client verificado do Thunderbird — escopo carddav)
CARDAV_CLIENT_ID = os.getenv(
    "GOOGLE_CARDAV_CLIENT_ID",
    "406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com")
CARDAV_CLIENT_SECRET = os.getenv("GOOGLE_CARDAV_CLIENT_SECRET", "kSmqreRr0qwBWJgbf5Y-PjSU")
CARDAV_SCOPE = "https://www.googleapis.com/auth/carddav"

_token_cache = {}
_carddav_token_cache = {}

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
                  "personFields": "names,phoneNumbers,emailAddresses,organizations,addresses",
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
            orgs = person.get("organizations", [])
            addresses = person.get("addresses", [])
            if names or phones or emails:
                contacts.append({"nome": names[0].get("displayName", "") if names else "",
                                 "telefones": phones, "emails": emails,
                                 "empresa": (orgs[0].get("name", "") if orgs else ""),
                                 "cargo": (orgs[0].get("title", "") if orgs else ""),
                                 "endereco": (addresses[0].get("formattedValue", "") if addresses else "")})
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return contacts[:max(0, int(limit))]


def _carddav_token() -> str:
    """Access token via refresh grant (escopo carddav — client Thunderbird)."""
    if _carddav_token_cache.get("exp", 0) > time.time() + 60:
        return _carddav_token_cache["token"]
    refresh = os.getenv("GOOGLE_CARDAV_REFRESH_TOKEN")
    if not refresh:
        raise ValueError(
            "CardDAV não configurado: defina GOOGLE_CARDAV_REFRESH_TOKEN "
            "(fluxo OAuth com escopo https://www.googleapis.com/auth/carddav)")
    body = urllib.parse.urlencode({
        "client_id": CARDAV_CLIENT_ID, "client_secret": CARDAV_CLIENT_SECRET,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise ValueError(f"Refresh CardDAV falhou (HTTP {e.code}): "
                         f"{e.read().decode(errors='replace')[:200]}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Refresh CardDAV falhou: {e.reason}") from e
    if "access_token" not in data:
        raise ValueError(f"Refresh CardDAV falhou: {data.get('error', data)}")
    _carddav_token_cache["token"] = data["access_token"]
    _carddav_token_cache["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _carddav_token_cache["token"]


def _vcard_parse(vcard_text) -> dict:
    """Campos de um VCARD: FN, TEL, EMAIL, ORG, TITLE, ADR, NOTE/URL/BDAY."""
    out = {"nome": "", "telefones": [], "emails": [], "empresa": "",
           "cargo": "", "endereco": "", "outros": []}
    unfolded = re.sub(r"\r?\n[ \t]", "", vcard_text or "")
    for line in unfolded.splitlines():
        if ":" not in line:
            continue
        head, _, value = line.partition(":")
        key = head.split(";")[0].strip().upper()
        value = value.strip()
        if not value:
            continue
        if key == "FN" and not out["nome"]:
            out["nome"] = value
        elif key == "TEL" and len(out["telefones"]) < 4:
            out["telefones"].append(value)
        elif key == "EMAIL" and len(out["emails"]) < 4:
            out["emails"].append(value)
        elif key == "ORG" and not out["empresa"]:
            out["empresa"] = value.split(";")[0].strip()
        elif key == "TITLE" and not out["cargo"]:
            out["cargo"] = value
        elif key == "ADR" and not out["endereco"]:
            out["endereco"] = ";".join(p for p in value.split(";") if p.strip())[:120]
        elif key in ("NOTE", "URL", "BDAY") and len(out["outros"]) < 5:
            out["outros"].append(f"{key.title()}: {value[:120]}")
    return out


def carddav_contacts(limit=200):
    """Contatos REAIS da conta Google via CardDAV (REPORT → VCARDs).

    Não exige client GCP próprio: usa o client verificado do Thunderbird
    (escopo carddav). Lança ValueError/OSError — envelope nas operações.
    """
    token = _carddav_token()
    email = os.getenv("EMAIL_SENDER", "").strip()
    if not email:
        raise ValueError("EMAIL_SENDER ausente: necessário para o principal do CardDAV")
    url = f"https://www.googleapis.com/carddav/v1/principals/{email}/lists/default/"
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">'
            '<D:prop><D:getetag/><C:address-data/></D:prop>'
            '<C:filter><C:prop-filter name="UID"/></C:filter>'
            '</C:addressbook-query>')
    req = urllib.request.Request(url, data=body.encode(), method="REPORT", headers={
        "Depth": "1", "Content-Type": "application/xml; charset=utf-8",
        "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        xml_text = response.read().decode("utf-8", errors="replace")
    vcards = re.findall(r"<(?:[\w-]+:)?address-data>(.*?)</(?:[\w-]+:)?address-data>",
                        xml_text, re.S)
    contacts = []
    for vcard in vcards:
        parsed = _vcard_parse(vcard)
        if parsed.get("nome") or parsed.get("emails") or parsed.get("telefones"):
            contacts.append(parsed)
        if len(contacts) >= limit:
            break
    return contacts


def list_contacts(user_id: str, term=None, limit=50):
    """Agenda unificada: cache local primeiro; Google completa quando ativo.

    Retorna [{"nome", "email", "telefones", "emails", "fonte"}] — envelope.
    """
    try:
        limit = max(1, int(limit))
        needle = (term or "").strip()
        local = cache.search_contacts(user_id, needle, limit=limit)
        items = [{"nome": c.get("nome", ""), "email": c.get("email", ""),
                  "telefones": c.get("telefones", []),
                  "emails": [c.get("email", "")] if c.get("email") else [],
                  "empresa": "", "cargo": "", "endereco": "", "outros": [],
                  "fonte": "cache"} for c in local]
        source = ("people" if _google_enabled()
                  else ("carddav" if os.getenv("GOOGLE_CARDAV_REFRESH_TOKEN") else None))
        if source and len(items) < limit:
            try:
                # com termo, varre a agenda inteira; sem termo, só o necessário
                fetch_n = 500 if needle else limit * 3
                google = (google_contacts(limit=fetch_n) if source == "people"
                          else carddav_contacts(limit=fetch_n))
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
                                  "empresa": c.get("empresa", ""), "cargo": c.get("cargo", ""),
                                  "endereco": c.get("endereco", ""), "outros": c.get("outros", []),
                                  "fonte": f"google:{source}"})
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
        "empresa": best.get("empresa", ""),
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
                org = c.get("empresa") or "—"
                lines.append(f"• {c.get('nome') or em} | empresa: {org} | tel: {tel} "
                             f"| e-mail: {em} | ({c.get('fonte')})")
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
