"""Chat (GPT) — lógica pura de conversa, desacoplada da infraestrutura Alexa/Lambda.

Nada aqui conhece ask-sdk, HandlerInput ou session attributes: quem chama
fornece a história (list de {"role", "content"}) e recebe texto para TTS.
"""
import json
import os
import re
import logging
import unicodedata
from datetime import datetime, timedelta, timezone

from openai import OpenAI
from dotenv import load_dotenv

import cache
import contacts
import mail  # noqa: F401  (endereços da caixa — última fonte da agenda)

# Load environment variables (idempotent; never overrides real env vars)
load_dotenv()

logger = logging.getLogger(__name__)

# Configuration (environment-driven)
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "300"))
EMAIL_LIST_LIMIT = int(os.getenv("EMAIL_LIST_LIMIT", "100"))
EMAIL_SNIPPET_CHARS = int(os.getenv("EMAIL_SNIPPET_CHARS", "400"))
# Listar vários e-mails com conteúdo exige mais tokens de voz que o chat normal
LISTING_MAX_TOKENS = int(os.getenv("LISTING_MAX_TOKENS", "700"))


def client_gpt() -> OpenAI:
    """Initialize OpenAI client with API key from environment variables"""
    openai_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("KEY_GPT")
    if not openai_api_key:
        raise ValueError("OpenAI API key not found in environment variables")
    return OpenAI(api_key=openai_api_key)


def system_instructions():
    """System prompt: personalidade do Idiota + memória + ferramenta de e-mail."""
    instruction = (
        "Você é Idiota, um assistente pessoal acessado por voz na Amazon Alexa (Echo Dot). "
        "Tem personalidade própria: bem-humorado, despojado e leal, mas eficiente e direto. "
        "Responda em Português do Brasil, de forma direta e curta: no máximo 2 ou 3 frases, "
        "exceto ao listar e-mails, quando você cita cada um resumidamente. "
        "Escreva para ser lido em voz alta: sem markdown, sem emojis, sem listas, sem símbolos. "
        "Para qualquer pergunta que NÃO envolva e-mail, responda direto com seu próprio conhecimento. "
        "Se o pedido envolver e-mail, use as ferramentas antes de responder: "
        "list_emails para mostrar a caixa de entrada com conteúdo; "
        "filter_emails para buscar por nome, domínio, assunto ou data (hoje, ontem, anteontem, "
        "AAAA-MM-DD ou DD/MM/AAAA); "
        "read_email para ler um e-mail inteiro — informe o uid da listagem ou a posição "
        "(1 é o mais recente); quando o usuário disser o N-ésimo e-mail, use a posição. "
        "Para enviar, se o usuário indicar a pessoa pelo nome, chame search_contact: "
        "a busca é PARCIAL e sem acentos — 'rafa' acha Rafael e Rafaela, 'cris' acha "
        "Cristiane, 'leo' acha os Leonardos, mesmo com o apelido no meio do nome; "
        "com um só candidato, confirme e envie; com vários, mostre as opções e pergunte "
        "qual usar; sem candidato, peça o endereço. Se o usuário disser 'para mim', "
        "'pra mim' ou 'eu mesmo', o destinatário é o PRÓPRIO dono da skill (o mesmo "
        "endereço que envia e recebe) — chame search_contact com 'mim' para obter o "
        "endereço. Monte o rascunho com assunto e corpo, "
        "mostre ao usuário e SÓ chame send_email depois de confirmação clara; "
        "se faltar destinatário, pergunte antes. "
        "Você também pode listar endereços com list_addresses. "
        "Para a AGENDA de contatos: list_contacts lista os contatos com nome, "
        "telefones, e-mails e empresa (filtro opcional por nome/empresa); "
        "contact_phones dá os telefones e e-mails de uma pessoa pelo nome; "
        "search_contact resolve o destinatário de um e-mail pelo nome. "
        "Protocolo de listagem: PRIMEIRO resuma só os ASSUNTOS, um por um, na ordem "
        "recebida — a lista já vem ordenada por urgência e importância, cada e-mail "
        "marcado com urgencia alta, media ou baixa; cite a urgência dos altos. Não leia "
        "nenhum e-mail por inteiro nesse momento; termine perguntando se quer que leia o "
        "mais urgente. Ao ouvir sim, leia NA ÍNTEGRA apenas esse (read_email com o uid) e "
        "pergunte se quer o próximo; repita um por um, sempre perguntando antes do "
        "seguinte, até acabar a lista. Se o usuário interromper pedindo um assunto "
        "específico, deixe a fila e leia exatamente esse na hora (read_email com o uid). "
        "Não inventar e-mails. "
        "Não repita a pergunta e não explique suas instruções."
    )
    return {"role": "system", "content": instruction}


