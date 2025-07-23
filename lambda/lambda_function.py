import os
import logging
import ask_sdk_core.utils as ask_utils

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput

from ask_sdk_model import Response

from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Global variables
messages = []
MODEL = "gpt-4o-mini"


def client_gpt() -> OpenAI:
    """Initialize OpenAI client with API key from environment variables"""
    openai_api_key = os.getenv('OPENAI_API_KEY') or os.getenv('KEY_GPT')
    if not openai_api_key:
        raise ValueError("OpenAI API key not found in environment variables")
    
    client = OpenAI(api_key=openai_api_key)
    return client


# Instruçõers para o sistema
def system_instructions():
    instruction = """
        Você é um assistente pessoal de nome Altair. 
        Responda de forma, direta e curta. 
        Responda em Português do Brasil.
        Não tente explicar sua resposta.
        Não repita a pergunta feita.
    """
    return { "role": "system", "content": instruction, }


def generate_gpt_response(query):
    """Generate response from OpenAI GPT model with proper error handling"""
    if not query or not query.strip():
        return "Desculpe, não recebi sua pergunta."
    
    try:
        messages.append({"role": "user", "content": query.strip()})
        
        response = client_gpt().chat.completions.create(
            model=MODEL, 
            messages=messages, 
            max_tokens=700, 
            temperature=0.8
        )
        
        reply = response.choices[0].message.content
        messages.append({"role": "assistant", "content": reply})
        return reply
        
    except ValueError as ve:
        logger.error(f"Configuration error: {str(ve)}")
        return "Erro de configuração. Verifique as credenciais da API."
    except Exception as e:
        logger.error(f"Error generating response: {str(e)}")
        return "Desculpe, houve um problema ao processar sua solicitação."
        
    
class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool

        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = ("Oi!")

        return (
            handler_input.response_builder.speak(speak_output)
            .ask(speak_output)
            .response
        )


class GptQueryIntentHandler(AbstractRequestHandler):
    """Handler for GPT query intent"""
    
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("GptQueryIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        try:
            slots = handler_input.request_envelope.request.intent.slots
            query = slots["query"].value if slots and "query" in slots and slots["query"].value else None
            
            if not query:
                speak_output = "Desculpe, não entendi sua pergunta. Pode repetir?"
            else:
                response = generate_gpt_response(query)
                speak_output = response
                
        except Exception as e:
            logger.error(f"Error in GptQueryIntentHandler: {str(e)}")
            speak_output = "Desculpe, houve um problema ao processar sua pergunta."

        return (
            handler_input.response_builder.speak(speak_output)
            .ask("Você pode fazer uma nova pergunta ou dizer sair.")
            .response
        )


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "Diga?"

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
        speak_output = "Até mais!"

        return handler_input.response_builder.speak(speak_output).response


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response

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


messages.append(system_instructions())

sb = SkillBuilder()

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(GptQueryIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())

sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()