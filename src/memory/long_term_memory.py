# Standard library imports
import os
import json
import pickle
import logging
from pathlib import Path
from typing import Dict, List, Optional

# Third-party library imports
import nltk
import torch
from tqdm import tqdm

from milvus_model.sparse import BM25EmbeddingFunction
from milvus_model.hybrid import BGEM3EmbeddingFunction
from milvus_model.sparse.bm25.tokenizers import build_default_analyzer

from pymilvus.orm.schema import CollectionSchema, FieldSchema
from pymilvus import Collection, DataType, MilvusClient, connections
from pymilvus.client.abstract import AnnSearchRequest, SearchResult, WeightedRanker

# Local application imports
from config.config import Config
from src.utils.utils import setup_logging
from src.utils.ner_extraction import EntityExtractor
from src.document_engine import Document, DocumentEngine

# Setup logging
setup_logging()


class LongTermMemory:
    def __init__(self):
        self.run_mode = Config.get("run_mode")
        self.ner_extractor = EntityExtractor(Config.get("locations_file"))

        # Define the weights for the hybrid search
        self.DENSE_SEARCH_WEIGHT = 0.1
        self.SPARSE_SEARCH_WEIGHT = 0.9

        # Establish a connection to the Milvus server
        self._client = self._connect()

        self._doc_engine = DocumentEngine()
        self._dense_ef = self._load_embedding_function("dense")
        self._sparse_ef = self._load_embedding_function("sparse")

        self._docs, self._chunks = None, None
        self._load_collections()

    def _connect(self):
        host = Config.get("MILVUS_HOST")
        port = Config.get("MILVUS_PORT")
        connections.connect(host=host, port=port)
        client = MilvusClient(host=host, port=port)
        return client

    def _load_embedding_function(self, ef_type, corpus=None):
        if ef_type == "sparse":
            # More info here: https://milvus.io/docs/embed-with-bm25.md
            # If a bm25 model already exists, load it. Otherwise, create a new one.
            model_path = Config.get("sparse_embed_func_path")
            if os.path.exists(model_path):
                # Load the existing model
                with open(model_path, "rb") as file:
                    bm25_ef = pickle.load(file)
            else:
                # Check if the 'stopwords' dataset is not already loaded in NLTK
                if "stopwords" not in nltk.corpus.util.lazy_imports:
                    # Download the 'stopwords' dataset using NLTK's download utility
                    nltk.download("stopwords")

                # Create an analyzer for processing documents, here specifying Spanish language
                analyzer = build_default_analyzer(language="sp")
                # Initialize a BM25 embedding function with the previously created analyzer
                bm25_ef = BM25EmbeddingFunction(analyzer)

                # Check if a corpus is provided to fit the model
                if corpus:
                    # Fit the BM25 embedding function to the provided corpus
                    bm25_ef.fit(corpus)
                    # Serialize the BM25 model into a file for persistence
                    with open(model_path, "wb") as file:
                        pickle.dump(
                            bm25_ef, file
                        )  # Use Python's pickle module for serialization

            return bm25_ef
        elif ef_type == "dense":
            # Más información aquí: https://milvus.io/docs/embed-with-bgm-m3.md
            bgeM3_ef = BGEM3EmbeddingFunction(
                model_name="BAAI/bge-m3",  # Especifica el nombre del modelo
                device="cpu",
                use_fp16=False,  # Usa FP16 si está en CUDA, de lo contrario False
                return_colbert_vecs=False,  # No se necesitan vectores de salida de COLBERT
                return_dense=True,  # Vectores densos para búsqueda semántica
                return_sparse=False,  # Los dispersos los tomaremos de bm25
            )
            return bgeM3_ef
        else:
            raise ValueError(f"Unsupported embedding function type: {ef_type}")

    def _load_collections(self):
        # RES_LOAD (reset and load): delete all documents and load new ones
        # RES_LOAD_FILES (reset database and load files): load chunks from files; used to recover from errors
        # NO_RES_NO_LOAD (no reset, no load): do not delete or load documents
        if self.run_mode == "RES_LOAD":
            self.delete_documents("all")

        # Load collections for all run modes
        self._docs = self._load_docs_collection()
        self._chunks = self._load_chunks_collection()

        # Load documents and chunks from the raw data folder
        self._ingest_folder()

    def _ingest_folder(self):
        # Define paths using configuration settings
        raw_folder = Path(Config.get("raw_data_folder"))

        if self.run_mode == "RES_LOAD":
            # Read and generate documents from PDF files in the raw data folder
            files = [str(file) for file in raw_folder.glob("*.pdf")]
            documents = self._doc_engine.generate_documents(files, "decrees")

            # Insert the generated document records into the database
            doc_records = self._generate_doc_records(documents)
            self._client.insert(collection_name="docs", data=doc_records)
            logging.info(f"Finished inserting {len(doc_records)} document records.")

            self._process_and_insert_chunks(documents)

    def _process_documents(self, documents):
        chunk_records = []
        chunks = self._doc_engine._chunk_documents(documents)

        # Generate chunk records including embeddings
        chunk_records.extend(self._generate_chunk_records(chunks))

        return chunk_records

    def _insert_chunk_records(self, chunk_records):
        """
        Batch load chunk records, including embeddings, into the _chunks collection.
        """
        self._chunks.insert(chunk_records)
        logging.info(f"Inserted {len(chunk_records)} chunk records.")

    def _load_docs_collection(self):
        if not self._client.has_collection("docs"):
            schema = self._create_docs_schema()
            self._client.create_collection(collection_name="docs", schema=schema)

            # Adjusted index_params structure to be a list containing one dictionary
            index_params = [
                {
                    "field_name": "docs_vector",
                    "params": {
                        "metric_type": "L2",
                        # Assuming you want to use an IVF_FLAT index as an example
                        "index_type": "IVF_FLAT",
                        "params": {
                            "nlist": 128
                        },  # Example specific parameter for IVF_FLAT
                    },
                }
            ]

            self._client.create_index(
                collection_name="docs",
                index_params=index_params,  # Now passing a list of dictionaries
            )

        self._client.load_collection("docs")
        return Collection(name="docs")

    def _load_chunks_collection(self):
        if not self._client.has_collection("chunks"):
            schema = self._create_chunks_schema()
            self._client.create_collection(collection_name="chunks", schema=schema)

        # Define index parameters for both fields as a list of dictionaries
        index_params = [
            {
                "field_name": "dense_vector",
                "params": {
                    "metric_type": "IP",  # Inner Product, assuming normalized vectors for cosine similarity
                    "index_type": "IVF_FLAT",
                    "params": {"nlist": 128},
                },
            },
            {
                "field_name": "sparse_vector",
                "params": {
                    "metric_type": "IP",  # Inner Product
                    # Assuming default index_type and other parameters if needed
                },
            },
        ]

        # Create indexes on the specified fields
        self._client.create_index(
            collection_name="chunks",
            index_params=index_params,  # Pass the list of dictionaries as index_params
        )

        self._client.load_collection("chunks")
        return Collection(name="chunks")

    def _create_docs_schema(self) -> CollectionSchema:
        """
        Returns a schema for the long-term memory database of documents.
        """
        schema = CollectionSchema(
            [
                FieldSchema(
                    name="id",
                    dtype=DataType.VARCHAR,
                    max_length=32,
                    is_primary=True,
                    auto_id=False,
                ),
                FieldSchema(name="page", dtype=DataType.INT64),
                FieldSchema(name="date", dtype=DataType.VARCHAR, max_length=32),
                FieldSchema(name="type", dtype=DataType.VARCHAR, max_length=32),
                FieldSchema(name="number", dtype=DataType.INT64),
                FieldSchema(
                    name="text",
                    dtype=DataType.VARCHAR,
                    max_length=Config.get("max_doc_size"),
                ),
                # Add a docs vector; Milvus requires at least one vector field in the schema
                # This is not used, just a workaround to satisfy the schema requirements
                FieldSchema(name="docs_vector", dtype=DataType.FLOAT_VECTOR, dim=2),
            ],
            description="Collection for storing text and metadata of each document",
            enable_auto_id=False,
        )

        return schema

    def _create_chunks_schema(self) -> CollectionSchema:
        """
        Returns a schema for the long-term memory database of chunks.
        """
        schema = CollectionSchema(
            [
                FieldSchema(
                    name="id",
                    dtype=DataType.VARCHAR,
                    max_length=32,
                    is_primary=True,
                    auto_id=False,
                ),
                FieldSchema(
                    name="dense_vector",
                    dtype=DataType.FLOAT_VECTOR,
                    dim=self._dense_ef.dim["dense"],
                ),
                FieldSchema(
                    name="sparse_vector",
                    dtype=DataType.FLOAT_VECTOR,
                    dim=self._sparse_ef.dim,
                ),
                FieldSchema(
                    name="parent_id",
                    dtype=DataType.VARCHAR,
                    max_length=32,
                ),
                FieldSchema(
                    name="text",
                    dtype=DataType.VARCHAR,
                    max_length=Config.get("chunk_text_size"),
                ),
                FieldSchema(name="entities", dtype=DataType.JSON),
            ],
            description="Collection for storing chunk embeddings",
            enable_auto_id=False,
        )

        return schema

    def delete_documents(self, collection: str) -> None:
        """
        Deletes documents from database.
        """
        if collection == "all":
            collections = self._client.list_collections()
            for collection_name in collections:
                self._client.drop_collection(collection_name)
        else:
            self._client.drop_collection(collection)
        logging.info(f"Deleted documents from collection: {collection}")

    def add_documents(self, files: List[str], type: str = "decrees") -> None:
        logging.info(f"Adding documents of type {type} to the database.")
        # Generate documents, format them into database records, and insert them
        documents = self._doc_engine.generate_documents(files, type)
        doc_records = self._generate_doc_records(documents)
        self._client.insert(collection_name="docs", data=doc_records)

        # Generate chunks, format them into database records, and insert them
        logging.info("Processing and inserting chunk records.")
        self._process_and_insert_chunks(documents)

    def _process_and_insert_chunks(
        self, documents, batch_size: int = 10, start_from: int = 0
    ):
        """
        Process documents in batches, create chunk records, insert them into the database,
        and log the process with a progress bar. Updated to include a starting index.

        Args:
            documents (List[Document]): List of Document objects to process.
            batch_size (int): Number of documents to process in each batch.
            start_from (int): Index to start processing from.
        """

        # Adjust total batches based on the new start_from parameter
        adjusted_docs = documents[start_from:]
        num_batches = len(adjusted_docs) // batch_size + (
            1 if len(adjusted_docs) % batch_size > 0 else 0
        )

        # Process documents in batches starting from start_from
        for i in tqdm(
            range(num_batches), desc="Processing and inserting document chunks"
        ):
            # Adjust batch slice indices based on start_from
            start_idx = start_from + i * batch_size
            end_idx = min(start_from + (i + 1) * batch_size, len(documents))

            # Select batch documents
            batch_documents = documents[start_idx:end_idx]

            try:
                # Generate and insert chunk records for the current batch
                self._process_and_insert_chunk_batch(batch_documents)
            except Exception as e:
                # Log the next start_from value before raising the error
                next_start_from = (
                    start_idx + batch_size
                )  # Or end_idx for more precision
                logging.error(
                    f"Error processing documents. Next start_from should be: {next_start_from}. Error: {e}"
                )
                raise  # Re-raise the error to stop the process

        logging.info("Completed processing and inserting chunks.")

    def _process_and_insert_chunk_batch(self, batch_documents: List[Document]):
        """
        Generate chunk records for a batch of documents and insert them into the database.

        Args:
            batch_documents (List[Document]): List of Document objects to process.
        """

        # Generate chunks for the documents
        chunks = self._doc_engine._chunk_documents(batch_documents)

        # Generate chunk records, including both dense and sparse embeddings
        chunk_records = self._generate_chunk_records(chunks)

        # Insert chunk records into the database
        self._insert_chunk_records(chunk_records)

    def _generate_doc_records(self, documents: List[Document]) -> List[Dict[str, any]]:
        records = []
        for doc in documents:
            record = {
                "id": doc.id,
                "page": doc.metadata.get("page", 0),
                "date": doc.metadata.get("date", ""),
                "type": doc.metadata.get("type", ""),
                "number": (
                    int(doc.metadata.get("number", 0))
                    if doc.metadata.get("number") is not None
                    else 0
                ),  # Ensure number is not None
                "text": doc.text[
                    : Config.get("doc_size")
                ],  # Truncate text if necessary
                "docs_vector": [0.0, 0.0],  # Dummy vector for the docs_vector field
            }
            records.append(record)

        return records

    def _generate_chunk_records(self, chunks: List[Document]) -> List[Dict[str, any]]:
        """
        Prepare the records for inserting into the _chunks collection,
        including both dense and sparse embeddings, formatted as dictionaries.
        """
        records = []

        # Extract chunk texts to generate embeddings
        chunk_texts = [chunk.text for chunk in chunks]
        # Generate dense embeddings using the BGE-M3 function
        dense_embeddings = self._dense_ef.encode_documents(chunk_texts)["dense"]

        # Generate sparse embeddings using the BM25 function
        raw_sparse_embeddings = self._sparse_ef.encode_documents(chunk_texts)

        # Convert sparse embeddings from csr_array format to a list for insertion
        sparse_embeddings = [
            sparse_embedding.toarray().tolist()[
                0
            ]  # Ensure it's a list of lists, not a list of arrays
            for sparse_embedding in raw_sparse_embeddings
        ]

        # Prepare records with both embeddings for each chunk
        for i, chunk in tqdm(enumerate(chunks), desc="Generating chunk records"):
            entities = self.ner_extractor.extract_entities(chunk.text)
            entities_list = [{"type": ent[0], "value": ent[1]} for ent in entities]
            # Extract the document ID from the chunk ID
            parent_document_id = chunk.id.split("_")[0]

            # Check if LAW entity already exists
            law_entity_exists = any(
                ent
                for ent in entities_list
                if ent["type"] == "LAW" and ent["value"] == parent_document_id
            )

            # Add the LAW entity with the parent document ID if it doesn't already exist
            if not law_entity_exists:
                entities_list.append({"type": "LAW", "value": parent_document_id})

            record = {
                "id": chunk.id,
                "dense_vector": dense_embeddings[i].tolist(),
                "sparse_vector": sparse_embeddings[i],
                "parent_id": chunk.metadata.get("parent_id", 0),
                "text": chunk.text[: Config.get("chunk_size")],
                "entities": entities_list,
            }
            records.append(record)

        return records

    def _insert_doc_records(self, doc_records: List[List[any]]) -> None:
        """
        Batch load document records into the _docs collection.

        Args:
            doc_records: A list of document records to insert.
        """
        # Transpose the doc_records list to match the structure expected by Milvus insert method.
        field_values = list(zip(*doc_records))

        # Construct a dictionary where keys are field names and values are lists of field values.
        data_to_insert = {
            "id": field_values[0],
            "page": field_values[1],
            "date": field_values[2],
            "type": field_values[3],
            "number": field_values[4],
            "text": field_values[5],
        }

        # Insert the formatted records into the _docs collection.
        self._docs.insert(data_to_insert)
        logging.info(f"Inserted {len(doc_records)} document records.")

    def _insert_chunk_records(self, chunk_records):
        """
        Batch load chunk records, including embeddings, into the _chunks collection.
        """
        self._chunks.insert(chunk_records)
        logging.info(f"Inserted {len(chunk_records)} chunk records.")

    def get_context(self, query: str, n_docs=2) -> List[Document]:
        """
        Retrieves relevant documents from the database based on a query.
        Args:
            query (str): Input query.

        Returns:
            List[Document]: List of relevant documents.
        """
        n_results = 1000
        results = self._find_relevant_chunks(query, n_results)
        documents = self._retrieve_parent_documents(results, n_docs)
        context = self._create_context(documents)

        return context

    def _find_relevant_chunks(
        self, query: str, n_results: int
    ) -> List[AnnSearchRequest]:
        """
        Retrieves relevant context from the database based on a query.

        Args:
            query (str): Input query string.
            n_docs (int): Number of documents to retrieve context for.

        Returns:
            The context as a string, aggregated from relevant documents.
        """

        # Generate dense embedding for the query
        raw_query_dense_embeddings = self._dense_ef.encode_queries([query])
        dense_query_embedding = [raw_query_dense_embeddings["dense"][0].tolist()]

        # Generate sparse embedding for the query
        raw_query_sparse_embeddings = self._sparse_ef.encode_queries(
            [query]
        )  # Returns a csr_matrix

        # Convert sparse embedding from csr_matrix format to a list for insertion
        sparse_query_embedding = raw_query_sparse_embeddings.toarray().tolist()

        # AnnSearchRequest for dense embeddings
        dense_search_request = AnnSearchRequest(
            data=dense_query_embedding,
            anns_field="dense_vector",
            param={"metric_type": "IP", "params": {"nprobe": 1000}},
            limit=n_results,
        )

        # AnnSearchRequest for sparse embeddings
        sparse_search_request = AnnSearchRequest(
            data=sparse_query_embedding,
            anns_field="sparse_vector",
            param={"metric_type": "IP", "params": {"nprobe": 1000}},
            limit=n_results,
        )

        extracted_entities = self.ner_extractor.extract_entities(query)
        if extracted_entities:
            # Construct the JSON array for JSON_CONTAINS_ANY
            entity_list = [
                {"type": entity_type, "value": entity_value.replace('"', '\\"')}
                for entity_type, entity_value in extracted_entities
            ]
            entity_list_json = json.dumps(entity_list)  # Convert to JSON string

            # Construct the filter using JSON_CONTAINS_ANY
            dynamic_filter = f"JSON_CONTAINS_ANY(entities, {entity_list_json})"

        response = None
        # Perform Hybrid Search
        if extracted_entities:
            response = self._chunks.hybrid_search(
                reqs=[
                    dense_search_request,
                    sparse_search_request,
                ],
                rerank=WeightedRanker(
                    self.DENSE_SEARCH_WEIGHT, self.SPARSE_SEARCH_WEIGHT
                ),
                output_fields=["parent_id", "entities"],
                limit=n_results,
                filter=dynamic_filter,
            )

            # Filter the response to only include chunks with the extracted entities
            hits = []
            for hit in response[0]:
                entities = hit.entity.get("entities")
                if any(ent in entities for ent in entity_list):
                    hits.append(hit)
            if hits:
                response[0] = hits

        # If no entities were extracted or no relevant chunks were found, perform a general search
        if not extracted_entities or not len(response):
            response = self._chunks.hybrid_search(
                reqs=[
                    dense_search_request,
                    sparse_search_request,
                ],
                rerank=WeightedRanker(
                    self.DENSE_SEARCH_WEIGHT, self.SPARSE_SEARCH_WEIGHT
                ),
                output_fields=["parent_id"],
                limit=n_results,
            )
        return response[0]

    def _retrieve_parent_documents(
        self, response: SearchResult, n_docs: int
    ) -> List[Document]:
        # Retrieve n_docs unique parent IDs from the response
        unique_parent_ids = []
        for hit in response:
            parent_id = hit.entity.get("parent_id")
            if parent_id not in unique_parent_ids:
                unique_parent_ids.append(parent_id)
                if len(unique_parent_ids) == n_docs:
                    break

        documents = [
            self._get_document_by_id(parent_id) for parent_id in unique_parent_ids
        ]

        return documents

    def _create_context(self, documents: List[Document]) -> (str, str):
        context = ""
        sources_list = []

        for doc in documents:
            # Create header for each document
            header = f"###DECRETO {doc.metadata['number']}###\n"
            # Add the text of the document to the context
            context += f"{header}{doc.text}\n\n"
            # Record the source of the document, which is its number and page
            sources_list.append(
                f"- Decreto {doc.metadata['number']} (página {doc.metadata['page']})"
            )

        # Combine the sources into a single string prefixed with "SOURCES:"
        sources = "Fuentes consultadas:\n" + "\n".join(sources_list)

        return context, sources

    def _get_document_by_id(self, data_id: str) -> Optional[Document]:
        """
        Gets a document from the Milvus 'docs' collection by its ID.

        Args:
            data_id (str): Unique ID associated with the document.

        Returns:
            Optional[Document]: Document associated with the given ID, or None if not found.
        """
        try:
            res = self._client.get(
                collection_name="docs",
                ids=[data_id],  # Milvus expects a list of IDs
            )

            if not res:
                logging.info(f"Document with ID {data_id} not found")
                return None

            fields = res[0]  # Extract the fields of the result
            # Create a new metadata dictionary that includes everything except 'id' and 'text'
            metadata = {
                key: value
                for key, value in fields.items()
                if key not in ["id", "text", "docs_vector"]
            }

            document = Document(
                id=fields["id"],
                text=fields.get("text", ""),
                metadata=metadata,  # Pass the new metadata dictionary
            )

            return document

        except Exception as e:
            logging.error(
                f"An error occurred while retrieving document ID {data_id}: {e}"
            )
            return None
