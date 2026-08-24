"""RDS IAM database auth (ADR 0026, #814) - the caller's STS triple becomes a database
password.

`generate_db_auth_token` is a LOCAL SigV4 presign - boto3 computes it without a network
call - producing a 15-minute token that RDS accepts as the password for a DB user granted
`rds_iam` (postgres) / AWSAuthenticationPlugin (mysql). Minted here from the delegated
STS triple ADR 0024's aws_keys exchange already hands every AWS engine, so each caller
authenticates as their OWN IAM principal; AWS's `rds-db:connect` permission gates each
caller source-side.

ONE home for the mint, shared by both RDS engines - a second copy is how the two
inevitably drift (#368/#799 shape).
"""
from __future__ import annotations

import json
import re

# An RDS/Aurora endpoint spells its region as the segment before rds.amazonaws.com:
# mydb.abc123.ap-southeast-1.rds.amazonaws.com (instances and clusters alike).
_HOST_REGION = re.compile(r"\.([a-z0-9-]+)\.rds\.amazonaws\.com$", re.IGNORECASE)


def region_from_host(host: str) -> str:
    m = _HOST_REGION.search(host or "")
    return m.group(1) if m else ""


def mint_token(config: dict, credential: str, port: int,
               rds_client_factory=None) -> str:
    """Redeem the delegated STS triple into an IAM auth token for the CONFIGURED db user.

    `rds_client_factory(region, cred_dict)` is the test seam, same idiom as the redshift
    engine's `user_client_factory` - the default builds the real boto3 client (lazy, LAW 7).
    """
    cred = json.loads(credential)
    region = config.get("region") or region_from_host(config.get("host", ""))
    if not region:
        raise ValueError(
            f"{config.get('kind') or 'rds'} store '{config.get('id', '?')}': cannot "
            f"determine the AWS region from host '{config.get('host', '')}' - set "
            "`region` in the store config (a custom/proxy endpoint does not spell it)")
    if rds_client_factory is None:
        def rds_client_factory(region, cred):
            import boto3   # lazy optional dep, only at connect time (LAW 7)

            return boto3.client("rds", region_name=region,
                                aws_access_key_id=cred["access_key_id"],
                                aws_secret_access_key=cred["secret_access_key"],
                                aws_session_token=cred["session_token"])
    client = rds_client_factory(region, cred)
    return client.generate_db_auth_token(DBHostname=config["host"], Port=port,
                                         DBUsername=config["user"])