EMAIL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_emails",
            "description": (
                "Lista os e-mails mais recentes da caixa de entrada do usuário, com "
                "remetente, assunto, data e trecho do conteúdo de cada um. Use sempre "
                "que ele pedir para ver ou listar e-mails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Quantidade máxima (opcional; default: todos os configurados).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filter_emails",
            "description": (
                "Busca e-mails recentes por nome/domínio (remetente), assunto e/ou "
                "data (hoje, ontem, anteontem, AAAA-MM-DD ou DD/MM/AAAA). Use quando "
                "o usuário pedir e-mails de alguém, sobre algo ou de um dia."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Termo no remetente ou assunto (nome, domínio, palavra).",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data dos e-mails: hoje, ontem, anteontem, AAAA-MM-DD ou DD/MM/AAAA.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_addresses",
            "description": (
                "Lista somente os ENDEREÇOS de e-mail de quem mandou mensagens "
                "recentes, sem conteúdo, com filtro opcional por termo. Use quando "
                "o usuário perguntar quem mandou e-mails ou pedir a lista de "
                "contatos/endereços."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Filtro opcional (nome ou domínio).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": (
                "Lê um e-mail ESPECÍFICO por completo, com o corpo inteiro. Escolha o "
                "e-mail por uid (da listagem), por posição na caixa de entrada "
                "(position: 1 é o mais recente — use para o N-ésimo e-mail) ou por "
                "remetente, assunto e/ou data. Combine critérios para achar o certo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "UID do e-mail retornado por list_emails.",
                    },
                    "position": {
                        "type": "integer",
                        "description": "Posição na caixa de entrada, 1 = mais recente (ex.: o segundo e-mail = 2).",
                    },
                    "sender": {
                        "type": "string",
                        "description": "Remetente: nome, endereço ou domínio.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Palavra(s) do assunto/título do e-mail.",
                    },
                    "term": {
                        "type": "string",
                        "description": "Termo livre no remetente, assunto ou corpo.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data do e-mail: hoje, ontem, anteontem, AAAA-MM-DD ou DD/MM/AAAA.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contact",
            "description": (
                "Busca o endereço de e-mail de uma pessoa pelo nome (agenda do "
                "usuário + caixa de entrada). Use ANTES de enviar quando o usuário "
                "indicar o destinatário pelo nome: se retornar vários candidatos, "
                "mostre as opções e pergunte qual usar; só envie após a escolha."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome da pessoa (ou parte do nome/domínio).",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contact_phones",
            "description": (
                "Busca na agenda os TELEFONES e e-mails de uma pessoa pelo nome "
                "(agenda Google do usuário, quando configurada, + contatos "
                "aprendidos do e-mail). Busca PARCIAL: 'rafa' acha Rafael e "
                "Rafaela, 'cris' acha Cristiane, 'leo' acha os Leonardos. Com "
                "vários candidatos, o resultado traz a lista — mostre as opções "
                "e pergunte qual o usuário quer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome da pessoa (ou parte do nome).",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": (
                "Lista os contatos da agenda do usuário com nome, telefones, "
                "e-mails e empresa (agenda Google + aprendidos do e-mail), com "
                "filtro opcional. Use quando ele pedir para listar contatos, "
                "saber quem trabalha em uma empresa, ou ver quem tem telefone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Filtro opcional (nome, domínio ou empresa).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Quantidade máxima (opcional; default 10).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "Envia um e-mail do usuário. SÓ chame após mostrar o rascunho "
                "(destinatário, assunto e corpo) e receber confirmação clara."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Destinatário (endereço completo). Se ausente, usa o destinatário padrão.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Assunto/título do e-mail.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Corpo da mensagem (texto do e-mail).",
                    },
                },
                "required": ["subject", "body"],
            },
        },
    },
]


def _snippet(text: str, limit: int = None) -> str:
    """Trecho inicial útil do corpo — o suficiente para voz, cabe no payload."""
    limit = limit or EMAIL_SNIPPET_CHARS
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    return clean[:limit] + ("…" if len(clean) > limit else "")


def _strip_accents(text: str) -> str:
    """Minúsculas sem acentos — casamento genérico de nomes (cris→Cristina)."""
    return "".join(c for c in unicodedata.normalize("NFD", str(text or ""))
                   if unicodedata.category(c) != "Mn").lower().strip()


_SELF_TARGET = re.compile(r"\b(mim|eu mesmo|eu mesma|eu pr[oó]prio|minha pessoa)\b", re.I)


