"""Azure adapters (ADR 0001/0004) — real implementations of the ports (Phase 1b).

Import submodules directly (the factory does this lazily for backend=azure), so the
local/in-memory backend never needs the azure SDKs installed:

    blob.AzureBlobStore(ObjectStorePort)        servicebus.ServiceBusQueue(QueuePort)
    docintel.DocIntelligenceExtractor(Extractor) aoai.AzureOpenAIEmbedding(EmbeddingPort)
    aisearch.AISearchIndex(IndexPort)            entra.EntraIdentity(IdentityPort)
    aoai.AzureOpenAILlm(LlmPort)                 keyvault.KeyVaultSecrets(SecretsPort)

RULE (Gate, LAW 7): `azure.*` / `openai` imports live ONLY in this package. If one
appears elsewhere in core/pipeline/query, that's a violation — move it here.
"""
