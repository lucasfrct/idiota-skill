import os
import re
import logging

import ask_sdk_core.utils as ask_utils

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput

from ask_sdk_model import Response
from ask_sdk_model.dialog import ElicitSlotDirective
from ask_sdk_model import Intent, Slot

from dotenv import load_dotenv

from chat import generate_gpt_response
import chat
import cache
from mail import send_email, suggest_subject

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configuration (environment-driven)
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT") or EMAIL_SENDER

ASK_REPROMPT = "Você pode fazer uma pergunta, pedir para enviar um e-mail, ou dizer sair."


def user_id(handler_input) -> str:
    """Stable per-user id — the key for local chat memory (sessions change)."""
    try:
        return handler_input.request_envelope.session.user.user_id or ""
    except AttributeError:
        return ""


def get_history(handler_input) -> list:
    """Per-session chat history stored in session attributes"""
    session_attr = handler_input.attributes_manager.session_attributes
    if "chat_history" not in session_attr:
        session_attr["chat_history"] = []
    return session_attr["chat_history"]


def spoken_email(address: str) -> str:
    """Make an email address readable by TTS (arroba/ponto instead of @/.)"""
    return address.replace("@", " arroba ").replace(".", " ponto ")


def parse_subject_body(message: str):
    """Extract subject and body from a spoken message.

    Supports the pattern 'assunto <subject> mensagem <body>'; anything else
    goes entirely to the body with an empty subject — the title is then
    SUGGESTED from the body at the end of the flow (ask_email_confirmation),
    once the body is already known.
    """
    match = re.search(
        r"assunto\s+(?P<subject>.+?)\s*,?\s+(?:mensagem|texto|dizendo)\s+(?P<body>.+)",
        message,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group("subject").strip(), match.group("body").strip()
    return "", message.strip()


def get_email_flow(handler_input):
    """Email flow state stored in session attributes.

    None = inactive; 'awaiting_body' = waiting for the message;
    dict = message captured, awaiting user confirmation.
    """
    session_attr = handler_input.attributes_manager.session_attributes
    return session_attr.get("email_flow")


def is_dictating(handler_input) -> bool:
    """Modo ditado é POR SESSÃO (explícito); o conteúdo fica no cache e
    sobrevive a sessões — retomar = dizer 'ditar um e-mail' de novo."""
    session_attr = handler_input.attributes_manager.session_attributes
    return bool(session_attr.get("dictating"))


def acknowledge_chunk(handler_input, app):
    """Fala curta pós-anotação + mantém a sessão aberta para o próximo trecho."""
    speak_output = (
        f"Anotado, parte {app['chunks']}. Continue ditando, ou diga pronto."
        if app.get("ok") else app.get("error", "Não consegui anotar.")
    )
    return (
        handler_input.response_builder.speak(speak_output)
        .ask("Continuo anotando. Continue.")
        .response
    )


def clear_email_flow(handler_input):
    handler_input.attributes_manager.session_attributes.pop("email_flow", None)


def ask_email_confirmation(handler_input, subject, body):
    """Store the pending email and ask the user to confirm.

    Título exigido no envio: se o usuário não disse um, sugere do corpo aqui —
    fim do fluxo, quando o corpo já existe — e fala a sugestão na confirmação.
    """
    subject = str(subject or "").strip()
    if not subject:
        subject = suggest_subject(body)
    handler_input.attributes_manager.session_attributes["email_flow"] = {
        "subject": subject,
        "body": body,
    }
    speak_output = (
        f"Vou enviar um e-mail para {spoken_email(EMAIL_RECIPIENT)}, "
        f"com o assunto {subject} e a mensagem: {body}. Confirma?"
    )
    return (
        handler_input.response_builder.speak(speak_output)
        .ask("Diga sim para enviar, ou não para cancelar.")
        .response
    )


def elicit_email_body(handler_input):
    """Ask the user for the email message via the dialog model"""
    speak_output = "O que você quer enviar no e-mail?"
    message_slot = Slot(name="message")
    updated_intent = Intent(name="SendEmailIntent", slots={"message": message_slot})
    return (
        handler_input.response_builder.speak(speak_output)
        .ask(speak_output)
        .add_directive(
            ElicitSlotDirective(
                slot_to_elicit="message",
                updated_intent=updated_intent,
            )
        )
        .response
    )


def send_pending_email(handler_input):
    """Send the confirmed email and return the spoken result"""
    flow = get_email_flow(handler_input)
    clear_email_flow(handler_input)

    if not flow or "body" not in flow:
        return "Não há e-mail pendente para enviar."

    if not EMAIL_SENDER or not EMAIL_RECIPIENT:
        logger.error("Email flow attempted without EMAIL_SENDER/EMAIL_RECIPIENT")
        return "O envio de e-mail não está configurado no ambiente."

    # Título é exigência do send_email: fallback de sugestão (corpo já conhecido)
    subject = str(flow.get("subject") or "").strip() or suggest_subject(flow.get("body", ""))
    result = send_email(subject=subject, body=flow.get("body", ""),
                        from_email=EMAIL_SENDER, to_email=EMAIL_RECIPIENT)
    logger.info(f"Email attempt result: {result}")

    # Frente 2 do cache: metadados do envio + lista de e-mails disponíveis
    ok = bool(result.get("ok")) if isinstance(result, dict) else False
    info = (result.get("data") or result.get("error") or "") if isinstance(result, dict) else str(result)
    cache.record_email(
        to_email=EMAIL_RECIPIENT,
        subject=subject,
        body=flow.get("body", ""),
        ok=ok,
        info=str(info),
    )

    # Ditado enviado com sucesso: o rascunho cumpriu seu papel — descarta.
    if ok:
        chat.dictation_clear(user_id(handler_input))

    # Fala derivada do envelope (send_email nunca lança; nunca falar dict)
    if ok:
        return str(info) or f"E-mail enviado com sucesso para {EMAIL_RECIPIENT}."
    return str(info) or "Não consegui enviar o e-mail."


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        clear_email_flow(handler_input)
        # Sessão nova = modo ditado desligado (o rascunho persiste no cache —
        # retomar é explícito: "ditar um e-mail").
        handler_input.attributes_manager.session_attributes.pop("dictating", None)
        # Memória de turnos: restaura a conversa anterior do usuário (cache
        # local); se não houver, começa vazia — degradação suave.
        handler_input.attributes_manager.session_attributes["chat_history"] = (
            chat.load_history(user_id(handler_input))
        )
        speak_output = (
            "Olá! Sou o Idiota. Pode fazer sua pergunta, "
            "ditar um e-mail longo, ou pedir para eu enviar um e-mail."
        )

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(speak_output)
            .response
        )


