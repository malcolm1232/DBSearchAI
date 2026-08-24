// DBSearch.AI — data-plane infrastructure (deploys INTO the customer's subscription).
// One resource group per tenant = isolation by construction (LAW 5). All data stays here;
// nothing in this file talks to the control plane.
//
// Deploy:
//   az group create -n dbsearch-acme -l eastus
//   az deployment group create -g dbsearch-acme -f infra/main.bicep -p namePrefix=dbsacme
//
// See docs/DEPLOY_AZURE.md for the full runbook (Entra app + Graph consent come first).

@description('Short, globally-unique-ish prefix for resource names (lowercase, <=11 chars).')
param namePrefix string

@description('Azure region.')
param location string = resourceGroup().location

@description('Free/dev mode: switch the two cost-driver SKUs (AI Search basic->free, Service Bus Standard->Basic) and Doc Intelligence S0->F0 free tier, so a trial runs at ~$0. Limits: one Free search + one F0 DocIntel per subscription; Free search = 50MB/3 indexes (enough for a demo corpus); Service Bus Basic = queues only (our pipeline uses queues). AOAI stays pay-per-call.')
param dev bool = false

@description('Embedding model + version for Azure OpenAI.')
param embeddingModel string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'

@description('Chat model + version for Azure OpenAI. As of 2026-06 gpt-4o-mini and gpt-4.1-mini are deprecated/blocked for new deployments; gpt-5.1 (2025-11-13) is the current model supporting the Standard SKU (depr 2027-05-15).')
param chatModel string = 'gpt-5.1'
param chatModelVersion string = '2025-11-13'

var storageName = toLower('${namePrefix}stor')
var sbNamespace = '${namePrefix}-bus'  // Azure reserves namespaces ending in '-sb'
var searchName = '${namePrefix}-search'
var aoaiName = '${namePrefix}-aoai'
var docIntelName = '${namePrefix}-docintel'
var kvName = '${namePrefix}-kv'

// --- Blob storage (raw docs, extracted text, embeddings) ---
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { allowBlobPublicAccess: false, minimumTlsVersion: 'TLS1_2' }
}
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'dbsearch'
}

// --- Service Bus + the three pipeline queues (LAW 4) ---
// Service Bus: Standard normally; Basic in dev (queues only — that's all the pipeline uses).
resource sb 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: sbNamespace
  location: location
  sku: dev ? { name: 'Basic', tier: 'Basic' } : { name: 'Standard', tier: 'Standard' }
}
resource qParse 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sb
  name: 'parse'
}
resource qChunk 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sb
  name: 'chunkembed'
}
resource qIndex 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sb
  name: 'index'
}

// --- Azure AI Search (hybrid vector + keyword, security trimming) ---
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  sku: dev ? { name: 'free' } : { name: 'basic' }
  properties: { replicaCount: 1, partitionCount: 1 }
}

// --- Azure OpenAI + model deployments ---
resource aoai 'Microsoft.CognitiveServices/accounts@2023-10-01-preview' = {
  name: aoaiName
  location: location
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: { customSubDomainName: aoaiName }
}
resource embedDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-10-01-preview' = {
  parent: aoai
  name: embeddingModel
  sku: { name: 'Standard', capacity: 50 }
  properties: { model: { format: 'OpenAI', name: embeddingModel, version: embeddingModelVersion } }
}
resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-10-01-preview' = {
  parent: aoai
  name: chatModel
  dependsOn: [ embedDeployment ]
  sku: { name: 'Standard', capacity: 50 }
  properties: { model: { format: 'OpenAI', name: chatModel, version: chatModelVersion } }
}

// --- Document Intelligence (OCR / text extraction) ---
resource docintel 'Microsoft.CognitiveServices/accounts@2023-10-01-preview' = {
  name: docIntelName
  location: location
  kind: 'FormRecognizer'
  sku: dev ? { name: 'F0' } : { name: 'S0' }
  properties: { customSubDomainName: docIntelName }
}

// --- Key Vault (secrets) ---
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

output blobAccountUrl string = storage.properties.primaryEndpoints.blob
output servicebusNamespace string = '${sbNamespace}.servicebus.windows.net'
output searchEndpoint string = 'https://${searchName}.search.windows.net'
output aoaiEndpoint string = aoai.properties.endpoint
output docintelEndpoint string = docintel.properties.endpoint
output keyvaultUrl string = kv.properties.vaultUri
output embeddingDeployment string = embeddingModel
output chatDeployment string = chatModel
