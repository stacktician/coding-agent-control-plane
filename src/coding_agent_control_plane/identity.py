"""Workload identity. Baseline: one shared Keycloak client-credentials token."""

import httpx

from coding_agent_control_plane.config import Settings


async def fetch_gateway_token(settings: Settings) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            settings.keycloak_token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.keycloak_client_id,
                "client_secret": settings.kc_agent_worker_secret.get_secret_value(),
            },
        )
        response.raise_for_status()
        token: str = response.json()["access_token"]
        return token
