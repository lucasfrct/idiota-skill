import chromadb
import hashlib

collection_name = 'knowledge'

def hash(text: str = "") -> str:
    text_bytes = text.encode('utf-8')
    hash_obj = hashlib.sha3_256()
    hash_obj.update(text_bytes)
    return hash_obj.hexdigest()


def sgdb() -> chromadb.ClientAPI:
	# Inicializa o cliente ChromaDB com armazenamento local
	return chromadb.PersistentClient(path='./chromadb_knoledge')
    

def collection() -> chromadb.Collection:
    return sgdb().get_or_create_collection(name=collection_name)
    
    
def save(content: str):
    hash_id = hash(content)
    collection().add(documents=[content], ids=[hash_id])
    

def query(question: str):
	result = collection().query(query_texts=[question], n_results=5)
	response = ""
	for r in result['documents'][0]:
		response += f"{r} \n"
	return response


def conflict_id(collection: chromadb.Collection, id: str = ""):
    results = collection.get(ids=[id])
    return len(results['ids']) > 0

save('Raseum é um clérico dos cavaleiros do expurgado, ele é um dos personagens principais do RPG Sombras do Expurgo.')
print("R: ", query('Quem é Raseum?'))