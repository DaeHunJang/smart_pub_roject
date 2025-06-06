import os
from typing import List, Dict, Any
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
import pandas as pd
from pathlib import Path

from ..utils.helpers import get_logger, load_json, save_json
from ..config import VECTOR_DB_DIR, EMBEDDING_MODEL_ID, MODEL_DIR

logger = get_logger(__name__)

class VectorStore:
    def __init__(self, vector_db_dir: str = VECTOR_DB_DIR, embedding_model_id: str = EMBEDDING_MODEL_ID):
        self.vector_db_dir = vector_db_dir
        self.embedding_model_id = embedding_model_id
        self.embedding_model = None
        self.index = None
        self.documents = []
        
        self.index_path = os.path.join(vector_db_dir, "faiss_index.bin")
        self.documents_path = os.path.join(vector_db_dir, "documents.json")
    
    def _get_local_model_path(self, model_id):
        """Get local model path if available"""
        if os.path.exists(model_id):
            # Already a local path
            return model_id
        
        # Extract model name from HF model ID
        if "/" in model_id:
            model_name = model_id.split("/")[-1]
        else:
            model_name = model_id
        
        local_path = Path(MODEL_DIR) / model_name
        
        # Check if local model exists
        if local_path.exists() and (local_path / "config.json").exists():
            logger.info(f"Using local embedding model: {local_path}")
            return str(local_path)
        else:
            logger.info(f"Local embedding model not found, will download: {model_id}")
            return model_id
    
    def load_embedding_model(self) -> bool:
        try:
            # Try to use local model first
            model_path = self._get_local_model_path(self.embedding_model_id)
            
            # Set cache directory for downloads
            cache_dir = Path(MODEL_DIR) / "sentence_transformers_cache"
            cache_dir.mkdir(exist_ok=True)
            
            self.embedding_model = SentenceTransformer(
                model_path, 
                cache_folder=str(cache_dir)
            )
            
            logger.info(f"Loaded embedding model from {model_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error loading embedding model: {e}")
            
            # Try fallback to original model ID if local path failed
            if model_path != self.embedding_model_id:
                try:
                    logger.info(f"Trying fallback to download: {self.embedding_model_id}")
                    cache_dir = Path(MODEL_DIR) / "sentence_transformers_cache"
                    cache_dir.mkdir(exist_ok=True)
                    
                    self.embedding_model = SentenceTransformer(
                        self.embedding_model_id,
                        cache_folder=str(cache_dir)
                    )
                    
                    logger.info(f"Successfully downloaded and loaded: {self.embedding_model_id}")
                    return True
                    
                except Exception as e2:
                    logger.error(f"Fallback also failed: {e2}")
            
            return False
    
    def build_index(self, drinks: List[Dict[str, Any]]) -> bool:
        if not self.embedding_model:
            if not self.load_embedding_model():
                return False
        
        try:
            self.documents = []
            texts = []
            
            for drink in drinks:
                basic_info = (
                    f"Name: {drink['name']}\n"
                    f"Category: {drink['category']}\n"
                    f"Alcoholic: {drink['alcoholic']}\n"
                    f"Glass: {drink['glass']}\n"
                    f"Tags: {', '.join(drink['tags'])}\n"
                )
                self.documents.append({
                    "id": f"{drink['id']}_basic",
                    "drink_id": drink['id'],
                    "type": "basic",
                    "text": basic_info,
                    "drink": drink
                })
                texts.append(basic_info)
                
                ingredients_text = "Ingredients:\n"
                for ingredient in drink['ingredients']:
                    ingredients_text += f"- {ingredient['ingredient']}: {ingredient['measure']}\n"
                self.documents.append({
                    "id": f"{drink['id']}_ingredients",
                    "drink_id": drink['id'],
                    "type": "ingredients",
                    "text": ingredients_text,
                    "drink": drink
                })
                texts.append(ingredients_text)
                
                instructions_text = f"Instructions:\n{drink['instructions']}"
                self.documents.append({
                    "id": f"{drink['id']}_instructions",
                    "drink_id": drink['id'],
                    "type": "instructions",
                    "text": instructions_text,
                    "drink": drink
                })
                texts.append(instructions_text)
            
            logger.info(f"Encoding {len(texts)} texts...")
            embeddings = self.embedding_model.encode(texts, show_progress_bar=True)
            
            dimension = embeddings.shape[1]
            self.index = faiss.IndexFlatL2(dimension)
            self.index.add(embeddings.astype(np.float32))
            
            self.save()
            
            logger.info(f"Built index with {len(self.documents)} documents")
            return True
        
        except Exception as e:
            logger.error(f"Error building index: {e}")
            return False
    
    def save(self) -> bool:
        try:
            os.makedirs(self.vector_db_dir, exist_ok=True)
            
            faiss.write_index(self.index, self.index_path)
            
            document_data = []
            for doc in self.documents:
                doc_copy = doc.copy()
                doc_copy["drink"] = doc_copy["drink"]["id"]
                document_data.append(doc_copy)
            
            save_json(document_data, self.documents_path)
            
            logger.info(f"Saved vector index to {self.index_path} and documents to {self.documents_path}")
            return True
        
        except Exception as e:
            logger.error(f"Error saving vector index: {e}")
            return False
    
    def load(self, drinks: List[Dict[str, Any]]) -> bool:
        try:
            if os.path.exists(self.index_path):
                self.index = faiss.read_index(self.index_path)
            else:
                logger.warning(f"Index file not found: {self.index_path}")
                return False
            
            if os.path.exists(self.documents_path):
                document_data = load_json(self.documents_path)
                
                drink_lookup = {drink["id"]: drink for drink in drinks}
                
                self.documents = []
                for doc in document_data:
                    drink_id = doc["drink"]
                    doc["drink"] = drink_lookup.get(drink_id, {"id": drink_id})
                    self.documents.append(doc)
            else:
                logger.warning(f"Documents file not found: {self.documents_path}")
                return False
            
            logger.info(f"Loaded vector index from {self.index_path} and {len(self.documents)} documents from {self.documents_path}")
            return True
        
        except Exception as e:
            logger.error(f"Error loading vector index: {e}")
            return False
    
    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if not self.embedding_model:
            if not self.load_embedding_model():
                return []
        
        if not self.index:
            logger.error("Index is not loaded. Call load() or build_index() first.")
            return []
        
        try:
            query_embedding = self.embedding_model.encode([query])
            
            distances, indices = self.index.search(query_embedding.astype(np.float32), top_k)
            
            results = []
            for i, idx in enumerate(indices[0]):
                if idx < len(self.documents):
                    result = self.documents[idx].copy()
                    result["distance"] = float(distances[0][i])
                    results.append(result)
            
            return results
        
        except Exception as e:
            logger.error(f"Error searching index: {e}")
            return []