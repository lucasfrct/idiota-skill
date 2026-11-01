"""
Standalone e-mail module for the Idiota Alexa Skill.

Self-contained: loads .env from this file's directory (then from cwd) and
performs ONLY the invoked operation — no connection or side effect on import.

Operations (all return the same envelope):
    {"ok": True,  "data": <payload>}        success
    {"ok": False, "error": "<mensagem>"}    failure (tratado, nunca lança)

send_email EXIGE título (subject), corpo (body) e remetente (from_email ou
EMAIL_SENDER no ambiente) — sem defaults silenciosos. O título pode ser
sugerido no fim do fluxo, quando o corpo já existe: suggest_subject(body).

    list_emails([n])           caixa de entrada COMPLETA em blocos: remetente,
                               assunto, data, corpo integral e links extraídos
    list_addresses([termo])    SOMENTE endereços de e-mail (sem mensagem), dedupe
    find_email(...)            busca e-mail ESPECÍFICO por remetente, assunto,
                               termo e/ou data — traz corpo completo p/ leitura
    format_email_blocks(items) apresenta os e-mails em blocos legíveis

CLI (standalone usage):
    python mail.py send --body "texto" [--subject X] [--to DEST] [--from REM]
        (--subject ausente é sugerido do corpo via suggest_subject)
    python mail.py list [--limit 10] [--headers-only] [--json]
    python mail.py addresses [termo] [--limit 50] [--json]
    python mail.py filter <termo> [--limit 10] [--json]
    python mail.py read <uid> [--json]
"""
import argparse
import base64
import imaplib
import json
import logging
import os
import re
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email import message_from_bytes
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from datetime import date as _date, datetime, timedelta, timezone

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


class _HTMLText(HTMLParser):
    """Extrai o texto visível de um e-mail HTML (ignora script/style/head)."""

    SKIP = {"script", "style", "head", "title"}
    BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)


def _html_to_text(html):
    """Texto legível de um corpo HTML ("" se o parser falhar)."""
    parser = _HTMLText()
    try:
        parser.feed(html or "")
    except Exception:
        return ""
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _decode_part(part):
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _body_text(message):
    """Corpo em texto puro; se o e-mail for só HTML, extrai o texto visível."""
    text = ""
    html = ""
    for part in message.walk():
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not text:
            text = _decode_part(part)
        elif ctype == "text/html" and not html:
            html = _decode_part(part)
    return text or _html_to_text(html)


def _extract_links(message, max_links=10):
    """Links http(s) presentes no corpo (texto puro e hrefs do HTML), dedupe."""
    chunks = []
    for part in message.walk():
        if part.get_filename():
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            chunks.append(_decode_part(part))
    links = []
    for text in chunks:
        for url in re.findall(r"https?://[^\s<>\"'()]+", text or ""):
            url = url.rstrip(".,;:!?")
            if url and url not in links:
                links.append(url)
            if len(links) >= max_links:
                return links
    return links


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
                item["links"] = _extract_links(message)
            messages.append(item)
        return messages


# --------------------------------------------------------------------------
# Operações públicas (envelope uniforme)
# --------------------------------------------------------------------------

def suggest_subject(body, max_len=48):
    """Sugere um título curto a partir do corpo (fim do fluxo, corpo já conhecido).

    Heurística local (sem rede): primeira frase; se curta demais, o texto todo;
    corta em limite de palavras com reticências. Corpo vazio -> "".
    """
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    if not text:
        return ""
    first = re.split(r"[.!?;\n]", text, maxsplit=1)[0].strip()
    if len(first) < 4:
        first = text
    if len(first) > max_len:
        cut = first[:max_len]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        first = cut.rstrip(",;: ") + "…"
    return first