def resolve_recipient(target, user_id: str = "") -> dict:
    """Resolve o destinatário dito pelo usuário, do mais genérico ao específico.

    - "para mim"/"pra mim"/"eu mesmo" -> o próprio dono da skill (EMAIL_SENDER,
      o mesmo endereço que envia e recebe);
    - endereço com "@" -> direto;
    - nome (parcial, sem acentos: "cris" acha Cristina, "rafa" acha Rafael) ->
      agenda (cache 1º, mail no miss, grava no cache).
    Retorna {"tipo": mim|endereco|unico|varios|nenhum|vazio|erro, ...}. Nunca lança.
    """
    target = str(target or "").strip()
    if not target:
        return {"tipo": "vazio"}
    if _SELF_TARGET.search(target):
        own = os.getenv("EMAIL_SENDER") or ""
        if not own:
            return {"tipo": "erro", "erro": "endereço do dono não configurado (EMAIL_SENDER)"}
        return {"tipo": "mim", "email": own, "nome": "você mesmo"}
    if "@" in target:
        return {"tipo": "endereco", "email": target, "nome": target}
    result = _search_contact(target, user_id)
    cands = result.get("candidatos") or []
    if not cands and " " in target:
        # alvo com várias palavras (ex.: "rafael que ok"): tenta cada token,
        # mais longo primeiro — adapta a nomes em qualquer posição da fala.
        for token in sorted((t for t in target.split() if len(t) >= 3),
                            key=len, reverse=True):
            cands = _search_contact(token, user_id).get("candidatos") or []
            if cands:
                break
    if len(cands) == 1:
        return {"tipo": "unico", "email": cands[0].get("email", ""),
                "nome": cands[0].get("nome", "") or cands[0].get("email", "")}
    if cands:
        return {"tipo": "varios", "candidatos": cands[:3], "nome": target}
    return {"tipo": "nenhum", "nome": target}


def _search_contact(name: str, user_id: str) -> dict:
    """Agenda para envio — chat orquestra; para na PRIMEIRA fonte que achar.

    1) cache (contacts.json); 2) agenda Google (contacts.py — CardDAV/People);
    3) endereços da caixa (mail.py — IMAP). O que aprende, grava no cache.
    Retorna {"fonte": "cache"|"google"|"imap"|"nenhuma", "candidatos": [...]}.
    """
    name = str(name or "").strip()
    if not name:
        return {"fonte": "nenhuma", "total": 0, "candidatos": [],
                "erro": "informe o nome da pessoa"}
    hits = cache.search_contacts(user_id, name)
    if hits:
        return {"fonte": "cache", "total": len(hits), "candidatos": hits}

    # 2) agenda Google (contacts.py — CardDAV/People): só candidatos COM e-mail
    try:
        g = contacts.list_contacts(user_id, name, limit=5)
        if g.get("ok"):
            google_hits = [{"nome": c.get("nome", "") or c["emails"][0],
                            "email": c["emails"][0],
                            "telefones": c.get("telefones", [])}
                           for c in (g.get("data") or []) if c.get("emails")]
            if google_hits:
                merged = list(cache.load_contacts(user_id))
                known = {c.get("email", "").lower() for c in merged}
                for c in google_hits:
                    if c["email"].lower() not in known:
                        merged.append({"nome": c["nome"], "email": c["email"]})
                cache.save_contacts(user_id, merged)
                return {"fonte": "google", "total": len(google_hits),
                        "candidatos": google_hits}
    except Exception as e:
        logger.warning(f"agenda Google indisponível na busca (segue para mail): {e}")

    # 3) endereços da caixa (mail.py — IMAP), última fonte
    result = mail.list_contacts(term=name, limit=10)
    if not result.get("ok"):
        return {"fonte": "nenhuma", "total": 0, "candidatos": [],
                "erro": result.get("error", "falha ao buscar contatos")}
    found = result.get("data") or []
    if not found:
        return {"fonte": "nenhuma", "total": 0, "candidatos": []}
    merged = list(cache.load_contacts(user_id))
    known = {c.get("email", "").lower() for c in merged}
    for c in found:
        if c.get("email", "").lower() not in known:
            merged.append(c)
    cache.save_contacts(user_id, merged)
    return {"fonte": "imap", "total": len(found), "candidatos": found}


