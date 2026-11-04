"""Storage for external registries: metadata on the PVC, credentials in one Secret.

The metadata file decides which registries exist. Credentials are written first
on add and removed last on remove, so a failure leaves at worst an unused key,
which the next change prunes. A registry whose key is missing is reported as
needing its credentials re-entered.

Kubernetes errors never leave this module as ApiException: the portal's handler
passes that status to the browser, and a 401 there would log the user out.
"""
import base64
import json
import logging
import threading
from pathlib import Path

from config import get_settings
from utils.exceptions import CanvasPortalError

logger = logging.getLogger(__name__)

MAX_SOURCES = 20

_lock = threading.Lock()


class StoreUnavailable(CanvasPortalError):
    def __init__(self, message: str):
        super().__init__(message, 503)


def _core_api():
    # Imported here because k8s.client reads settings at import time.
    from kubernetes import client
    from k8s.client import get_base_api_client

    return client.CoreV1Api(get_base_api_client())


def _secret_ref() -> tuple[str, str]:
    settings = get_settings()
    name = settings.REGISTRY_CREDENTIALS_SECRET.strip()
    namespace = settings.REGISTRY_CREDENTIALS_NAMESPACE.strip()
    if not name or not namespace:
        raise StoreUnavailable("Credential storage is not configured; upgrade the portal chart.")
    return name, namespace


def _read_secret_data() -> dict:
    name, namespace = _secret_ref()
    try:
        return _core_api().read_namespaced_secret(name, namespace).data or {}
    except Exception as exc:
        logger.warning("Could not read the registry credentials Secret: %s", type(exc).__name__)
        raise StoreUnavailable("Could not read the registry credentials Secret.") from None


def _patch_secret(body: dict) -> None:
    name, namespace = _secret_ref()
    try:
        _core_api().patch_namespaced_secret(name, namespace, body)
    except Exception as exc:
        logger.warning("Could not update the registry credentials Secret: %s", type(exc).__name__)
        raise StoreUnavailable("Could not update the registry credentials Secret.") from None


def read_credentials() -> dict[str, dict]:
    """Return {registry id: credentials}, skipping any entry that does not decode."""
    credentials = {}
    for key, value in _read_secret_data().items():
        try:
            decoded = json.loads(base64.b64decode(value))
        except ValueError:
            logger.warning("Ignoring an unreadable entry in the registry credentials Secret")
            continue
        if isinstance(decoded, dict):
            credentials[key] = decoded
    return credentials


def _catalog_path() -> Path:
    return Path(get_settings().EXTERNAL_REGISTRY_CATALOG_PATH)


def load_sources() -> list[dict]:
    """Return the saved registries, without their credentials."""
    path = _catalog_path()
    try:
        if not path.exists():
            return []
        registries = json.loads(path.read_text(encoding="utf-8")).get("registries")
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("Could not read the external registry catalog: %s", type(exc).__name__)
        raise StoreUnavailable("Could not read the external registry catalog.") from None
    return [entry for entry in registries or [] if isinstance(entry, dict)]


def _write_sources(sources: list[dict]) -> None:
    path = _catalog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps({"version": 1, "registries": sources}, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        logger.warning("Could not write the external registry catalog: %s", type(exc).__name__)
        raise StoreUnavailable("Could not write the external registry catalog.") from None


def _prune_credentials(keep_ids: set[str]) -> None:
    """Remove keys no registry uses. Best effort: an unused key does no harm until the next run."""
    try:
        orphans = set(_read_secret_data()) - keep_ids
        if orphans:
            _patch_secret({"data": {key: None for key in orphans}})
    except StoreUnavailable:
        pass


def add_source(metadata: dict, credentials: dict) -> None:
    """Save a registry, refusing a duplicate name or more than MAX_SOURCES entries."""
    with _lock:
        sources = load_sources()
        if metadata["name"].lower() in {source.get("name", "").lower() for source in sources}:
            raise CanvasPortalError(f"A registry named '{metadata['name']}' already exists", 409)
        if len(sources) >= MAX_SOURCES:
            raise CanvasPortalError(f"At most {MAX_SOURCES} external registries can be configured", 400)

        _patch_secret({"stringData": {metadata["id"]: json.dumps(credentials)}})
        try:
            _write_sources([*sources, metadata])
        except StoreUnavailable:
            _prune_credentials({source.get("id") for source in sources})
            raise
        _prune_credentials({source.get("id") for source in sources} | {metadata["id"]})


def remove_source(source_id: str) -> bool:
    """Remove a registry and its credentials. Returns False if it does not exist."""
    with _lock:
        sources = load_sources()
        remaining = [source for source in sources if source.get("id") != source_id]
        if len(remaining) == len(sources):
            return False
        _write_sources(remaining)
        _prune_credentials({source.get("id") for source in remaining})
        return True
