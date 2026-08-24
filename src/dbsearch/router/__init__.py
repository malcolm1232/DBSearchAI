"""Phase E — multi-store query router (card #96).

E1: the Semantic Catalog, the uniform StorePort + Evidence envelope, an IndexedStore
over the existing QueryService, the hereditary catalog-visibility gate (#1), and the
'compose for databases' provisioning seam (StoreProviderPort + registry + local
provider + manifest loader). E2: the hybrid router brains (route() → RoutingDecision).
E3: fan-out execution + heterogeneous synthesis (ask() → RouterResult).
"""
from dbsearch.router.evidence import CHUNK, RECORD, ROW, Evidence
from dbsearch.router.store import (
    ANALYTICAL, EXACT, FEDERATED_DOC, FEDERATED_SQL, INDEXED, NATIVE_SEARCH, SEMANTIC,
    AccessContext, StorePort, StoreProfile,
)
from dbsearch.router.catalog import (
    BUSINESS_UNIT, SOURCE, STORE, TENANT, CatalogNode, StoreCatalog,
)
from dbsearch.router.indexed_store import IndexedStore
from dbsearch.router.provider import (
    ProviderRegistry, StoreProviderPort, get_provider, register_provider,
)
from dbsearch.router.identity_broker import (
    IdentityBroker, OboTokenExchange, StaticTokenExchange, TokenExchangePort,
    EntraOboExchange, EntraRefreshExchange, GcpWifExchange, AwsStsExchange, AwsKeysExchange,
    GoogleRefreshExchange, env_subject_token_provider, exchange_from_config,
)
from dbsearch.router.native_search import (
    GraphSearchProvider, GraphSearchStore, env_token_provider,
)
from dbsearch.router.structured import (
    CsvSqlProvider, FederatedSqlStore, SqlEnginePort, SqliteEngine,
    keyword_sql_generator, llm_sql_generator, memoized_sql_generator, validate_sql,
)
from dbsearch.router.providers.azure_sql import (
    AzureSqlEngine, AzureSqlProvider,
)
from dbsearch.router.providers.bigquery import (
    BigQueryEngine, BigQueryProvider,
)
from dbsearch.router.providers.redshift import (
    RedshiftEngine, RedshiftProvider,
)
from dbsearch.router.providers.databricks import (
    DatabricksEngine, DatabricksProvider,
)
from dbsearch.router.providers.postgres import (
    PostgresEngine, PostgresProvider, RdsPostgresEngine, RdsPostgresProvider,
)
from dbsearch.router.providers.mysql import (
    MySqlEngine, MySqlProvider, RdsMySqlEngine, RdsMySqlProvider,
)
from dbsearch.router.providers.synapse import (
    SynapseProvider,
)
from dbsearch.router.providers.cosmos import (
    CosmosEngine, CosmosProvider, CosmosStore, keyword_cosmos_query,
    llm_cosmos_generator, validate_cosmos_query,
)
from dbsearch.router.providers.connector import (
    ConnectorBackedStore, ConnectorStoreProvider,
    folder_connector_factory, gdrive_connector_factory, s3_connector_factory,
    sharepoint_connector_factory, sharepoint_link_connector_factory,
)
from dbsearch.router.providers.local import LocalIndexProvider
from dbsearch.router.provisioning import (
    load_manifest, register_delegations, resolve_env,
)
from dbsearch.router.secret_handles import ScopedSecretResolver, format_handle
from dbsearch.router.classify import classify_query
from dbsearch.router.decompose import (decompose_query, llm_cross_store_planner,
                                        llm_decomposer)
from dbsearch.router.structured import CANNOT_ANSWER, CannotAnswerFromSchema
from dbsearch.router.decision import RoutedStore, RoutingDecision, SubQuery
from dbsearch.router.executor import (
    BUDGET, EMPTY, ERROR, OK, TIMEOUT, DispatchReport, StoreOutcome, execute,
)
from dbsearch.router.synthesizer import (
    RouterResult, citations_from, compound_disclosure, disclosure_from,
    merge_evidence, synthesize,
)
from dbsearch.router.router_service import RouterQueryService

__all__ = [
    "Evidence", "CHUNK", "ROW", "RECORD",
    "StorePort", "StoreProfile", "AccessContext",
    "SEMANTIC", "ANALYTICAL", "EXACT", "INDEXED", "FEDERATED_SQL", "FEDERATED_DOC",
    "NATIVE_SEARCH", "GraphSearchStore", "GraphSearchProvider", "env_token_provider",
    "IdentityBroker", "OboTokenExchange", "StaticTokenExchange", "TokenExchangePort",
    "EntraOboExchange", "EntraRefreshExchange", "GcpWifExchange", "AwsStsExchange",
    "AwsKeysExchange",
    "GoogleRefreshExchange",
    "env_subject_token_provider", "exchange_from_config", "register_delegations",
    "FederatedSqlStore", "SqlEnginePort", "SqliteEngine", "CsvSqlProvider",
    "AzureSqlEngine", "AzureSqlProvider",
    "BigQueryEngine", "BigQueryProvider", "RedshiftEngine", "RedshiftProvider",
    "DatabricksEngine", "DatabricksProvider",
    "PostgresEngine", "PostgresProvider", "RdsPostgresEngine", "RdsPostgresProvider",
    "MySqlEngine", "MySqlProvider", "RdsMySqlEngine", "RdsMySqlProvider",
    "SynapseProvider",
    "CosmosEngine", "CosmosProvider", "CosmosStore",
    "keyword_cosmos_query", "llm_cosmos_generator", "validate_cosmos_query",
    "keyword_sql_generator", "llm_sql_generator", "memoized_sql_generator", "validate_sql",
    "StoreCatalog", "CatalogNode", "TENANT", "BUSINESS_UNIT", "SOURCE", "STORE",
    "IndexedStore",
    "StoreProviderPort", "ProviderRegistry", "register_provider", "get_provider",
    "LocalIndexProvider", "load_manifest", "resolve_env",
    "ScopedSecretResolver", "format_handle",
    "ConnectorStoreProvider", "ConnectorBackedStore",
    "folder_connector_factory", "gdrive_connector_factory", "s3_connector_factory",
    "sharepoint_connector_factory", "sharepoint_link_connector_factory",
    "RouterQueryService", "RoutingDecision", "RoutedStore", "SubQuery",
    "classify_query", "decompose_query", "llm_cross_store_planner", "llm_decomposer",
    "CANNOT_ANSWER", "CannotAnswerFromSchema",
    "execute", "DispatchReport", "StoreOutcome", "OK", "EMPTY", "ERROR", "TIMEOUT", "BUDGET",
    "synthesize", "RouterResult", "merge_evidence", "citations_from", "disclosure_from",
    "compound_disclosure",
]