def _tool_payload(name: str, arguments: str, user_id: str = "") -> dict:
    """Execute a ferramenta de e-mail pedida pelo modelo e devolve payload compacto.

    mail.py nunca lança (envelope ok/error) — aqui só interpretamos o envelope.
    A listagem traz remetentes (rápida) e, por e-mail, assunto, data e trecho
    do conteúdo — o modelo fala mais que endereços, sem estourar o payload.
    `user_id` alimenta a agenda de contatos (cache por usuário) nas buscas.
    """
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"erro": "argumentos inválidos da ferramenta"}
    if name == "list_emails":
        result = mail.list_emails(args.get("limit") or EMAIL_LIST_LIMIT,
                                  with_body=True, order="urgency")
    elif name == "filter_emails":
        if str(args.get("date") or "").strip():
            result = mail.find_email(term=(args.get("term") or "").strip() or None,
                                     date=args.get("date"), limit=EMAIL_LIST_LIMIT)
        else:
            result = mail.filter_emails(args.get("term", ""), EMAIL_LIST_LIMIT)
    elif name == "list_addresses":
        result = mail.list_addresses(args.get("term") or None, EMAIL_LIST_LIMIT)
        if not result.get("ok"):
            return {"erro": result.get("error", "falha ao consultar e-mails")}
        addresses = result.get("data") or []
        return {"total": len(addresses), "enderecos": addresses}
    elif name == "search_contact":
        target = str(args.get("name") or "").strip()
        if _SELF_TARGET.search(target):
            res = resolve_recipient(target, user_id)
            if res.get("tipo") == "mim":
                return {"fonte": "eu", "total": 1,
                        "candidatos": [{"nome": "eu mesmo (o dono da skill)",
                                        "email": res["email"]}]}
            return {"fonte": "nenhuma", "total": 0, "candidatos": [],
                    "erro": res.get("erro", "não sei quem é o dono (EMAIL_SENDER ausente)")}
        return _search_contact(target, user_id)
    elif name == "list_contacts":
        result = contacts.list_contacts(user_id, args.get("term") or None,
                                        int(args.get("limit") or 10))
        if not result.get("ok"):
            return {"erro": result.get("error", "falha ao listar contatos")}
        data = result.get("data") or []
        return {"total": len(data), "contatos": [
            {"nome": c.get("nome", ""), "telefones": c.get("telefones", []),
             "emails": c.get("emails", []), "empresa": c.get("empresa", ""),
             "cargo": c.get("cargo", "")} for c in data]}
    elif name == "contact_phones":
        result = contacts.contact_phones(user_id, str(args.get("name") or "").strip())
        if not result.get("ok"):
            return {"erro": result.get("error", "contato não encontrado")}
        d = result.get("data") or {}
        payload = {"multi": d.get("multi", False), "nome": d.get("nome", ""),
                   "telefones": d.get("telefones", []), "emails": d.get("emails", []),
                   "empresa": d.get("empresa", ""), "fonte": d.get("fonte", "")}
        if d.get("multi"):
            payload["candidates"] = d.get("candidates", [])
            payload["instrucao"] = ("vários candidatos — liste NOME + TELEFONE de cada "
                                    "um e pergunte qual o usuário quer")
        return payload
    elif name == "read_email":
        uid = str(args.get("uid") or "").strip()
        position = args.get("position")
        if not uid and position:
            try:
                pos = max(1, int(position))
            except (TypeError, ValueError):
                return {"erro": "posição inválida: informe um número a partir de 1"}
            listing = mail.list_emails(pos, with_body=False)
            if not listing.get("ok"):
                return {"erro": listing.get("error", "falha ao localizar o e-mail")}
            data = listing.get("data") or []
            if pos > len(data):
                return {"erro": f"só encontrei {len(data)} e-mails; não existe posição {pos}"}
            uid = data[pos - 1].get("uid", "")
        if uid:
            result = mail.read_email(uid)
            if not result.get("ok"):
                return {"erro": result.get("error", "falha ao ler o e-mail")}
            m = result.get("data") or {}
            return {"uid": m.get("uid", uid), "remetente": m.get("from", ""),
                    "assunto": m.get("subject", ""), "data": m.get("date", ""),
                    "corpo_completo": _snippet(m.get("body", ""), 1500)}
        result = mail.find_email(term=args.get("term"), sender=args.get("sender"),
                                 subject=args.get("subject"), date=args.get("date"),
                                 limit=3)
        if not result.get("ok"):
            return {"erro": result.get("error", "falha ao ler o e-mail")}
        found = result.get("data") or []
        return {"total": len(found), "emails": [{
            "uid": m.get("uid", ""),
            "remetente": m.get("from", ""),
            "assunto": m.get("subject", ""),
            "data": m.get("date", ""),
            "corpo_completo": _snippet(m.get("body", ""), 1500),
        } for m in found]}
    elif name == "send_email":
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        to = str(args.get("to") or "").strip() or None
        if not subject or not body:
            return {"erro": "assunto e corpo são obrigatórios para enviar o e-mail"}
        result = mail.send_email(subject=subject, body=body, to_email=to)
        if not result.get("ok"):
            return {"erro": result.get("error", "falha ao enviar o e-mail")}
        return {"enviado": True, "mensagem": str(result.get("data", ""))}
    else:
        return {"erro": f"ferramenta desconhecida: {name}"}
    if not result.get("ok"):
        return {"erro": result.get("error", "falha ao consultar e-mails")}
    data = result.get("data") or []
    return {
        "total": len(data),
        "remetentes": [m.get("from", "") for m in data],
        "emails": [{
            "uid": m.get("uid", ""),
            "remetente": m.get("from", ""),
            "assunto": m.get("subject", ""),
            "data": m.get("date", ""),
            "urgencia": m.get("urgencia", ""),
            "conteudo": _snippet(m.get("body", "")),
        } for m in data],
    }


