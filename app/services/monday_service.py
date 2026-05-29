# app/services/monday_service.py

import httpx
from app.core.config import settings

# ─────────────────────────────────────────
# exchange_code_for_token()
# ─────────────────────────────────────────
# This function is used in OAuth flow
# It exchanges "code" (received from monday.com)
# into an "access_token"
async def exchange_code_for_token(code: str):

    # Monday OAuth token endpoint
    url = "https://auth.monday.com/oauth2/token"

    # Request payload (required by monday.com)
    payload = {
        "client_id": settings.monday_client_id,
        "client_secret": settings.monday_client_secret,
        "code": code,
        "redirect_uri": f"{settings.app_base_url}/oauth/callback"
    }

    # Create async HTTP client
    async with httpx.AsyncClient() as client:

        # Send POST request to monday.com
        response = await client.post(
            url,
            data=payload        # Sending data as JSON
        )

    # If request failed, raise error
    response.raise_for_status()

    # Return response as JSON (contains access_token)
    return response.json()


# ─────────────────────────────────────────
# get_monday_me()
# ─────────────────────────────────────────
# This function fetches user info from monday.com using GraphQL API
async def get_monday_me(access_token: str):

    # GraphQL query to get current user ("me")
    query = """
    query {
      me {
        id
        name
        email
        account {
          id
          name
        }
      }
    }
    """

    # Headers with authentication token
    headers = {
        "Authorization": access_token,   # Access token from OAuth
        "Content-Type": "application/json"
    }
    

    # Create async HTTP client
    async with httpx.AsyncClient() as client:

        # Send POST request to monday GraphQL API
        response = await client.post(
            "https://api.monday.com/v2",
            json={"query": query},
            headers=headers
        )

    # Raise error if request fails
    response.raise_for_status()

    # Return user data
    return response.json()