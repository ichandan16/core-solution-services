# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Query Vector Store
"""
from abc import ABC, abstractmethod
import json
import gc
import os
import shutil
import tempfile
import numpy as np
from pathlib import Path
from typing import List
from common.models import QueryEngine
from common.models.llm_query import (VECTOR_STORE_LANGCHAIN_PGVECTOR,
                                     VECTOR_STORE_MATCHING_ENGINE)
from common.utils.logging_handler import Logger
from common.utils.http_exceptions import InternalServerError
from google.cloud import aiplatform, storage
from google.cloud.exceptions import Conflict
from services.query import embeddings
from config import PROJECT_ID, REGION
from config.vector_store import (LC_VECTOR_STORES,
                                 PG_HOST, PG_PORT,
                                 PG_DBNAME, PG_USER, PG_PASSWD)
from langchain.schema.vectorstore import VectorStore as LCVectorStore
from langchain.vectorstores import PGVector


# pylint: disable=broad-exception-caught

Logger = Logger.get_logger(__file__)

# embedding dimensions generated by TextEmbeddingModel
DIMENSIONS = 768

# number of document match results to retrieve
NUM_MATCH_RESULTS = 5

# number of text chunks to process into an embeddings file
MAX_NUM_TEXT_CHUNK_PROCESS = 1000


class VectorStore(ABC):
  """
  Abstract class for vector store db operations.  A VectorStore is created
  for a QueryEngine instance and manages the document index for that engine.
  """

  def __init__(self, q_engine: QueryEngine) -> None:
    self.q_engine = q_engine


  @abstractmethod
  def index_document(self, doc_name: str, text_chunks: List[str],
                          index_base: int) -> int:
    """
    Generate index for a document in this vector store
    Args:
      doc_name (str): name of document to be indexed
      text_chunks (List[str]): list of text content chunks for document
      index_base (int): index to start from; each chunk gets its own index
    """

  @abstractmethod
  def deploy(self):
    """ Deploy vector store index for this query engine """


  @abstractmethod
  def find_neighbors(self, q_engine: QueryEngine,
                     query_embeddings: List[List[float]]) -> List[int]:
    """
    Retrieve text matches for query embeddings.
    Args:
      q_engine: QueryEngine model
      query_embeddings: list of embedding arrays
    Returns:
      list of indexes that are matched of length NUM_MATCH_RESULTS
    """

class MatchingEngineVectorStore(VectorStore):
  """
  Class for vector store based on Vertex matching engine. 
  """
  def __init__(self, q_engine: QueryEngine) -> None:
    super().__init__(q_engine)
    self.storage_client = storage.Client(project=PROJECT_ID)

    # create bucket for ME index data
    self.bucket_name = f"{PROJECT_ID}-{q_engine.name}-data"
    try:
      bucket = self.storage_client.create_bucket(self.bucket_name,
                                                 location=REGION)
    except Conflict:
      # if bucket already exists, delete and recreate
      bucket = self.storage_client.bucket(self.bucket_name)
      bucket.delete(force=True)
      bucket = self.storage_client.create_bucket(self.bucket_name,
                                                 location=REGION)
    self.bucket_uri = f"gs://{bucket.name}"

  def index_document(self, doc_name: str, text_chunks: List[str],
                          index_base: int) -> int:
    """
    Generate matching engine index data files in a local directory.
    Args:
      doc_name (str): name of document to be indexed
      text_chunks (List[str]): list of text content chunks for document
      index_base (int): index to start from; each chunk gets its own index
    """

    chunk_index = 0
    num_chunks = len(text_chunks)

    # create a list of chunks to process
    while chunk_index < num_chunks:
      remaining_chunks = num_chunks - chunk_index
      chunk_size = min(MAX_NUM_TEXT_CHUNK_PROCESS, remaining_chunks)
      end_chunk_index = chunk_index + chunk_size
      process_chunks = text_chunks[chunk_index:end_chunk_index]

      Logger.info(f"processing {chunk_size} chunks for file {doc_name} "
                  f"remaining chunks {remaining_chunks}")

      # generate np array of chunk IDs starting from index base
      ids = np.arange(index_base, index_base + len(process_chunks))

      # Create temporary folder to write embeddings to
      embeddings_dir = Path(tempfile.mkdtemp())

      # Convert chunks to embeddings in batches, to manage API throttling
      is_successful, chunk_embeddings = embeddings.get_embeddings(
          text_chunks=process_chunks
      )

      Logger.info(f"generated embeddings for chunks"
                  f" {chunk_index} to {end_chunk_index}")

      # create JSON
      embeddings_formatted = [
        json.dumps(
          {
            "id": str(idx),
            "embedding": [str(value) for value in embedding],
          }
        )
        + "\n"
        for idx, embedding in zip(ids[is_successful], chunk_embeddings)
      ]

      # Create output file
      doc_stem = Path(doc_name).stem
      chunk_path = embeddings_dir.joinpath(
          f"{doc_stem}_{index_base}_index.json")

      # write embeddings for chunk to file
      with open(chunk_path, "w", encoding="utf-8") as f:
        f.writelines(embeddings_formatted)

      Logger.info(f"wrote embeddings file for chunks {chunk_index} "
                  f"to {end_chunk_index}")

      # clean up any large data structures
      gc.collect()

      index_base = index_base + len(process_chunks)
      chunk_index = chunk_index + len(process_chunks)

    # copy data files up to bucket
    bucket = self.storage_client.get_bucket(self.bucket_name)
    for root, _, files in os.walk(embeddings_dir):
      for filename in files:
        local_path = os.path.join(root, filename)
        blob = bucket.blob(filename)
        blob.upload_from_filename(local_path)

    Logger.info(f"data uploaded for {doc_name}")

    # clean up tmp files
    shutil.rmtree(embeddings_dir)

    return index_base

  def deploy(self):
    """ Create matching engine index and endpoint """

    # ME index name and description
    index_name = self.q_engine.name.replace("-", "_") + "_MEindex"

    # create ME index
    Logger.info(f"creating matching engine index {index_name}")

    index_description = (
        "Matching Engine index for LLM Service query engine: " + \
        self.q_engine.name)

    tree_ah_index = aiplatform.MatchingEngineIndex.create_tree_ah_index(
        display_name=index_name,
        contents_delta_uri=self.bucket_uri,
        dimensions=DIMENSIONS,
        approximate_neighbors_count=150,
        distance_measure_type="DOT_PRODUCT_DISTANCE",
        leaf_node_embedding_count=500,
        leaf_nodes_to_search_percent=80,
        description=index_description,
    )
    Logger.info(f"Created matching engine index {index_name}")

    # create index endpoint
    index_endpoint = aiplatform.MatchingEngineIndexEndpoint.create(
        display_name=index_name,
        description=index_name,
        public_endpoint_enabled=True,
    )
    Logger.info(f"Created matching engine endpoint for {index_name}")

    # store index in query engine model
    self.q_engine.index_id = tree_ah_index.resource_name
    self.q_engine.index_name = index_name
    self.q_engine.endpoint = index_endpoint.resource_name
    self.q_engine.update()

    # deploy index endpoint
    try:
      # this seems to consistently time out, throwing an error, but
      # actually successfully deploys the endpoint
      index_endpoint.deploy_index(
          index=tree_ah_index,
          deployed_index_id=self.q_engine.deployed_index_name
      )
      Logger.info(f"Deployed matching engine endpoint for {index_name}")
    except Exception as e:
      Logger.error(f"Error creating ME index or endpoint {e}")

  def find_neighbors(self, q_engine: QueryEngine,
                     query_embeddings: List[List[float]]) -> List[int]:
    """
    Retrieve text matches for query embeddings.
    Args:
      q_engine: QueryEngine model
      query_embeddings: list of embedding arrays
    Returns:
      list of indexes that are matched of length NUM_MATCH_RESULTS
    """
    index_endpoint = aiplatform.MatchingEngineIndexEndpoint(q_engine.endpoint)

    match_indexes_list = index_endpoint.find_neighbors(
        queries=query_embeddings,
        deployed_index_id=q_engine.deployed_index_name,
        num_neighbors=NUM_MATCH_RESULTS
    )
    return match_indexes_list


class LangChainVectorStore(VectorStore):
  """
  Generic LLM Service interface to Langchain vector store classes.
  """
  def __init__(self, q_engine: QueryEngine) -> None:
    super().__init__(q_engine)
    self.lc_vector_store = self._get_langchain_vector_store()

  def _get_langchain_vector_store(self) -> LCVectorStore:
    # retrieve langchain vector store obj from config
    lc_vectorstore = LC_VECTOR_STORES.get(self.q_engine.vector_store)
    if lc_vectorstore is None:
      raise InternalServerError(
          f"vector store {self.q_engine.vector_store} not found in config")
    return lc_vectorstore

  def index_document(self, doc_name: str, text_chunks: List[str],
                          index_base: int) -> int:

    # generate np array of chunk IDs starting from index base
    ids = np.arange(index_base, index_base + len(text_chunks))

    # Convert chunks to embeddings
    _, chunk_embeddings = embeddings.get_embeddings(
        text_chunks=text_chunks
    )

    self.lc_vector_store().add_embeddings(texts=text_chunks,
                                          embeddings=chunk_embeddings,
                                          ids=ids)


  def find_neighbors(self, q_engine: QueryEngine,
                     query_embeddings: List[List[float]]) -> List[int]:

    return self.lc_vector_store().similarity_search_by_vector(
        embedding=query_embeddings,
        k=NUM_MATCH_RESULTS
    )

  def deploy(self):
    """ Create matching engine index and endpoint """
    pass



class PostgresVectorStore(LangChainVectorStore):
  """
  LLM Service interface for Postgres Vector Stores, based on langchain
  PGVector VectorStore class.
  """

  def _get_langchain_vector_store(self) -> LCVectorStore:
    
    # get postgres connection string using PGVector utility method
    connection_string = PGVector.connection_string_from_db_params(
        driver="psycopg2",
        host: PG_HOST_NAME,
        port: PG_PORT,
        database: PG_DBNAME,
        user: PG_USER,
        password: PG_PASSWD
    )
    
    # Each query engine is stored in a different PGVector collection,
    # where the collection name is just the query engine name.
    collection_name = self.q_engine.name
    
    # instantiate the langchain vector store object
    langchain_vector_store = PGVector(
        connection_string=connection_string,
        collection_name=collection_name
        )
    
    return langchain_vector_store
  

def from_query_engine(q_engine: QueryEngine) -> VectorStore:
  qe_vector_store = q_engine.vector_store
  if qe_vector_store is None:
    # default to matching engine vector store
    return MatchingEngineVectorStore(q_engine)
  else:
    qe_vector_store_class = VECTOR_STORES.get(qe_vector_store)
    if qe_vector_store_class is None:
      raise InternalServerError(
         f"vector store class {qe_vector_store} not found in config")
    return qe_vector_store_class(q_engine)