def sanitize_for_tts(text: str) -> str:
    """Strip markdown and symbols that Alexa would read aloud"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`{1,3}(.+?)`{1,3}", r"\1", text, flags=re.S)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"^[ \t]+|[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def load_history(user_id: str) -> list:
    """Restore previous chat memory for a user from the local cache.

    Memória de turnos além da sessão Alexa: o usuário retoma a conversa
    de onde parou. Falha do cache degrada para [] (só memória de sessão).
    """
    try:
        return list(cache.load_chat(user_id))
    except Exception as e:
        logger.error(f"Chat memory restore failed: {e}")
        return []


def save_history(user_id: str, history: list) -> bool:
    """Persist the chat history to the local cache (bounded by MAX_HISTORY).

    None é parâmetro inválido (fail-fast -> False); [] é estado válido.
    """
    try:
        if history is None:
            return False
        return cache.save_chat(user_id, list(history)[-MAX_HISTORY:])
    except Exception as e:
        logger.error(f"Chat memory persist failed: {e}")
        return False


def clear_history(user_id: str) -> bool:
    """Erase a user's persisted chat memory (cache.clear_chat)."""
    try:
        return cache.clear_chat(user_id)
    except Exception as e:
        logger.error(f"Chat memory clear failed: {e}")
        return False


def annotate_turn(user_id: str, query) -> bool:
    """Anota IMEDIATAMENTE a fala do usuário como abertura do turno.

    Crash-safe: a fala é gravada no cache ANTES da chamada ao GPT — se a
    Lambda morrer no meio do round-trip, o que o usuário falou já está
    anotado e o turno já começou. A resposta entra depois via save_history
    (que sobrescreve com a lista completa user+assistant, sem duplicar).
    """
    if not query or not str(query).strip():
        return False
    try:
        history = load_history(user_id)
        history.append({"role": "user", "content": str(query).strip(), "at": _now()})
        return cache.save_chat(user_id, history[-MAX_HISTORY:])
    except Exception as e:
        logger.error(f"Turn annotation failed: {e}")
        return False


EMAIL_DRAFT_INSTRUCTIONS = (
    "Você transforma uma mensagem falada em um e-mail curto e pronto para enviar. "
    "Responda EXATAMENTE neste formato, sem markdown e nada além dele:\n"
    "ASSUNTO: <título curto, no máximo 8 palavras>\n"
    "CORPO: <texto do e-mail em português, fiel à mensagem, sem markdown>"
)


def generate_email(message):
    """Gera rascunho de e-mail (assunto + corpo) via GPT.

    Retorna {"subject", "body"} ou None em falha (nunca lança).
    """
    if not message or not str(message).strip():
        return None
    try:
        client = client_gpt()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EMAIL_DRAFT_INSTRUCTIONS},
                {"role": "user", "content": str(message).strip()},
            ],
            max_completion_tokens=MAX_TOKENS,
        )
        text = response.choices[0].message.content or ""
        match = re.search(r"ASSUNTO:\s*(?P<s>.+?)\s*CORPO:\s*(?P<b>.+)",
                          text, flags=re.S | re.I)
        if not match:
            logger.error(f"Email draft parse failed: {text[:200]!r}")
            return None
        subject = sanitize_for_tts(match.group("s")).strip()
        body = sanitize_for_tts(match.group("b")).strip()
        if not body:
            return None
        return {"subject": subject, "body": body}
    except Exception as e:
        logger.error(f"Email generation failed: {e}")
        return None


def generate_subject(body) -> str:
    """Gera o título (assunto) a partir do CORPO do e-mail via GPT.

    Título curto (máx. 8 palavras), pronto para voz. Fallback local:
    suggest_subject (heurística) se o GPT falhar ou vier vazio. Nunca lança.
    """
    body = str(body or "").strip()
    if not body:
        return ""
    try:
        client = client_gpt()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": (
                    "Você gera o ASSUNTO de um e-mail a partir do corpo. Responda "
                    "com o título e nada além dele: curto, no máximo 8 palavras, "
                    "em português, sem markdown, sem aspas e sem ponto final.")},
                {"role": "user", "content": f"CORPO:\n{body[:4000]}"},
            ],
            max_completion_tokens=64,
            reasoning_effort="none",
        )
        subject = sanitize_for_tts(response.choices[0].message.content or "").strip()
        subject = subject.strip('"').rstrip(".").strip()
        return subject or mail.suggest_subject(body)
    except Exception as e:
        logger.error(f"generate_subject failed: {e}")
        return mail.suggest_subject(body)