def send_email(subject=None, body=None, from_email=None, to_email=None):
    """Envia e-mail via SMTP — EXIGE título, corpo e remetente.

    subject: obrigatório (sem default silencioso; sugira com suggest_subject)
    body:    obrigatório
    from_email: obrigatório — explícito ou EMAIL_SENDER no ambiente
    to_email:   explícito, EMAIL_RECIPIENT no ambiente, ou o próprio remetente
    """
    subject = str(subject or "").strip()
    body = str(body or "").strip()
    from_email = from_email or os.getenv("EMAIL_SENDER")
    to_email = to_email or os.getenv("EMAIL_RECIPIENT") or from_email

    if not body:
        return _err("Corpo do e-mail (body) é obrigatório")
    if not subject:
        return _err("Assunto (título) do e-mail é obrigatório — sugira com suggest_subject(body)")
    if not from_email:
        return _err("Remetente ausente: passe from_email ou defina EMAIL_SENDER")
    if "@" not in from_email:
        return _err(f"Remetente inválido: {from_email}")
    if not to_email or "@" not in to_email:
        return _err("Destinatário inválido ou ausente")

    try:
        message = MIMEMultipart()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
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


# Ordem de urgência/importância para a leitura da caixa de entrada
URGENT_KEYWORDS = (
    "urgente", "prazo", "vence", "vencimento", "hoje", "amanh", "multa",
    "jurídico", "juridico", "tribunal", "processo", "cobran", "inadimpl",
    "bloquead", "suspens", "fiscal", "imposto", "regulariz", "penalidade",
    "notificação", "notificacao", "rescis", "protesto", "serasa", "spc",
)
IMPORTANT_KEYWORDS = (
    "reuni", "responder", "aguardando", "pendente", "confirma", "agenda",
    "renova", "assinatura", "contrato", "pagamento", "fatura", "nota fiscal",
    "lembrete", "orçamento", "orcamento", "proposta", "comunicado", "verificar",
    "document", "autoriz", "agend", "convoc",
)


def urgency_score(item) -> int:
    """Pontuação determinística de urgência/importância de um e-mail.

    Palavras de prazo/cobrança/jurídico pesam mais; recência soma; remetente
    no-reply desce (comunicados transacionais raramente são urgentes).
    """
    hay = " ".join((item.get("subject", ""), item.get("from", ""),
                    item.get("body", ""))).lower()
    score = min(75, 25 * sum(1 for k in URGENT_KEYWORDS if k in hay))
    score += min(30, 10 * sum(1 for k in IMPORTANT_KEYWORDS if k in hay))
    day = _day_key(item.get("date"))
    today = _date.today().isoformat()
    if day == today:
        score += 15
    elif day == (_date.today() - timedelta(days=1)).isoformat():
        score += 8
    elif day >= (_date.today() - timedelta(days=7)).isoformat():
        score += 4
    sender = item.get("from", "").lower()
    if "no-reply" in sender or "noreply" in sender or "naoresponda" in sender:
        score -= 15
    return score


def urgency_label(score) -> str:
    """Rótulo de urgência para narração: alta | media | baixa."""
    if score >= 40:
        return "alta"
    if score >= 15:
        return "media"
    return "baixa"


def list_emails(limit=10, with_body=True, order="recent"):
    """Lista os e-mails recentes da caixa de entrada COMPLETOS por padrão:
    remetente, assunto, data, corpo integral e links extraídos.
    with_body=False devolve só cabeçalhos (leve, para uso interno).
    order="urgency" ordena por urgência/importância (heurística local:
    palavras de prazo/cobrança/jurídico, recência; remetente no-reply desce)
    e marca cada item com "urgencia": alta | media | baixa."""
    try:
        limit = max(1, int(limit))
        items = _fetch_messages("ALL", limit, with_body=with_body)
        if order == "urgency":
            for item in items:
                item["urgencia"] = urgency_label(urgency_score(item))
            items.sort(key=urgency_score, reverse=True)  # estável: empate = mais novo 1º
        return _ok(items)
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao listar e-mails: {e}")


