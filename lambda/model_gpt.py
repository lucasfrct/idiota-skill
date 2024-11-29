
import os
import random
from openai import OpenAI
from dotenv import load_dotenv

import knowledgebase as KnowledgeBase 
import mail as MailSMTP

load_dotenv()

messages = []
MODEL = "gpt-4o-mini"

record_actions = ["anote na memória", "fixe isso", "armazene na memória"]
record_responses = ["Ok!", "Entendi, guardado.", "Ok, armazenado.", "Entendi, fixado."]

remember_actions = ["procure na meméria por", "ache na meméria", "lembre de", "me fale sobre", "Conhece sobre"]
remenber_responses = ["Ok!", "Entendi, guardado.", "Ok, armazenado.", "Entendi, fixado."]

email_actions = ["gere um email", "criar um email", "escreva um email"]
email_responses = ["Já enviei.", "Email enviado", "Já tá feito"]


def client_gpt()-> OpenAI:
    openai_api_key = os.getenv('KEY_GPT')
    client = OpenAI(api_key=openai_api_key)
    return client


def system_instructions():
    instruction = """
        Você é um assistente pessoal de nome Altair. 
        Responda de forma, direta e curta. 
        Responda em Português do Brasil.
        Não tente explicar sua resposta.
        Não repita a pergunda feita.
    """
    return { "role": "system", "content": instruction, }


def to_ask(question: str, messages = None):
    if messages is None:
        messages = []
    messages.append({"role": "user", "content": question})
    response = client_gpt().chat.completions.create(model=MODEL, messages=messages, temperature=0.8)
    return response.choices[0].message.content


def generate_gpt_response(query: str):
    try:
        
        if any(action in discover_the_intention(query) for action in email_actions):
            return action_email(query)
        
        if any(action in discover_the_intention(query) for action in record_actions):
            return action_save(query)

        reply = to_ask(query, messages)
        messages.append({ "role": "assistant", "content": f"{len(messages)}. {reply}" })
        return reply
    except Exception as e:
        return f"Erro ao gerar resposta: {str(e)}"
    

def discover_the_intention(question: str):
    
    actions = ""
    
    for ac in record_actions:
        actions += f"- {ac}\n"
        
    for a in email_actions:
        actions += f"- {a}\n"
        
    for act in remember_actions:
        actions += f"- {act}\n"
        
    prompt = f"""
        /shorten
        Verifique a intenção para ação da pergunta abaixo.
        Responda somente com a ação encontrada em uma das opções.
        Caso não encontrar uma opção, responda com texto vazio.
        As opções de categorias são:
        {actions}
        
        Com base na seguinte pergunta, categorize: {question}
    """
    response = to_ask(prompt)
    
    return response


def action_save(question: str):  
    KnowledgeBase.save(question)
    return random.choice(record_responses)


def action_email(question: str):
    
    addressee_email = extract_email_addressee(question)
    if(addressee_email == ""):
        return "Não foi possível identificar o destinatário."
    
    addressee_name = extract_name_addresse(question)
  
    sender_email = extract_email_sender(question)
    if(sender_email == ""):
        return "Não foi possível identificar o remetente."
    
    sender_name = extract_name_sender(question)
    
    title = assingn_title(question)
    if(title == ""):
        return "Não foi possível identificar o título do email."
    
    instructions = f"""
        /context
        Use uma lingugem formal de e pessial.
        Seja educado e empático.
        Remove o texto 'Assunto:'.
        Deve conte uma saudação.
        Não use abreviações.
        Não use gírias.
        Deve conter uma despedida cordial.
        
        /shorten
        O email destinatário é: {addressee_email}
        O Nome destinatário é: {addressee_name}
        O email do remetente é: {sender_email}
        O Nome do remetente é: {sender_name}
        {question}
    """
    
    email = to_ask(instructions)
    print("Email: ", email[:30].strip())
    MailSMTP.send_email(sender_email, addressee_email, title, email)
    return f"{random.choice(email_responses)} de {sender_email} para {addressee_email}"


def extract_email_addressee(question: str):
    
    addressee_email = "lucasfrct@gmail.com"
    
    prompt = f"""
        /shorten
        Descarte o email de envio.
        Descubra o email do destinatário expresso no texto.
        Se não encontrar o email, retorne {addressee_email}.
        Encontre o email do destinatário no seguinte texto: {question}
        Retorne somente o email do destinatário.
    """
    return to_ask(prompt)


def extract_email_sender(question: str):
    
    sender_email = "lucasfrct@gmail.com"
    
    prompt = f"""
        /shorten
        Descarte o email de destinatário expresso no texto.
        Descubra o email do remetente expresso no texto.
        Se não encontrar o email do remetente, retorne {sender_email}.
        Encontre o email do remetente no seguinte texto: {question}
        Retorne somente o email do remetente.
    """
    return to_ask(prompt)


def extract_name_addresse(question: str):
    
    addressee_name = "Lucas Costa"
    
    prompt = f"""
        /shorten
        Extraia o nome do destinatário.
        retorne somente o nome do destinatário.
        Encontre o nome formal do receptor do email.
        Se não encontrar o nome, retorne {addressee_name}.
        Responda com o nome que indique ser o detinatário : {question}
    """
    return to_ask(prompt)


def extract_name_sender(question: str):
    sender_name = "Lucas Costa"
    prompt = f"""
        /shorten
        Extraia o nome do remetende.
        Reorne somete o nome do emissor.
        Encontre o nome formal do emissor do email.
        Se não encontrar o nome, retorne {sender_name}.
        Responda com o nome que indique ser o emissário: {question}
    """
    return to_ask(prompt)


def assingn_title(email: str):
    prompt = f"""
        /shorten
        Atribua um título para o email abaixo com no máximo 5 palavrras.
        O título de expressar a inteção principal do email.
        Não responda com a pergunta.
        Se não hover título, retorne vazio.
        Responda com o título para o email: {email}
    """
    return to_ask(prompt)

client_gpt()
messages.append(system_instructions())

# messages.append(system_instructions())
# question = "Escreva um email para solicitações a respeita da dispensa do funcionário Jair. Envie para e o email lucasfrct@outlook.com destinado ao senhor Rafael."
# print(action_email(question))