def send_composed_email(message, from_email=None, to_email=None):
    """Gera o e-mail a partir da mensagem falada e envia via mail.py.

    Título gerado do CORPO (GPT; fallback heurístico) quando não falado.
    Retorna o envelope do mail.py: {"ok", "data"/"error"}.
    """
    draft = generate_email(message)
    if not draft:
        draft = {"subject": "", "body": str(message or "").strip()}
    if not draft["subject"]:
        draft["subject"] = generate_subject(draft["body"])
    if not draft["body"]:
        return {"ok": False, "error": "Mensagem vazia: nada para enviar"}
    return mail.send_email(subject=draft["subject"], body=draft["body"],
                           from_email=from_email, to_email=to_email)


# ---------------------------------------------------------------------------
# Ditado longo — anotação multi-turno/multi-sessão com síntese no fim
# ---------------------------------------------------------------------------

DICTATION_CHUNK_CHARS = int(os.getenv("DICTATION_CHUNK_CHARS", "600"))
DICTATION_TOTAL_CHARS = int(os.getenv("DICTATION_TOTAL_CHARS", "24000"))
DICTATION_MAX_MINUTES = int(os.getenv("DICTATION_MAX_MINUTES", "10"))


def dictation_start(user_id: str, mode: str = "email") -> dict:
    """Inicia (ou retoma) um rascunho no modo dado ('email'|'conversa').

    Recusa se houver rascunho ativo no OUTRO modo (diga pronto/sua vez antes).
    'started' marca o início da sentada — o limite de 10 min vale por sentada.
    Retorna {"ok", "mode", "chunks", "resumed"} ou {"ok": False, "error"}.
    """
    mode = mode if mode in ("email", "conversa") else "email"
    entry = cache.load_dictation_entry(user_id)
    chunks = entry.get("chunks", [])
    if chunks and entry.get("mode", "email") != mode:
        return {"ok": False, "error": (
            f"Há um {'ditado de e-mail' if entry.get('mode') == 'email' else 'modo de conversa'} "
            "em andamento — diga pronto (ou sua vez) antes de trocar de modo")}
    resumed = bool(chunks)
    if not cache.save_dictation(user_id, chunks, mode=mode, started=_now()):
        return {"ok": False, "error": "Falha ao iniciar o modo"}
    return {"ok": True, "mode": mode, "chunks": len(chunks), "resumed": resumed}


def dictation_append(user_id: str, text) -> dict:
    """Anota um trecho falado no rascunho persistente do usuário (cache).

    Limites: por trecho (DICTATION_CHUNK_CHARS), total (DICTATION_TOTAL_CHARS)
    e tempo da sentada (DICTATION_MAX_MINUTES desde dictation_start).
    Retorna {"ok": True, "chunks", "chars"} ou {"ok": False, "error"} — nunca lança.
    """
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "Trecho vazio"}
    entry = cache.load_dictation_entry(user_id)
    started = entry.get("started", "")
    if started:
        try:
            elapsed = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(started)).total_seconds()
            if elapsed > DICTATION_MAX_MINUTES * 60:
                return {"ok": False, "error": (
                    f"Tempo da sentada esgotado ({DICTATION_MAX_MINUTES} minutos) — "
                    "diga pronto (ou sua vez) para a resposta")}
        except (TypeError, ValueError):
            pass
    if len(text) > DICTATION_CHUNK_CHARS:
        return {"ok": False, "error": (
            f"Trecho longo demais ({len(text)} caracteres; máximo {DICTATION_CHUNK_CHARS}) — divida em partes")}
    chunks = entry.get("chunks", [])
    total = sum(len(c) for c in chunks) + len(text)
    if total > DICTATION_TOTAL_CHARS:
        return {"ok": False, "error": (
            f"Rascunho cheio ({total} de {DICTATION_TOTAL_CHARS} caracteres) — diga pronto para enviar o que já tem")}
    chunks.append(text)
    if not cache.save_dictation(user_id, chunks):
        return {"ok": False, "error": "Falha ao salvar a anotação"}
    return {"ok": True, "chunks": len(chunks), "chars": total}


def dictation_text(user_id: str) -> str:
    """Anotações acumuladas do usuário, na ordem falada."""
    return "\n".join(cache.load_dictation(user_id)).strip()


def dictation_clear(user_id: str) -> bool:
    """Descarta o rascunho do usuário (após envio ou pedido explícito)."""
    return cache.clear_dictation(user_id)


