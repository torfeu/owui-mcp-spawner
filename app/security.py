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


def mask_secrets(value):
    """Return a copy of *value* with every secret-named string masked.

    Valve values are `dict[str, Any]`, so a credential can sit one level down —
    `{"connection": {"password": "…"}}` — and the flat version of this function
    handed it straight to the config API and into a backup taken *without*
    secrets, which then labelled itself `contains_secrets: false`.

    A key's own name decides, at whatever depth it sits, and the rule does not
    spread to what is underneath it: `is_secret_field` matches on substrings,
    so "key" and "auth" also fire on "keywords" and "author", and masking a
    whole subtree because of a name like that would blank out settings nobody
    calls secret. Under-masking was the bug; over-masking is not the fix.
    """
    if isinstance(value, dict):
        return {
            k: (SECRET_MASK if is_secret_field(k) and isinstance(v, str) and v
                else mask_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def keep_masked_values(incoming, previous):
    """Return *incoming* with every mask replaced by what *previous* holds there.

    The counterpart to mask_secrets, and the reason it has to exist: a client
    that reads a config and writes it back unchanged sends `********` where the
    credential was. Without this the recursion above would turn a read-then-save
    in the edit dialog into a config whose password is literally eight stars —
    a security fix that destroys credentials is not one.

    A mask with nothing behind it is dropped rather than stored: there was no
    value to keep, and the literal mask is not one either.
    """
    if isinstance(incoming, dict):
        result = {}
        for key, value in incoming.items():
            known = isinstance(previous, dict) and key in previous
            if value == SECRET_MASK:
                if known:
                    result[key] = previous[key]
                continue
            result[key] = keep_masked_values(value, previous[key] if known else None)
        return result
    if isinstance(incoming, list):
        # Positional: a client echoes back the list it was given, in order.
        return [keep_masked_values(
                    item, previous[i] if isinstance(previous, list) and i < len(previous) else None)
                for i, item in enumerate(incoming)]
    return incoming


def drop_masked_values(value, _prefix: str = "") -> tuple[object, list[str]]:
    """Strip masks from *value*, and name where they were.

    Used when a config arrives from somewhere that has no previous value to put
    back — a redacted backup. The paths are what the restore report shows, so
    "this instance came back without its credential" is something the person
    reading the report can see rather than discover on the first call.
    """
    if isinstance(value, dict):
        kept, dropped = {}, []
        for key, item in value.items():
            path = f"{_prefix}{key}"
            if item == SECRET_MASK:
                dropped.append(path)
                continue
            sub, sub_dropped = drop_masked_values(item, f"{path}.")
            kept[key] = sub
            dropped.extend(sub_dropped)
        return kept, dropped
    if isinstance(value, list):
        kept, dropped = [], []
        for i, item in enumerate(value):
            sub, sub_dropped = drop_masked_values(item, f"{_prefix}{i}.")
            kept.append(sub)
            dropped.extend(sub_dropped)
        return kept, dropped
    return value, []