def _day_key(date_hdr):
    """Chave do dia (AAAA-MM-DD, UTC) a partir do cabeçalho Date; "" se ilegível."""
    try:
        return parsedate_to_datetime(date_hdr).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _parse_day(value):
    """Converte o pedido de data em chave de dia ou substring do cabeçalho.

    Aceita: hoje | ontem | anteontem | AAAA-MM-DD | DD/MM/AAAA | DD-MM-AAAA.
    Qualquer outro vira substring crua do cabeçalho (ex.: "oct", "31 oct").
    Retorna (chave_ou_substring, eh_substring).
    """
    v = str(value or "").strip().lower()
    if not v:
        return None, False
    today = datetime.now(timezone.utc).date()
    named = {"hoje": 0, "today": 0, "ontem": 1, "yesterday": 1, "anteontem": 2}
    if v in named:
        return (today - timedelta(days=named[v])).isoformat(), False
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        return _date(int(m[1]), int(m[2]), int(m[3])).isoformat(), False
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", v)
    if m:
        return _date(int(m[3]), int(m[2]), int(m[1])).isoformat(), False
    return v, True


def find_email(term=None, sender=None, subject=None, date=None, limit=5):
    """Busca e-mails ESPECÍFICOS por remetente, assunto, termo e/ou data.

    Diferente de filter_emails (título/remetente apenas), traz o CORPO COMPLETO
    de cada resultado — é a operação de leitura de um e-mail escolhido.
    Data aceita hoje/ontem/anteontem, AAAA-MM-DD, DD/MM/AAAA ou substring do
    cabeçalho (ex.: "oct"). Pelo menos um critério é exigido. Envelope ok/error.
    """
    if not any(str(c or "").strip() for c in (term, sender, subject, date)):
        return _err("Informe um critério: remetente, assunto, termo ou data")
    try:
        limit = max(1, min(int(limit or 5), 20))
    except (TypeError, ValueError):
        limit = 5

    day, is_sub = _parse_day(date)
    needle = str(term or "").strip().lower()
    sender_n = str(sender or "").strip().lower()
    subject_n = str(subject or "").strip().lower()
    date_raw = str(date or "").strip().lower()

    try:
        messages = _fetch_messages("ALL", max(limit * 10, 60), with_body=True)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao buscar e-mails: {e}")

    hits = []
    for m in messages:
        if sender_n and sender_n not in m["from"].lower():
            continue
        if subject_n and subject_n not in m["subject"].lower():
            continue
        if needle and needle not in (
                m["from"] + " " + m["subject"] + " " + m.get("body", "")).lower():
            continue
        if date:
            if is_sub:
                if date_raw not in m["date"].lower():
                    continue
            elif _day_key(m["date"]) != day:
                continue
        hits.append(m)
        if len(hits) >= limit:
            break
    return _ok(hits)


def list_addresses(term=None, limit=50):
    """Lista SOMENTE os endereços de e-mail (sem mensagem), dedupe, da caixa
    de entrada recente. Filtro opcional por termo (no endereço ou no nome)."""
    try:
        limit = max(1, int(limit))
        scan = max(limit * 5, 50)
        messages = _fetch_messages("ALL", scan, with_body=False)
        needle = (term or "").strip().lower()
        addresses, seen = [], set()
        for m in messages:
            for _name, addr in getaddresses([m["from"]]):
                addr = (addr or "").strip().lower()
                if "@" not in addr or addr in seen:
                    continue
                if needle and needle not in addr and needle not in m["from"].lower():
                    continue
                seen.add(addr)
                addresses.append(addr)
                if len(addresses) >= limit:
                    return _ok(addresses)
        return _ok(addresses)
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao listar endereços: {e}")


def list_contacts(term=None, limit=50):
    """Contatos da caixa de entrada: [{"nome", "email"}] dedupe, recentes 1º.

    Nome = display name do cabeçalho From (fallback: parte local do endereço).
    Filtro opcional por termo (casado no nome OU no endereço). Envelope ok/error.
    """
    try:
        limit = max(1, int(limit))
        scan = max(limit * 5, 50)
        messages = _fetch_messages("ALL", scan, with_body=False)
        needle = (term or "").strip().lower()
        contacts, seen = [], set()
        for m in messages:
            for name, addr in getaddresses([m["from"]]):
                addr = (addr or "").strip().lower()
                if "@" not in addr or addr in seen:
                    continue
                name = re.sub(r"\s+", " ", (name or "")).strip().strip('"')
                if not name:
                    name = addr.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
                if needle and needle not in addr and needle not in name.lower():
                    continue
                seen.add(addr)
                contacts.append({"nome": name, "email": addr})
                if len(contacts) >= limit:
                    return _ok(contacts)
        return _ok(contacts)
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao listar contatos: {e}")


