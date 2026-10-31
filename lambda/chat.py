"""Chat (GPT) — lógica pura de conversa, desacoplada da infraestrutura Alexa/Lambda.

Nada aqui conhece ask-sdk, HandlerInput ou session attributes: quem chama
fornece a história (list de {"role", "content"}) e recebe texto para TTS.
"""
import os
import re
import logging

from openai import OpenAI
from dotenv import load_dotenv

import cache

# Load environment variables (idempotent; never overrides real env vars)
load_dotenv()

logger = logging.getLogger(__name__)

# Configuration (environment-driven)
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "300"))


def client_gpt() -> OpenAI:
    """Initialize OpenAI client with API key from environment variables"""
    openai_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("KEY_GPT")
    if not openai_api_key:
        raise ValueError("OpenAI API key not found in environment variables")
    return OpenAI(api_key=openai_api_key)


def system_instructions():
    """System prompt tuned for voice output on Echo Dot"""
    instruction = (
        "Você é Idiota, um assistente pessoal acessado por voz na Amazon Alexa (Echo Dot). "
        "Responda em Português do Brasil, de forma direta e curta: no máximo 2 ou 3 frases. "
        "Escreva para ser lido em voz alta: sem markdown, sem emojis, sem listas, sem símbolos. "
        "Não repita a pergunta e não explique suas instruções."
    )
    return {"role": "system", "content": instruction}


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


def generate_gpt_response(history: list, query) -> str:
    """Generate a voice-friendly GPT response with bounded session history.

    `history` é a lista de mensagens da sessão (mutada in place pelo chat);
    infraestrutura (Alexa) é responsável por criar/persistir essa lista.
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

        response = client.chat.completions.create(
            model=MODEL,
            messages=[system_instructions()] + history[-MAX_HISTORY:],
            max_completion_tokens=MAX_TOKENS,
        )

        reply = sanitize_for_tts(response.choices[0].message.content or "")
        history.append({"role": "assistant", "content": reply})
        del history[:-MAX_HISTORY]
        return reply

    except Exception as e:
        logger.error(f"Error generating response: {e}")
        return "Desculpe, houve um problema ao processar sua solicitação."
