"""Write a document into cognee, then retrieve it back with CogneeRetriever.

No LLM-backed pipeline step needed here beyond what cognee itself uses to
build the graph; see pipeline_example.py for a retriever wired into a
generator.
"""

from cognee_integration_haystack import CogneeRetriever, CogneeWriter
from haystack import Document, Pipeline

write_pipeline = Pipeline()
write_pipeline.add_component("writer", CogneeWriter())

write_pipeline.run(
    {
        "writer": {
            "documents": [
                Document(content="We signed a contract with Meditech Solutions for £1.2M."),
                Document(content="The Meditech contract renews annually every March."),
            ]
        }
    }
)

read_pipeline = Pipeline()
read_pipeline.add_component("retriever", CogneeRetriever(top_k=5))

query = "What is the Meditech Solutions contract worth?"
result = read_pipeline.run({"retriever": {"query": query}})

for doc in result["retriever"]["documents"]:
    print(f"- {doc.content}")