def format_email_blocks(items):
    """Apresenta e-mails em blocos legíveis (um por e-mail, tudo completo)."""
    blocks = []
    for i, m in enumerate(items, 1):
        links = m.get("links") or []
        body = re.sub(r"[ \t]+", " ", m.get("body", "") or "").strip()
        block = (
            f"{'─' * 56}\n"
            f"[{i}] UID {m.get('uid', '?')}\n"
            f"De:      {m.get('from', '—')}\n"
            f"Assunto: {m.get('subject', '—')}\n"
            f"Data:    {m.get('date', '—')}\n"
            f"Links:   {', '.join(links) if links else '—'}\n"
            f"Corpo:\n{body or '—'}"
        )
        blocks.append(block)
    return "\n\n".join(blocks) if blocks else "Nenhum e-mail na caixa de entrada."


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
                "links": _extract_links(message),
            })
    except ValueError as e:
        return _err(e)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return _err(f"Falha ao ler e-mail: {e}")


# --------------------------------------------------------------------------
# CLI standalone — executa apenas a função invocada
# --------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="mail.py standalone: enviar e-mails, listar caixa em blocos, "
                    "listar endereços e ler mensagens")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cmd_send = sub.add_parser("send", help="envia um e-mail")
    cmd_send.add_argument("--from", dest="from_email", help="remetente (default: EMAIL_SENDER)")
    cmd_send.add_argument("--to", dest="to_email", help="destinatário (default: EMAIL_RECIPIENT)")
    cmd_send.add_argument("--subject", help="assunto (ausente: sugerido do corpo)")
    cmd_send.add_argument("--body", required=True, help="corpo do e-mail")

    cmd_list = sub.add_parser("list", help="lista e-mails COMPLETOS em blocos (corpo, links, remetente)")
    cmd_list.add_argument("--limit", type=int, default=10)
    cmd_list.add_argument("--headers-only", action="store_true", help="sem corpo/links (leve)")
    cmd_list.add_argument("--json", action="store_true", help="saída JSON em vez de blocos")

    cmd_addr = sub.add_parser("addresses", help="lista somente endereços de e-mail (filtro opcional)")
    cmd_addr.add_argument("term", nargs="?", default=None, help="filtro (endereço ou nome)")
    cmd_addr.add_argument("--limit", type=int, default=50)
    cmd_addr.add_argument("--json", action="store_true")

    cmd_filter = sub.add_parser("filter", help="filtra e-mails por termo (remetente/assunto)")
    cmd_filter.add_argument("term")
    cmd_filter.add_argument("--limit", type=int, default=10)
    cmd_filter.add_argument("--json", action="store_true")

    cmd_read = sub.add_parser("read", help="lê um e-mail completo pelo UID")
    cmd_read.add_argument("uid")
    cmd_read.add_argument("--json", action="store_true")

    args = parser.parse_args()
    human = None
    if args.cmd == "send":
        subject = args.subject or suggest_subject(args.body)
        result = send_email(subject=subject, body=args.body,
                            from_email=args.from_email, to_email=args.to_email)
    elif args.cmd == "list":
        result = list_emails(args.limit, with_body=not args.headers_only)
        if result.get("ok") and not args.json:
            human = format_email_blocks(result["data"])
    elif args.cmd == "addresses":
        result = list_addresses(args.term, args.limit)
        if result.get("ok") and not args.json:
            human = "\n".join(f"• {a}" for a in result["data"]) or "Nenhum endereço encontrado."
    elif args.cmd == "filter":
        result = filter_emails(args.term, args.limit)
        if result.get("ok") and not args.json:
            human = format_email_blocks(result["data"])
    else:
        result = read_email(args.uid)
        if result.get("ok") and not args.json:
            human = format_email_blocks([result["data"]])
    print(human if human is not None else json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()
