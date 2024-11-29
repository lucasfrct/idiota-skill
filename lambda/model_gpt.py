
from openai import OpenAI
import knowledgebase as KnowledgeBase 

openai_api_key = ""

MODEL = "gpt-4o-mini"

client = OpenAI(api_key=openai_api_key)

instruction = """
    Você é um assistente pessoal de nome Altair. 
    Não tente explicar a resposta, ao menos que seja solicitado.
    Não repita a pergunda que foi lhe feito.
    Responda de forma, direta e curta. 
    Responda em Português do Brasil.
"""
messages = [ { "role": "system", "content": instruction, } ]


def generate_gpt_response(query):
    try:
        
        docs = KnowledgeBase.query(query)
        
        question = query
        if(len(docs) > 0):
            question = f"""
                /train
                User o seguinte documento como base para melhorar as respostas:
                
                [DOCUMENTOS]
                {docs}
                
                [PERGUNTA]
                Responda a  pergunta com base nos documentos acima: {query}.
                Caso não horver informção suficiente, responda com base no seu conhecimento.
                
                /shorten
            """
        
        messages.append({"role": "user", "content": question})
        response = client.chat.completions.create(model=MODEL, messages=messages, max_tokens=700, temperature=0.8)
        reply = response.choices[0].message.content
        messages.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        return f"Erro ao gerar resposta: {str(e)}"