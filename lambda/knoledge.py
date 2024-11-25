import chromadb
import uuid

# Inicializa o cliente ChromaDB com armazenamento local
client = chromadb.PersistentClient(path='./chromadb_knoledge')
collection_name = 'knoledge'

def collection() -> chromadb.Collection:
    return client.get_or_create_collection(name=collection_name)
    
    
def inserir(text: str):
	collection().add(documents=text, ids=str(uuid.uuid4()))

def consultar(question: str):
	result = collection().query(query_texts=[question], n_results=5)
	response = ""
	for r in result['documents'][0]:
		response += f"{r} \n"
	return response


inserir('Raseum é um clérico dos cavaleiros do expurgado, ele é um dos personagens principais do RPG Sombras do Expurgo.')
print(consultar('Quem é Raseum?'))