def dictation_synthesize(user_id: str):
    """Une todas as anotações e sintetiza (GPT) o e-mail pronto p/ confirmar.

    Título ausente é gerado do corpo (GPT; fallback heurístico). O rascunho
    permanece no cache até o envio — rejeitar a síntese não perde as anotações.
    Retorna {"subject", "body"} ou None (nunca lança).
    """
    full = dictation_text(user_id)
    if not full:
        return None
    draft = generate_email(full)
    if draft and not str(draft.get("subject") or "").strip():
        draft["subject"] = generate_subject(draft.get("body", ""))
    return draft


CONVERSATION_REPLY_MAX = int(os.getenv("CONVERSATION_REPLY_MAX", "500"))


def conversation_reply(user_id: str) -> str:
    """'Sua vez': o GPT pensa sobre TUDO anotado na conversa e responde.

    A resposta entra na transcrição (alternância usuário/Idiota) e a conversa
    segue aberta. Nunca lança; degrada com mensagem falada.
    """
    entry = cache.load_dictation_entry(user_id)
    chunks = entry.get("chunks", [])
    if not chunks:
        return "Não há conversa em andamento para eu responder."
    transcript = "\n".join(f"- {c}" for c in chunks)
    try:
        client = client_gpt()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                system_instructions(),
                {"role": "user", "content": (
                    "Estamos numa conversa contínua e pausada. Abaixo está a "
                    "transcrição de tudo que o usuário falou (e o que você já "
                    "respondeu). Produza sua resposta reflexiva e curta (2 a 5 "
                    "frases) sobre o conjunto, continuando o diálogo.\n\n"
                    f"TRANSCRIÇÃO:\n{transcript}")},
            ],
            max_completion_tokens=max(MAX_TOKENS, CONVERSATION_REPLY_MAX),
            reasoning_effort="none",
        )
        reply = sanitize_for_tts(response.choices[0].message.content or "")
        if not reply:
            return "Pensei, mas não cheguei a uma resposta. Continue falando."
        cache.save_dictation(user_id, chunks + [f"Idiota: {reply}"])
        return reply
    except Exception as e:
        logger.error(f"conversation_reply failed: {e}")
        return "Tive um problema para pensar agora. Continue falando."


# ---------------------------------------------------------------------------
# Memória de longo prazo: notas explícitas + busca/recall de turnos antigos
# ---------------------------------------------------------------------------

