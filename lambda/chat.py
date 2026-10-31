"""Chat (GPT) — lógica pura de conversa, desacoplada da infraestrutura Alexa/Lambda.

Nada aqui conhece ask-sdk, HandlerInput ou session attributes: quem chama
fornece a história (list de {"role", "content"}) e recebe texto para TTS.
"""
import json
import os
import re
import logging

from openai import OpenAI
from dotenv import load_dotenv

import cache
import mail

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
        "Você se lembra das últimas mensagens da conversa e tem ferramenta de e-mail: "
        "quando o usuário pedir para ver, listar ou procurar e-mails, chame list_emails "
        "ou filter_emails antes de responder; para perguntas sobre QUEM mandou e-mails "
        "ou sobre endereços/contatos, chame list_addresses. Ao listar, cite remetente, "
        "assunto e resuma o conteúdo de cada e-mail, do mais recente para o mais antigo, "
        "sem inventar e-mails. "
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
                "Filtra os e-mails recentes por um termo no remetente ou no assunto. "
                "Use quando o usuário pedir e-mails de alguém ou sobre algo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Termo a buscar (nome, domínio ou assunto).",
                    },
                },
                "required": ["term"],
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
]


def _snippet(text: str, limit: int = None) -> str:
    """Trecho inicial útil do corpo — o suficiente para voz, cabe no payload."""
    limit = limit or EMAIL_SNIPPET_CHARS
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    return clean[:limit] + ("…" if len(clean) > limit else "")


def _tool_payload(name: str, arguments: str) -> dict:
    """Execute a ferramenta de e-mail pedida pelo modelo e devolve payload compacto.

    mail.py nunca lança (envelope ok/error) — aqui só interpretamos o envelope.
    A listagem traz remetentes (rápida) e, por e-mail, assunto, data e trecho
    do conteúdo — o modelo fala mais que endereços, sem estourar o payload.
    """
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"erro": "argumentos inválidos da ferramenta"}
    if name == "list_emails":
        result = mail.list_emails(args.get("limit") or EMAIL_LIST_LIMIT, with_body=True)
    elif name == "filter_emails":
        result = mail.filter_emails(args.get("term", ""), EMAIL_LIST_LIMIT)
    elif name == "list_addresses":
        result = mail.list_addresses(args.get("term") or None, EMAIL_LIST_LIMIT)
        if not result.get("ok"):
            return {"erro": result.get("error", "falha ao consultar e-mails")}
        addresses = result.get("data") or []
        return {"total": len(addresses), "enderecos": addresses}
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


def send_composed_email(message, from_email=None, to_email=None):
    """Gera o e-mail a partir da mensagem falada e envia via mail.py.

    Título vem do GPT; se faltar, é sugerido do corpo no fim do fluxo.
    Retorna o envelope do mail.py: {"ok", "data"/"error"}.
    """
    draft = generate_email(message)
    if not draft:
        draft = {"subject": "", "body": str(message or "").strip()}
    if not draft["subject"]:
        draft["subject"] = mail.suggest_subject(draft["body"])
    if not draft["body"]:
        return {"ok": False, "error": "Mensagem vazia: nada para enviar"}
    return mail.send_email(subject=draft["subject"], body=draft["body"],
                           from_email=from_email, to_email=to_email)


def generate_gpt_response(history: list, query) -> str:
    """Generate a voice-friendly GPT response with bounded session history.

    `history` é a lista de mensagens da sessão (mutada in place pelo chat);
    infraestrutura (Alexa) é responsável por criar/persistir essa lista.
    Perguntas sobre e-mail disparam ferramentas (mail.py): a troca de tool
    calls vive em mensagens locais — só o turno user/assistant entra na
    memória persistida (replay de tool messages quebraria a API).
    """
    if not query or not query.strip():
        return "Desculpe, não recebi sua pergunta."

    try:
        client = client_gpt()
    except ValueError as ve:
        logger.error(f"Configuration error: {ve}")
        return "Erro de configuração. Verifique a chave da API no ambiente."

    try:
        history.append({"role": "user", "content": query.strip()})
        messages = [system_instructions()] + history[-MAX_HISTORY:]

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
                payload = _tool_payload(call.function.name, call.function.arguments)
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
        history.append({"role": "assistant", "content": reply})
        del history[:-MAX_HISTORY]
        return reply

    except Exception as e:
        logger.error(f"Error generating response: {e}")
        return "Desculpe, houve um problema ao processar sua solicitação."
