
import os
import random
from openai import OpenAI
from dotenv import load_dotenv

import mail as MailSMTP
import time

load_dotenv()

messages = []
MODEL = "gpt-4o-mini"

email_actions = ["gere um email", "criar um email", "escreva um email", "faça um email", 'faz um email', 'monte uma email', 'elabore uma email', 'redija um email']
email_responses = ["Já enviei", "Email enviado", "Já tá feito", 'Acabei de mandar', 'Envio realizado', 'Jã está na caixa de enviados']


def client_gpt()-> OpenAI:
    openai_api_key = os.getenv('KEY_GPT')
    client = OpenAI(api_key=openai_api_key)
    return client

# Instruçõers para o sistema
def system_instructions():
    instruction = """
        Você é um assistente pessoal de nome Altair. 
        Responda de forma, direta e curta. 
        Responda em Português do Brasil.
        Não tente explicar sua resposta.
        Não repita a pergunda feita.
    """
    return { "role": "system", "content": instruction, }


# encontrar categoria
def categorize(question: str):
    response = client_gpt().chat.completions.create(model=MODEL, messages=[{"role": "assistant", "content": question}], temperature=0.5, stream=False)
    print("CAT: ", response.choices[0].message.content)
    return response.choices[0].message.content


# Fazer a pergunta
def to_ask(question: str):
    messages.append({"role": "user", "content": question})
    response = client_gpt().chat.completions.create(model=MODEL, messages=messages, temperature=0.8, stream=False)
    print("TO - RES: ", response.choices[0].message.content)
    return response.choices[0].message.content


# gerar resposta
def generate_gpt_response(query: str):
    try:
        
        actions = "" 
        for action in email_actions:
            actions += f"- {action}\n"
            
        intention = discover_the_intention(query).strip()
        print("Intenção: ", intention)
        
        if intention != "" and intention in actions:
            print("Ação de email")
            return action_email(query)
        
        print("Ação COMUM", query)
        reply = to_ask(query)
        messages.append({ "role": "assistant", "content": f"{len(messages)}. {reply}" })
        return reply
    except Exception as e:
        return f"Erro ao gerar resposta: {str(e)}"
    

def discover_the_intention(question: str):
    
    actions = "" 
    
    for action in email_actions:
        actions += f"- {action}\n        "
        
    prompt = f"""
        /shorten
        Verifique a intenção para ação da pergunta abaixo.
        Responda somente com a ação encontrada em uma das opções.
        Caso não encontrar uma opção, responda com texto vazio.
        As opções de categorias são:
        {actions}
        Com base na seguinte pergunta, categorize: {question}
    """
    response = categorize(prompt)
    return response


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
        Use uma lingugem formal de e pessoal.
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
    MailSMTP.send_email(sender_email, addressee_email, title, email)
    return f"{random.choice(email_responses)} de {sender_email} para {addressee_email}"


def extract_email_addressee(question: str):
    
    addressee_email = "lucasfrct@gmail.com"
    
    prompt = f"""
        /shorten
        Descubra o endereço do email do destinatário expresso no texto.
        O endereço de email deve conter um dominio com @.
        O endereço de email deve ser diferente de {addressee_email}.
        O email pode estar perto do terecho 'envie para...', 'enviar para...', 'mandar para...'
        Encontre o endereço de email do destinatário no seguinte texto: {question}
        Se não encontrar o endereço de email, retorne {addressee_email}.
        Retorne somente o endereço de email do destinatário.
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

messages.append(system_instructions())
question = "Escreva um email para solicitqando dispensa do funcionário Jair. Justifique disendo que le não comparece ao trabalho faz 5 dias. Envie para e o email lucasfrct@outlook.com destinado ao senhor Rafael."
# question = "Avalie para mim quais documentos preciso para abrir uma empresa?"
print(generate_gpt_response(question))