class GptQueryIntentHandler(AbstractRequestHandler):
    """Handler for GPT queries; also captures the email body when
    the skill is waiting for it (safety net if elicitation misses)."""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("GptQueryIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        try:
            query = ask_utils.get_slot_value(handler_input=handler_input, slot_name="query")

            # Modo ditado: fala livre é ANOTAÇÃO, não pergunta ao GPT.
            if is_dictating(handler_input):
                if query:
                    app = chat.dictation_append(user_id(handler_input), query)
                    return acknowledge_chunk(handler_input, app)
                speak_output = "Não ouvi o trecho. Fale de novo."
                return (
                    handler_input.response_builder.speak(speak_output)
                    .ask("Continuo anotando. Continue.")
                    .response
                )

            if get_email_flow(handler_input) == "awaiting_body":
                clear_email_flow(handler_input)
                if query:
                    subject, body = parse_subject_body(query)
                    return ask_email_confirmation(handler_input, subject, body)
                speak_output = "Não recebi a mensagem. Peça o e-mail de novo, por favor."
                return (
                    handler_input.response_builder.speak(speak_output)
                    .ask(ASK_REPROMPT)
                    .response
                )

            if not query:
                speak_output = "Desculpe, não entendi sua pergunta. Pode repetir?"
                return (
                    handler_input.response_builder.speak(speak_output)
                    .ask(ASK_REPROMPT)
                    .response
                )

            history = get_history(handler_input)
            uid = user_id(handler_input)
            speak_output = generate_gpt_response(history, query, user_id=uid)
            # Persiste a memória de turnos do usuário no cache local
            chat.save_history(uid, history)

        except Exception as e:
            logger.error(f"Error in GptQueryIntentHandler: {e}")
            speak_output = "Desculpe, houve um problema ao processar sua pergunta."

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(ASK_REPROMPT)
            .response
        )


