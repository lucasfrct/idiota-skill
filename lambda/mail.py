"""
Standalone e-mail module for the Idiota Alexa Skill.

Self-contained: loads .env from this file's directory (then from cwd) and
performs ONLY the invoked operation — no connection or side effect on import.

Operations (all return the same envelope):
    {"ok": True,  "data": <payload>}        success
    {"ok": False, "error": "<mensagem>"}    failure (tratado, nunca lança)

CLI (standalone usage):
    python mail.py send --body "texto" [--subject X] [--to DEST] [--from REM]
    python mail.py list [--limit 10]
    python mail.py filter <termo> [--limit 10]
    python mail.py read <uid>
"""
import argparse
import base64
import imaplib
import json
import logging
import os
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email import message_from_bytes
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))  # envs do próprio módulo
load_dotenv()                                   # fallback: .env do cwd

TIMEOUT = 10  # segundos — evita estourar o limite do Lambda
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

_oauth_cache = {}  # {"token": str, "exp": float} — cache do access token por processo

logger = logging.getLogger(__name__)


def _ok(data):
    return {"ok": True, "data": data}


def _err(message):
    logger.error("mail.py: %s", message)
    return {"ok": False, "error": str(message)}


def _xoauth2_token():
    """Access token OAuth2 via refresh grant (XOAUTH2 — independe de senha de app)."""
    if _oauth_cache.get("exp", 0) > time.time() + 60:
        return _oauth_cache["token"]
    client_id = os.getenv("EMAIL_OAUTH_CLIENT_ID")
    client_secret = os.getenv("EMAIL_OAUTH_CLIENT_SECRET")
    refresh = os.getenv("EMAIL_OAUTH_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh):
        raise ValueError("OAuth ausente: defina EMAIL_OAUTH_CLIENT_ID, EMAIL_OAUTH_CLIENT_SECRET e EMAIL_OAUTH_REFRESH_TOKEN")
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
        raise ValueError(f"Refresh OAuth falhou (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Refresh OAuth falhou: {e.reason}") from e
    if "access_token" not in data:
        raise ValueError(f"Refresh OAuth falhou: {data.get('error', data)}")
    _oauth_cache["token"] = data["access_token"]
    _oauth_cache["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _oauth_cache["token"]


def _smtp_conf():
    """Config SMTP a partir das envs — fail-fast se ausentes."""
    host = os.getenv("EMAIL_HOST", "smtp.gmail.com")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    if not user:
        raise ValueError("Credenciais SMTP ausentes: defina EMAIL_SENDER")
    if not password and os.getenv("EMAIL_AUTH", "").lower() != "xoauth2":
        raise ValueError("Credenciais SMTP ausentes: defina EMAIL_PASSWORD ou EMAIL_AUTH=xoauth2")
    return host, port, user, password


def _imap_conf():
    """Config IMAP: EMAIL_IMAP_HOST/PORT ou derivado do EMAIL_HOST (smtp.→imap.)."""
    host = os.getenv("EMAIL_IMAP_HOST") or os.getenv("EMAIL_HOST", "imap.gmail.com").replace("smtp.", "imap.", 1)
    port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
    user = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    if not user:
        raise ValueError("Credenciais IMAP ausentes: defina EMAIL_SENDER")
    if not password and os.getenv("EMAIL_AUTH", "").lower() != "xoauth2":
        raise ValueError("Credenciais IMAP ausentes: defina EMAIL_PASSWORD ou EMAIL_AUTH=xoauth2")
    return host, port, user, password


def _smtp_auth(server, user, password):
    """Autentica SMTP: XOAUTH2 (EMAIL_AUTH=xoauth2) ou AUTH PLAIN com senha de app."""
    if os.getenv("EMAIL_AUTH", "").lower() == "xoauth2":
        token = _xoauth2_token()
        sasl = base64.b64encode(f"user={user}\x01auth=Bearer {token}\x01\x01".encode()).decode()
        code, response = server.docmd("AUTH", "XOAUTH2 " + sasl)
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, response)
        return
    # AUTH PLAIN explícito: traz o erro real do servidor (ex.: 535 do Gmail)
    # em vez do 'Connection unexpectedly closed' que o login() dispara quando
    # o servidor encerra a conexão após recusar a credencial.
    token = base64.b64encode(f"\x00{user}\x00{password}".encode()).decode()
    code, response = server.docmd("AUTH", "PLAIN " + token)
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, response)


def _connect_smtp():
    host, port, user, password = _smtp_conf()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        server.starttls()
    _smtp_auth(server, user, password)
    return server


def _connect_imap():
    """Conexão IMAP autenticada (XOAUTH2 ou LOGIN), INBOX selecionada readonly."""
    host, port, user, password = _imap_conf()
    imap = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
    if os.getenv("EMAIL_AUTH", "").lower() == "xoauth2":
        token = _xoauth2_token()
        sasl = f"user={user}\x01auth=Bearer {token}\x01\x01"
        imap.authenticate("XOAUTH2", lambda _: sasl.encode())
    else:
        imap.login(user, password)
    imap.select("INBOX", readonly=True)
    return imap


