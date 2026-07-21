"""Mobilizon OAuth + GraphQL client."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .state import AppCredentials, State, TokenBundle

log = logging.getLogger(__name__)

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"

# Scopes required to sync events into a group.
REQUIRED_SCOPES = [
    "read:group:events",
    "read:profile",
    "write:event:create",
    "write:event:update",
    "write:event:delete",
    "write:media:upload",
]


class MobilizonError(RuntimeError):
    pass


class OAuthError(MobilizonError):
    pass


class GraphQLError(MobilizonError):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(_summarize_gql_errors(errors))
        self.errors = errors


def _summarize_gql_errors(errors: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{e.get('message', 'unknown')}"
        + (f" @ {'/'.join(str(p) for p in e['path'])}" if e.get("path") else "")
        for e in errors
    )


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class MobilizonAuth:
    """Handles app registration, device flow, and token refresh."""

    def __init__(self, instance_url: str, state: State, timeout: float = 30.0):
        self.instance_url = instance_url.rstrip("/")
        self.state = state
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    # -- App registration --------------------------------------------------
    def ensure_app(self, name: str, website: str | None) -> AppCredentials:
        creds = self.state.get_app(self.instance_url)
        if creds is not None:
            granted = set(creds.scope.split())
            # Re-register if the stored app is missing any newly-required scopes
            # (e.g. write:media:upload added after the app was first registered).
            if set(REQUIRED_SCOPES).issubset(granted):
                return creds
            log.info(
                "Stored app scope is missing required scopes %s; re-registering app.",
                sorted(set(REQUIRED_SCOPES) - granted),
            )
        scope = " ".join(REQUIRED_SCOPES)
        payload = {
            "name": name,
            # A dummy redirect URI is required by /apps but we use device flow.
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "scope": scope,
        }
        if website:
            payload["website"] = website
        r = self._client.post(f"{self.instance_url}/apps", json=payload)
        if r.status_code >= 400:
            raise OAuthError(f"app registration failed ({r.status_code}): {r.text}")
        body = r.json()
        creds = AppCredentials(
            client_id=body["client_id"],
            client_secret=body["client_secret"],
            scope=body.get("scope") or scope,
        )
        self.state.save_app(self.instance_url, creds)
        return creds

    # -- Device flow -------------------------------------------------------
    def request_device_code(self, creds: AppCredentials, scope: str | None = None) -> DeviceCode:
        scope = scope or creds.scope
        r = self._client.post(
            f"{self.instance_url}/login/device/code",
            data={"client_id": creds.client_id, "scope": scope},
        )
        if r.status_code >= 400:
            raise OAuthError(f"device_code failed ({r.status_code}): {r.text}")
        b = r.json()
        return DeviceCode(
            device_code=b["device_code"],
            user_code=b["user_code"],
            verification_uri=b["verification_uri"],
            expires_in=int(b.get("expires_in", 900)),
            interval=int(b.get("interval", 5)),
        )

    def poll_device_token(
        self,
        creds: AppCredentials,
        dc: DeviceCode,
        on_poll: "callable | None" = None,
    ) -> TokenBundle:
        deadline = time.time() + dc.expires_in
        interval = max(1, dc.interval)
        while time.time() < deadline:
            r = self._client.post(
                f"{self.instance_url}/oauth/token",
                data={
                    "client_id": creds.client_id,
                    "device_code": dc.device_code,
                    "grant_type": DEVICE_GRANT,
                },
            )
            if r.status_code == 200:
                return _token_from_response(r.json())
            try:
                err = r.json().get("error")
            except Exception:
                err = None
            if err == "authorization_pending":
                if on_poll:
                    on_poll("pending")
                time.sleep(interval)
                continue
            if err == "slow_down":
                interval += 5
                if on_poll:
                    on_poll("slow_down")
                time.sleep(interval)
                continue
            # Mobilizon quirk: while the device activation is in the
            # transient :confirmed state (user entered the code but hasn't
            # clicked "Approve" yet) it returns invalid_grant instead of
            # authorization_pending. Treat that as pending too — a truly
            # invalid grant will keep returning invalid_grant until the
            # code expires, and we'll bail on the expiry check above.
            if err == "invalid_grant":
                if on_poll:
                    on_poll("pending")
                time.sleep(interval)
                continue
            raise OAuthError(f"device token exchange failed ({r.status_code}): {r.text}")
        raise OAuthError("device code expired before authorization was granted")

    def refresh(self, creds: AppCredentials, token: TokenBundle) -> TokenBundle:
        r = self._client.post(
            f"{self.instance_url}/oauth/token",
            data={
                "grant_type": REFRESH_GRANT,
                "refresh_token": token.refresh_token,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
            },
        )
        if r.status_code >= 400:
            raise OAuthError(f"refresh failed ({r.status_code}): {r.text}")
        return _token_from_response(r.json())


def _token_from_response(body: dict[str, Any]) -> TokenBundle:
    now = int(time.time())
    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        access_expires_at=now + int(body["expires_in"]),
        refresh_expires_at=now + int(body["refresh_token_expires_in"]),
        scope=body.get("scope") or "",
    )


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------


class MobilizonClient:
    """Authenticated GraphQL client bound to a single org's token bundle."""

    def __init__(
        self,
        instance_url: str,
        org_id: str,
        state: State,
        auth: MobilizonAuth,
        timeout: float = 30.0,
    ):
        self.instance_url = instance_url.rstrip("/")
        self.org_id = org_id
        self.state = state
        self.auth = auth
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def _token(self) -> TokenBundle:
        t = self.state.get_token(self.instance_url, self.org_id)
        if t is None:
            raise OAuthError(
                f"no token stored for org {self.org_id!r}. Run 'login' first."
            )
        if t.access_expired():
            if t.refresh_expired():
                raise OAuthError(
                    f"refresh token expired for org {self.org_id!r}. Run 'login' again."
                )
            creds = self.state.get_app(self.instance_url)
            if creds is None:
                raise OAuthError("app credentials missing; run 'login' again.")
            t = self.auth.refresh(creds, t)
            self.state.save_token(self.instance_url, self.org_id, t)
        return t

    def _is_rate_limited(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return "too many requests" in msg or "rate" in msg or "429" in msg

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._token()

        def do_request() -> httpx.Response:
            return self._client.post(
                f"{self.instance_url}/api",
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {token.access_token}"},
            )

        last_error: Exception | None = None
        for attempt in range(5):
            r = do_request()
            if r.status_code >= 400:
                err = MobilizonError(f"HTTP {r.status_code} from GraphQL: {r.text}")
                if self._is_rate_limited(err) and attempt < 4:
                    delay = 1.0 * (2**attempt) + (attempt * 0.5)
                    log.warning("Rate limited on GraphQL attempt %d, retrying in %.1fs", attempt + 1, delay)
                    time.sleep(delay)
                    continue
                raise err
            body = r.json()
            if body.get("errors"):
                err = GraphQLError(body["errors"])
                if self._is_rate_limited(err) and attempt < 4:
                    delay = 1.0 * (2**attempt) + (attempt * 0.5)
                    log.warning("Rate limited on GraphQL attempt %d, retrying in %.1fs", attempt + 1, delay)
                    time.sleep(delay)
                    continue
                raise err
            return body.get("data", {})
        raise MobilizonError("Max retries exceeded for GraphQL request")  # pragma: no cover

    # -- high-level helpers --------------------------------------------------
    def resolve_organizer_actor_id(self, preferred_username: str | None) -> str:
        """Return one of the current user's own actor IDs.

        If `preferred_username` is provided, pick the matching identity.
        Otherwise (or if it's a placeholder) fall back to the default actor.
        """
        data = self.graphql(
            """
            query {
              loggedUser {
                defaultActor { id preferredUsername }
                actors { id preferredUsername }
              }
            }
            """
        )
        user = data.get("loggedUser") or {}
        actors = user.get("actors") or []
        default_actor = user.get("defaultActor") or None

        wanted = (preferred_username or "").strip()
        if wanted and wanted.upper() != "REPLACE_WITH_YOUR_PROFILE_USERNAME":
            for a in actors:
                if a.get("preferredUsername") == wanted:
                    return a["id"]
            available = ", ".join(a.get("preferredUsername", "?") for a in actors) or "(none)"
            raise MobilizonError(
                f"profile {wanted!r} not found among your identities. Available: {available}"
            )

        if default_actor:
            return default_actor["id"]
        if actors:
            return actors[0]["id"]
        raise MobilizonError("no profiles found on the authenticated user")

    def fetch_group_id(self, preferred_username: str) -> str:
        data = self.graphql(
            """
            query($u: String!) {
              group(preferredUsername: $u) { id preferredUsername }
            }
            """,
            {"u": preferred_username},
        )
        group = data.get("group")
        if not group:
            raise MobilizonError(f"group {preferred_username!r} not found")
        return group["id"]

    def list_group_events(self, preferred_username: str, after_iso: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page = 1
        limit = 100
        while True:
            data = self.graphql(
                """
                query($u: String!, $after: DateTime, $page: Int!, $limit: Int!) {
                  group(preferredUsername: $u) {
                    organizedEvents(afterDatetime: $after, page: $page, limit: $limit) {
                      total
                      elements {
                        id uuid title description
                        beginsOn endsOn status url
                      }
                    }
                  }
                }
                """,
                {"u": preferred_username, "after": after_iso, "page": page, "limit": limit},
            )
            group = data.get("group") or {}
            oe = group.get("organizedEvents") or {}
            batch = oe.get("elements") or []
            events.extend(batch)
            if len(batch) < limit:
                break
            page += 1
        return events

    def create_event(self, variables: dict[str, Any]) -> dict[str, Any]:
        data = self.graphql(_CREATE_EVENT_MUTATION, variables)
        return data["createEvent"]

    def update_event(self, variables: dict[str, Any]) -> dict[str, Any]:
        data = self.graphql(_UPDATE_EVENT_MUTATION, variables)
        return data["updateEvent"]

    def upload_media(self, file_path: str, name: str, actor_id: str | None = None) -> dict[str, Any]:
        """Upload a media file to Mobilizon and return the media object with UUID."""
        import mimetypes
        import os
        from pathlib import Path

        if not os.path.exists(file_path):
            raise MobilizonError(f"File not found: {file_path}")

        file_name = Path(file_path).name
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        url = f"{self.instance_url}/api"
        token = self._token().access_token
        headers = {"Authorization": f"Bearer {token}"}

        # Absinthe.Plug native file upload format (NOT graphql-multipart-request-spec).
        # See: https://hexdocs.pm/absinthe_plug/Absinthe.Plug.Types.html
        # The request must be multipart/form-data with:
        #   - a "query" form field holding the GraphQL document
        #   - a "variables" form field (JSON) where the :upload argument's value
        #     is a STRING matching the name of another form field
        #   - a form field with that name containing the binary file
        #
        # Only include actorId when provided: the resolver falls back to the
        # current user's default actor via Map.get(args, :actor_id, default),
        # which only triggers when the key is ABSENT. Sending actorId: null would
        # insert a NULL actor_id and violate the medias.actor_id NOT NULL constraint.
        if actor_id is not None:
            query = """
            mutation($name: String!, $file: Upload!, $actorId: ID) {
              uploadMedia(name: $name, file: $file, actorId: $actorId) {
                uuid
                url
                name
                alt
              }
            }
            """
            # The value of "file" references the form field holding the binary.
            variables = {"name": name, "file": "file", "actorId": actor_id}
        else:
            query = """
            mutation($name: String!, $file: Upload!) {
              uploadMedia(name: $name, file: $file) {
                uuid
                url
                name
                alt
              }
            }
            """
            variables = {"name": name, "file": "file"}

        with open(file_path, "rb") as f:
            files = {
                "query": (None, query),
                "variables": (None, json.dumps(variables)),
                "file": (file_name, f, content_type),
            }

            for attempt in range(5):
                response = self._client.post(url, headers=headers, files=files)
                if response.status_code >= 400:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    err = MobilizonError(error_msg)
                    if self._is_rate_limited(err) and attempt < 4:
                        delay = 1.0 * (2**attempt) + (attempt * 0.5)
                        log.warning("Rate limited on upload attempt %d, retrying in %.1fs", attempt + 1, delay)
                        time.sleep(delay)
                        continue
                    log.error(f"Upload failed: {error_msg}")
                    raise err
                data = response.json()
                if "errors" in data:
                    err = GraphQLError(data["errors"])
                    if self._is_rate_limited(err) and attempt < 4:
                        delay = 1.0 * (2**attempt) + (attempt * 0.5)
                        log.warning("Rate limited on upload attempt %d, retrying in %.1fs", attempt + 1, delay)
                        time.sleep(delay)
                        continue
                    raise err
                return data["data"]["uploadMedia"]
            raise MobilizonError("Max retries exceeded for upload_media")  # pragma: no cover

    def delete_event(self, event_id: str) -> None:
        self.graphql(
            "mutation($id: ID!) { deleteEvent(eventId: $id) { id } }",
            {"id": event_id},
        )


_CREATE_EVENT_MUTATION = """
mutation CreateEvent(
  $organizerActorId: ID!,
  $attributedToId: ID,
  $title: String!,
  $description: String!,
  $beginsOn: DateTime!,
  $endsOn: DateTime,
  $status: EventStatus,
  $visibility: EventVisibility,
  $category: EventCategory,
  $tags: [String],
  $onlineAddress: String,
  $options: EventOptionsInput,
  $physicalAddress: AddressInput,
  $picture: MediaInput
) {
  createEvent(
    organizerActorId: $organizerActorId,
    attributedToId: $attributedToId,
    title: $title,
    description: $description,
    beginsOn: $beginsOn,
    endsOn: $endsOn,
    status: $status,
    visibility: $visibility,
    category: $category,
    tags: $tags,
    onlineAddress: $onlineAddress,
    options: $options,
    physicalAddress: $physicalAddress,
    picture: $picture
  ) {
    id uuid title beginsOn endsOn status url
  }
}
"""

_UPDATE_EVENT_MUTATION = """
mutation UpdateEvent(
  $eventId: ID!,
  $title: String,
  $description: String,
  $beginsOn: DateTime,
  $endsOn: DateTime,
  $status: EventStatus,
  $visibility: EventVisibility,
  $category: EventCategory,
  $tags: [String],
  $onlineAddress: String,
  $options: EventOptionsInput,
  $physicalAddress: AddressInput,
  $metadata: [EventMetadataInput],
  $picture: MediaInput
) {
  updateEvent(
    eventId: $eventId,
    title: $title,
    description: $description,
    beginsOn: $beginsOn,
    endsOn: $endsOn,
    status: $status,
    visibility: $visibility,
    category: $category,
    tags: $tags,
    onlineAddress: $onlineAddress,
    options: $options,
    physicalAddress: $physicalAddress,
    metadata: $metadata,
    picture: $picture
  ) {
    id uuid title beginsOn endsOn status url options {
      timezone
      maximumAttendeeCapacity
    }
    metadata {
      key
      title
      value
      type
    }
  }
}
"""