class SendEmailIntentHandler(AbstractRequestHandler):
    """Handler for the email flow: elicit the message, then confirm before sending"""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("SendEmailIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        try:
            message = ask_utils.get_slot_value(handler_input=handler_input, slot_name="message")

            if not message:
                # Dialog model: elicit the message in the next turn
                handler_input.attributes_manager.session_attributes["email_flow"] = "awaiting_body"
                return elicit_email_body(handler_input)

            subject, body = parse_subject_body(message)
            return ask_email_confirmation(handler_input, subject, body)

        except Exception as e:
            logger.error(f"Error in SendEmailIntentHandler: {e}")
            speak_output = "Desculpe, houve um problema com o envio do e-mail."
            return (
                handler_input.response_builder.speak(speak_output)
                .ask(ASK_REPROMPT)
                .response
            )


class StartDictationIntentHandler(AbstractRequestHandler):
    """Inicia (ou retoma) o ditado longo de um e-mail, entre sessões."""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("StartDictationIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        try:
            clear_email_flow(handler_input)
            handler_input.attributes_manager.session_attributes["dictating"] = True
            uid = user_id(handler_input)
            chunks = cache.load_dictation(uid)
            if chunks:
                speak_output = (
                    f"Você já tinha {len(chunks)} anotação(ões) guardadas. "
                    "Continuo de onde paramos — fale a próxima parte."
                )
            else:
                speak_output = (
                    "Modo ditado ativado. Fale o conteúdo do e-mail em partes, "
                    "eu anoto tudo e não perco nada entre os turnos. "
                    "Quando terminar, diga pronto."
                )
            return (
                handler_input.response_builder.speak(speak_output)
                .ask("Estou anotando. Continue.")
                .response
            )
        except Exception as e:
            logger.error(f"Error in StartDictationIntentHandler: {e}")
            speak_output = "Não consegui iniciar o ditado."
            return (
                handler_input.response_builder.speak(speak_output)
                .ask(ASK_REPROMPT)
                .response
            )


class DictationChunkIntentHandler(AbstractRequestHandler):
    """Captura um trecho dito com frase portadora (anote/continuar)."""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("DictationChunkIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        try:
            handler_input.attributes_manager.session_attributes["dictating"] = True
            text = ask_utils.get_slot_value(handler_input=handler_input, slot_name="text")
            if not text:
                speak_output = "Não ouvi o trecho. Fale de novo."
                return (
                    handler_input.response_builder.speak(speak_output)
                    .ask("Continuo anotando. Continue.")
                    .response
                )
            app = chat.dictation_append(user_id(handler_input), text)
            return acknowledge_chunk(handler_input, app)
        except Exception as e:
            logger.error(f"Error in DictationChunkIntentHandler: {e}")
            speak_output = "Problema ao anotar o trecho."
            return (
                handler_input.response_builder.speak(speak_output)
                .ask("Tente de novo.")
                .response
            )


class FinishDictationIntentHandler(AbstractRequestHandler):
    """Encerra o ditado: sintetiza (GPT) as anotações e pede confirmação."""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("FinishDictationIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        try:
            uid = user_id(handler_input)
            chunks = cache.load_dictation(uid)
            if not chunks:
                speak_output = "Não há ditado em andamento. Diga: ditar um e-mail, para começar."
                return (
                    handler_input.response_builder.speak(speak_output)
                    .ask(ASK_REPROMPT)
                    .response
                )
            draft = chat.dictation_synthesize(uid)
            if not draft or not draft.get("body"):
                speak_output = (
                    "Não consegui organizar as anotações num e-mail. "
                    "Continue ditando, ou diga sair."
                )
                return (
                    handler_input.response_builder.speak(speak_output)
                    .ask("Continue ditando.")
                    .response
                )
            # Modo ditado encerra na sessão; o rascunho fica no cache até o
            # envio — rejeitar a confirmação não perde as anotações.
            handler_input.attributes_manager.session_attributes["dictating"] = False
            return ask_email_confirmation(handler_input, draft.get("subject", ""), draft["body"])
        except Exception as e:
            logger.error(f"Error in FinishDictationIntentHandler: {e}")
            speak_output = "Problema ao organizar o ditado. Tente de novo."
            return (
                handler_input.response_builder.speak(speak_output)
                .ask("Tente de novo.")
                .response
            )


class YesIntentHandler(AbstractRequestHandler):
    """Confirms the pending email (or acknowledges no pending action)"""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.YesIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        flow = get_email_flow(handler_input)
        if isinstance(flow, dict) and "body" in flow:
            speak_output = send_pending_email(handler_input)
        else:
            speak_output = "Ok. Pode fazer sua pergunta."

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(ASK_REPROMPT)
            .response
        )


class NoIntentHandler(AbstractRequestHandler):
    """Cancels the pending email (or acknowledges)"""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.NoIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        flow = get_email_flow(handler_input)
        clear_email_flow(handler_input)
        if isinstance(flow, dict) and "body" in flow:
            if cache.load_dictation(user_id(handler_input)):
                speak_output = (
                    "Tudo bem, não enviei. Suas anotações do ditado continuam "
                    "guardadas — continue ditando mais tarde, ou diga pronto."
                )
            else:
                speak_output = "Tudo bem, não enviei o e-mail."
        else:
            speak_output = "Ok. Pode fazer sua pergunta."

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(ASK_REPROMPT)
            .response
        )


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = (
            "Você pode me fazer perguntas sobre qualquer assunto. "
            "Para enviar um e-mail, diga: enviar e-mail, com a mensagem. "
            "Antes de enviar, eu confirmo com você. O que você quer saber?"
        )

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(speak_output)
            .response
        )


class FallbackIntentHandler(AbstractRequestHandler):
    """Handles utterances that don't match any intent"""

    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "Desculpe, não entendi. Você pode fazer uma pergunta ou dizer sair."

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(speak_output)
            .response
        )


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.CancelIntent")(
            handler_input
        ) or ask_utils.is_intent_name("AMAZON.StopIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        clear_email_flow(handler_input)
        speak_output = "Até mais!"

        return handler_input.response_builder.speak(speak_output).response


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        reason = handler_input.request_envelope.request.reason
        logger.info(f"Session ended. Reason: {reason}")

        # Any cleanup logic goes here.
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> bool
        return True

    def handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> Response
        logger.error(exception, exc_info=True)

        speak_output = "Desculpe, não posso te entender."

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(speak_output)
            .response
        )


sb = SkillBuilder()

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(StartDictationIntentHandler())
sb.add_request_handler(DictationChunkIntentHandler())
sb.add_request_handler(FinishDictationIntentHandler())
sb.add_request_handler(GptQueryIntentHandler())
sb.add_request_handler(SendEmailIntentHandler())
sb.add_request_handler(YesIntentHandler())
sb.add_request_handler(NoIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())

sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
