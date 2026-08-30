import re
from packaging.requirements import Requirement, InvalidRequirement

_SAFE_PACKAGE_RE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')
_SECRET_FIELDS = {"api_key", "token", "secret", "password", "key", "auth"}

# Placeholder returned instead of secret values; update endpoints must treat an
# incoming value equal to this as "unchanged", or round-tripping a fetched
# config would overwrite real secrets with the mask.
SECRET_MASK = "********"


def validate_package_spec(spec: str) -> bool:
    """Return True if spec is a safe PyPI requirement string."""
    spec = spec.strip()
    if not spec or spec.startswith(("#", "-", "git+", "http://", "https://")):
        return False
    try:
        req = Requirement(spec)
        # A PEP 508 direct reference ("pkg @ https://host/x.whl") parses as a
        # normal requirement but points pip at an arbitrary URL — exactly what
        # the git+/http prefixes above exist to block. The startswith check
        # never sees it because the spec starts with the package name.
        if req.url:
            return False
        return bool(_SAFE_PACKAGE_RE.match(req.name))
    except InvalidRequirement:
        return False


def is_secret_field(key: str) -> bool:
    """True when *key* names a credential-like value that must be masked."""
    key_lower = key.lower()
    return any(s in key_lower for s in _SECRET_FIELDS)


def mask_secrets(values: dict) -> dict:
    """Return a copy of values with secret fields masked."""
    result = {}
    for k, v in values.items():
        if is_secret_field(k) and isinstance(v, str) and v:
            result[k] = SECRET_MASK
        else:
            result[k] = v
    return result