def _decode(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body_text(message):
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")
    return ""


def _fetch_messages(criteria, limit, with_body):
    """Busca mensagens da INBOX (mais recentes primeiro) via IMAP."""
    with _connect_imap() as imap:
        status, data = imap.uid("search", None, criteria)
        if status != "OK":
            raise RuntimeError(f"Busca IMAP falhou (status {status})")
        uids = data[0].split()[::-1][:limit]
        messages = []
        for uid in uids:
            _, fetched = imap.uid("fetch", uid, "(RFC822)" if with_body else "(BODY.PEEK[HEADER])")
            if not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            message = message_from_bytes(raw)
            item = {
                "uid": uid.decode(),
                "from": _decode(message.get("From")),
                "subject": _decode(message.get("Subject")),
                "date": _decode(message.get("Date")),
            }
            if with_body:
                item["body"] = _body_text(message)
            messages.append(item)
        return messages


# --------------------------------------------------------------------------
# Operações públicas (envelope uniforme)
# --------------------------------------------------------------------------

def send_email(from_email=None, to_email=None, subject=None, body=None):
    """Envia e-mail via SMTP. Defaults vêm das envs (EMAIL_SENDER/RECIPIENT/SUBJECT)."""
    from_email = from_email or os.getenv("EMAIL_SENDER")
    to_email = to_email or os.getenv("EMAIL_RECIPIENT") or from_email
    subject = subject or os.getenv("EMAIL_SUBJECT", "Mensagem do Idiota")
    if not body or not body.strip():
        return _err("Corpo do e-mail (body) é obrigatório")
    if not from_email or "@" not in from_email:
        return _err("Remetente inválido ou ausente (EMAIL_SENDER)")
    if not to_email or "@" not in to_email:
        return _err("Destinatário inválido ou ausente")

    try:
        message = MIMEMultipart()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain", "utf-8"))

        with _connect_smtp() as server:
            server.sendmail(from_email, [to_email], message.as_string())
        logger.info("E-mail enviado: %s -> %s", from_email, to_email)
        return _ok(f"E-mail enviado com sucesso para {to_email}")
    except ValueError as e:
        return _err(e)
    except smtplib.SMTPAuthenticationError:
        return _err("Autenticação SMTP falhou: verifique EMAIL_SENDER/EMAIL_PASSWORD (use senha de app)")
    except (smtplib.SMTPException, OSError) as e:
        return _err(f"Falha ao enviar e-mail: {e}")


def list_emails(limit=10):
    """Lista os e-mails mais recentes da caixa de entrada (sem corpo)."""
    try:
        limit = max(1, int(limit))
        return _ok(_fetch_messages("ALL", limit, with_body=False))
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao listar e-mails: {e}")


def filter_emails(term, limit=10):
    """Filtra e-mails recentes por termo em remetente ou assunto."""
    if not term or not term.strip():
        return _err("Informe um termo para filtrar")
    try:
        limit = max(1, int(limit))
        messages = _fetch_messages("ALL", max(limit * 5, 50), with_body=False)
        needle = term.strip().lower()
        hits = [m for m in messages
                if needle in m["from"].lower() or needle in m["subject"].lower()]
        return _ok(hits[:limit])
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao filtrar e-mails: {e}")


def read_email(uid):
    """Lê um e-mail completo (com corpo) pelo UID."""
    if not uid or not str(uid).strip().isdigit():
        return _err("UID inválido: informe o número retornado por list/filter")
    try:
        with _connect_imap() as imap:
            status, fetched = imap.uid("fetch", str(uid).strip().encode(), "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                return _err(f"E-mail com UID {uid} não encontrado")
            message = message_from_bytes(fetched[0][1])
            return _ok({
                "uid": str(uid).strip(),
                "from": _decode(message.get("From")),
                "to": _decode(message.get("To")),
                "subject": _decode(message.get("Subject")),
                "date": _decode(message.get("Date")),
                "body": _body_text(message),
            })
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao ler e-mail: {e}")


# --------------------------------------------------------------------------
# CLI standalone — executa apenas a função invocada
# --------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="mail.py: enviar, listar, filtrar e ler e-mails (standalone)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cmd_send = sub.add_parser("send", help="envia um e-mail")
    cmd_send.add_argument("--from", dest="from_email", help="remetente (default: EMAIL_SENDER)")
    cmd_send.add_argument("--to", dest="to_email", help="destinatário (default: EMAIL_RECIPIENT)")
    cmd_send.add_argument("--subject", help="assunto (default: EMAIL_SUBJECT)")
    cmd_send.add_argument("--body", required=True, help="corpo do e-mail")

    cmd_list = sub.add_parser("list", help="lista os e-mails recentes")
    cmd_list.add_argument("--limit", type=int, default=10)

    cmd_filter = sub.add_parser("filter", help="filtra por termo (remetente/assunto)")
    cmd_filter.add_argument("term")
    cmd_filter.add_argument("--limit", type=int, default=10)

    cmd_read = sub.add_parser("read", help="lê um e-mail completo pelo UID")
    cmd_read.add_argument("uid")

    args = parser.parse_args()
    if args.cmd == "send":
        result = send_email(args.from_email, args.to_email, args.subject, args.body)
    elif args.cmd == "list":
        result = list_emails(args.limit)
    elif args.cmd == "filter":
        result = filter_emails(args.term, args.limit)
    else:
        result = read_email(args.uid)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()