def _now() -> str:
    """Timestamp ISO (microssegundos) — anexado a cada turno no histórico."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def annotate(user_id: str, text: str) -> bool:
    """Salva uma nota explícita ("anote que ...") na memória persistente."""
    return cache.save_note(user_id, text)


def _date_window(low: str):
    """Janela [since, until) ISO derivada de palavras de data na pergunta."""
    day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if "anteontem" in low:
        return ((day0 - timedelta(days=2)).isoformat(), (day0 - timedelta(days=1)).isoformat())
    if "ontem" in low:
        return ((day0 - timedelta(days=1)).isoformat(), day0.isoformat())
    if "hoje" in low:
        return (day0.isoformat(), (day0 + timedelta(days=1)).isoformat())
    return None, None


def summarize_turns(turns: list, question: str) -> str:
    """Recebe turnos [{role, content, at}] + a pergunta e produz um INSUMO
    objetivo (fatos, decisões, datas, nomes) para responder à pergunta.
    Retorna 'NADA RELEVANTE' quando o histórico não ajuda. Nunca lança."""
    if not turns:
        return "NADA RELEVANTE"
    lines = [
        f"- ({(t.get('at') or 'sem data')[:16]}) {t.get('role', 'user')}: "
        f"{str(t.get('content', ''))[:400]}"
        for t in turns[:150]
    ]
    prompt = (
        "Você resume histórico de conversa para responder uma pergunta. "
        "Abaixo, a pergunta e os turnos anteriores (com data). Produza um INSUMO "
        "objetivo em português: fatos, decisões, datas e nomes que ajudem a "
        "responder; ignore o irrelevante; não invente nada. Se nada for "
        "relevante, escreva exatamente: NADA RELEVANTE.\n\n"
        f"PERGUNTA: {question}\n\nTURNOS:\n" + "\n".join(lines)
    )
    try:
        client = client_gpt()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=max(MAX_TOKENS, 500),
            reasoning_effort="none",
        )
        summary = (response.choices[0].message.content or "").strip()
        return summary or "NADA RELEVANTE"
    except Exception as e:
        logger.error(f"summarize_turns failed: {e}")
        return "NADA RELEVANTE"


def answer_from_memory(user_id: str, question: str) -> str:
    """Recall: busca turnos + notas (com janela de data da pergunta), resume e
    responde. Degrada para o insumo bruto se a 2ª chamada falhar. Nunca lança."""
    since, until = _date_window(question.lower())
    turns = cache.search_turns(user_id, since=since, until=until, limit=200)
    notes = cache.search_notes(user_id, since=since, until=until, limit=100)
    if not turns and not notes:
        return "Não encontrei nada na memória sobre isso."
    items = ([{"role": t["role"], "content": t["content"], "at": t["at"]} for t in turns]
             + [{"role": "nota", "content": n["text"], "at": n["at"]} for n in notes])
    insumo = summarize_turns(items, question)
    if "NADA RELEVANTE" in insumo.upper():
        return "Não encontrei nada relevante na memória sobre isso."
    try:
        client = client_gpt()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                system_instructions(),
                {"role": "user", "content": (
                    "Responda em voz, curto, usando APENAS o insumo abaixo "
                    "(memória de conversas anteriores do usuário).\n\n"
                    f"INSUMO: {insumo}\n\nPERGUNTA: {question}")},
            ],
            max_completion_tokens=MAX_TOKENS,
            reasoning_effort="none",
        )
        return sanitize_for_tts(response.choices[0].message.content or insumo)
    except Exception as e:
        logger.error(f"answer_from_memory final failed: {e}")
        return sanitize_for_tts(insumo)


def generate_gpt_response(history: list, query, user_id: str = "") -> str:
    """Generate a voice-friendly GPT response with bounded session history.

    `history` é a lista de mensagens da sessão (mutada in place pelo chat);
    infraestrutura (Alexa) é responsável por criar/persistir essa lista.
    Perguntas sobre e-mail disparam ferramentas (mail.py): a troca de tool
    calls vive em mensagens locais — só o turno user/assistant entra na
    memória persistida (replay de tool messages quebraria a API).

    Memória de longo prazo (precisa de user_id): "anote/anota/lembre que X"
    salva nota persistente; "o que conversamos/anotamos..." e "lembra ..."
    buscam turnos/notas (com janela de data: ontem/hoje/anteontem) e respondem
    via insumo (summarize_turns). Turnos ganham timestamp "at"; a API do GPT
    recebe só {role, content}.
    """
    if not query or not query.strip():
        return "Desculpe, não recebi sua pergunta."

    q = str(query).strip()
    low = q.lower()

    m_note = re.match(r"^\s*(?:anote|anota|lembre)\b\s*(?:que\s+|de\s+|:)?\s*(.+)", q, re.I | re.S)
    m_recall = (re.search(r"\b(?:o que|quais?|quando|quem)\b.{0,80}?\b"
                          r"(?:conversamos|falamos|anotamos|anotei|combinamos)\b", low)
                or re.match(r"^\s*lembra\b", low))

    if user_id and m_note:
        note = m_note.group(1).strip()
        ok_note = cache.save_note(user_id, note)
        reply = "Anotado." if ok_note else "Não consegui salvar a anotação."
        history.append({"role": "user", "content": q, "at": _now()})
        history.append({"role": "assistant", "content": reply, "at": _now()})
        del history[:-MAX_HISTORY]
        return reply

    if user_id and m_recall:
        reply = answer_from_memory(user_id, q)
        history.append({"role": "user", "content": q, "at": _now()})
        history.append({"role": "assistant", "content": reply, "at": _now()})
        del history[:-MAX_HISTORY]
        return reply

    try:
        client = client_gpt()
    except ValueError as ve:
        logger.error(f"Configuration error: {ve}")
        return "Erro de configuração. Verifique a chave da API no ambiente."

    try:
        history.append({"role": "user", "content": q, "at": _now()})
        # A API aceita apenas {role, content} — timestamps ficam no histórico local.
        messages = [system_instructions()] + [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in history[-MAX_HISTORY:]
        ]

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=EMAIL_TOOLS,
            tool_choice="auto",
            max_completion_tokens=MAX_TOKENS,
            # gpt-5.6-luna: function tools em /chat/completions exigem reasoning_effort none
            reasoning_effort="none",
        )
        message = response.choices[0].message

        if getattr(message, "tool_calls", None):
            messages.append(message)
            for call in message.tool_calls:
                payload = _tool_payload(call.function.name, call.function.arguments,
                                        user_id=user_id)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                })
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_completion_tokens=max(MAX_TOKENS, LISTING_MAX_TOKENS),
                # mesma razão da 1ª chamada: reasoning consumiria o orçamento de voz
                reasoning_effort="none",
            )
            message = response.choices[0].message

        reply = sanitize_for_tts(message.content or "")
        history.append({"role": "assistant", "content": reply, "at": _now()})
        del history[:-MAX_HISTORY]
        return reply

    except Exception as e:
        logger.error(f"Error generating response: {e}")
        return "Desculpe, houve um problema ao processar sua solicitação."
