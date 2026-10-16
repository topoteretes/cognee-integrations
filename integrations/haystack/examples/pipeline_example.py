"""CogneeRetriever wired into a real RAG pipeline: retriever -> prompt -> chat generator.

Needs an OpenAI key (LLM_API_KEY) and something already stored in cognee —
run example.py first, or point COGNEE_PROJECT_NODE_SET at a dataset that
already has data.
"""

from cognee_integration_haystack import CogneeRetriever
from haystack import Pipeline
from haystack.components.builders import PromptBuilder
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.utils import Secret

pipeline = Pipeline()
pipeline.add_component("retriever", CogneeRetriever(top_k=5))
pipeline.add_component(
    "prompt",
    PromptBuilder(
        template=(
            "Answer the question using only the context below.\n\n"
            "Context:\n{% for d in documents %}{{ d.content }}\n{% endfor %}\n"
            "Question: {{ query }}"
        ),
        required_variables=["documents", "query"],
    ),
)
pipeline.add_component(
    "llm",
    OpenAIChatGenerator(api_key=Secret.from_env_var("LLM_API_KEY"), model="gpt-4o-mini"),
)
pipeline.connect("retriever.documents", "prompt.documents")
pipeline.connect("prompt.prompt", "llm.messages")

query = "What is the Meditech Solutions contract worth?"
result = pipeline.run({"retriever": {"query": query}, "prompt": {"query": query}})

print(result["llm"]["replies"][0].text)
