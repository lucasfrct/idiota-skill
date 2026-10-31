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
from mail import send_email

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configuration (environment-driven)
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT") or EMAIL_SENDER
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Mensagem do Idiota")

ASK_REPROMPT = "Você pode fazer uma pergunta, pedir para enviar um e-mail, ou dizer sair."


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

    Supports the pattern 'assunto <subject> mensagem <body>';
    anything else goes entirely to the body with the default subject.
    """
    match = re.search(
        r"assunto\s+(?P<subject>.+?)\s*,?\s+(?:mensagem|texto|dizendo)\s+(?P<body>.+)",
        message,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group("subject").strip(), match.group("body").strip()
    return EMAIL_SUBJECT, message.strip()


def get_email_flow(handler_input):
    """Email flow state stored in session attributes.

    None = inactive; 'awaiting_body' = waiting for the message;
    dict = message captured, awaiting user confirmation.
    """
    session_attr = handler_input.attributes_manager.session_attributes
    return session_attr.get("email_flow")


def clear_email_flow(handler_input):
    handler_input.attributes_manager.session_attributes.pop("email_flow", None)


def ask_email_confirmation(handler_input, subject, body):
    """Store the pending email and ask the user to confirm"""
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

    result = send_email(EMAIL_SENDER, EMAIL_RECIPIENT, flow["subject"], flow["body"])
    logger.info(f"Email attempt result: {result}")
    return result["data"] if result.get("ok") else result.get("error", "Falha ao enviar o e-mail")


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        clear_email_flow(handler_input)
        handler_input.attributes_manager.session_attributes["chat_history"] = []
        speak_output = (
            "Olá! Sou o Idiota. Pode fazer sua pergunta, "
            "ou pedir para eu enviar um e-mail."
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
            speak_output = generate_gpt_response(history, query)

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
