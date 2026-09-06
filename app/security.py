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


class AmbiguousMask(Exception):
    """A masked value in a list cannot be traced back to what it stood for.

    Raised rather than guessed. See keep_masked_values().
    """


def mask_secrets(value):
    """Return a copy of *value* with every secret-named string masked.

    Valve values are `dict[str, Any]`, so a credential can sit one level down —
    `{"connection": {"password": "…"}}` — and the flat version of this function
    handed it straight to the config API and into a backup taken *without*
    secrets, which then labelled itself `contains_secrets: false`.

    A key's own name decides, at whatever depth it sits. Where that name has to
    carry the decision alone, it does: a **list** under a secret name has no
    inner names to judge by, so its strings are masked — `{"api_keys": ["…"]}`
    is the same secret as `{"api_key": "…"}`, and leaving it in the clear was
    the gap this recursion closed everywhere else. A **dictionary** under a
    secret name is different: it brings its own keys, and those are the better
    evidence, so the rule stops there and judges the leaves by their own names.

    That does mean `{"keywords": ["birds"]}` comes back masked, because
    `is_secret_field` matches on substrings and "key" fires on "keywords". Not
    lovely — but `{"keywords": "birds"}` has been masked that way since the
    first version of this function, so masking the list is what *agrees* with
    the rest, and the write-back below puts the value straight back.
    """
    if isinstance(value, dict):
        return {k: (_mask_named_secret(v) if is_secret_field(k) else mask_secrets(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def _mask_named_secret(value):
    """Mask a value whose *own key* named it a secret."""
    if isinstance(value, str):
        return SECRET_MASK if value else value
    if isinstance(value, list):
        return [_mask_named_secret(v) for v in value]
    if isinstance(value, dict):
        # Its keys say more than the name above it does.
        return mask_secrets(value)
    return value


def _fits(incoming, previous) -> bool:
    """True when *incoming* can only be the edited form of *previous*.

    Everything that is not a mask has to match. A mask matches whatever stands
    in its place, because that is exactly what it was standing in for.
    """
    if incoming == SECRET_MASK:
        return True
    if isinstance(incoming, dict):
        if not isinstance(previous, dict) or set(incoming) != set(previous):
            return False
        return all(_fits(v, previous[k]) for k, v in incoming.items())
    if isinstance(incoming, list):
        if not isinstance(previous, list) or len(incoming) != len(previous):
            return False
        return all(_fits(a, b) for a, b in zip(incoming, previous))
    return incoming == previous


def keep_masked_values(incoming, previous):
    """Return *incoming* with every mask replaced by what *previous* holds there.

    The counterpart to mask_secrets, and the reason it has to exist: a client
    that reads a config and writes it back unchanged sends `********` where the
    credential was. Without this the recursion above would turn a read-then-save
    in the edit dialog into a config whose password is literally eight stars —
    a security fix that destroys credentials is not one.

    In a **dictionary** the key says which value a mask stood for, and that is
    the end of it. In a **list** there is no such name, and the first version of
    this function used the position — which is right only as long as nobody
    touches the list. The edit dialog shows structures as JSON and lets them be
    edited, so deleting the first of two accounts handed the second account the
    first one's token: not a lost secret, a *swapped* one, which is worse,
    because it fails as a wrong login rather than as a missing one.

    So a list entry is matched by everything about it that is *not* masked. As
    long as the list still has the same length, the entry's own position is
    tried first and used when it still fits — that carries the ordinary
    open-and-save, an edit in place, and two identical entries alike. When it
    does not fit, or the length changed at all, position proves nothing and the
    entry has to fit exactly one previous entry; that carries deletion and
    reordering of entries that can be told apart.

    Fitting none or several is refused with `AmbiguousMask` rather than
    guessed. Two cases land there. An entry renamed while its secret stayed
    masked is indistinguishable from a new one. And deleting from a list of
    bare secrets — `["********", "********"]` — leaves nothing at all to match
    on, which is precisely the case that used to hand the survivor the wrong
    value. The honest answer in both is to ask for the secret again.

    A mask with nothing behind it is dropped rather than stored: there was no
    value to keep, and the literal mask is not one either.
    """
    return _keep(incoming, previous, "")


def _keep(incoming, previous, path: str):
    if isinstance(incoming, dict):
        result = {}
        for key, value in incoming.items():
            here = f"{path}{key}"
            known = isinstance(previous, dict) and key in previous
            if value == SECRET_MASK:
                if known:
                    result[key] = previous[key]
                continue
            result[key] = _keep(value, previous[key] if known else None, f"{here}.")
        return result

    if isinstance(incoming, list):
        old = previous if isinstance(previous, list) else []
        same_length = len(incoming) == len(old)
        result = []
        for i, item in enumerate(incoming):
            if not _holds_mask(item):
                # Nothing to trace back: whatever it is, it is what was sent.
                result.append(_keep(item, None, f"{path}{i}."))
                continue
            if same_length and _fits(item, old[i]):
                match = old[i]
            else:
                candidates = [prev for prev in old if _fits(item, prev)]
                if len(candidates) != 1:
                    raise AmbiguousMask(
                        f"{path.rstrip('.') or 'values'}: entry {i + 1} of this list "
                        "still holds a masked value, but it "
                        + ("no longer matches any of the previous entries"
                           if not candidates else
                           f"matches {len(candidates)} of them equally well")
                        + " — the value it stood for cannot be traced back. Enter it "
                        "instead of leaving the mask, or put the entry back as it was."
                    )
                match = candidates[0]
            # A mask that *is* the entry has no key to look it up by; the entry
            # it was matched to is the value it stood for.
            result.append(match if item == SECRET_MASK
                          else _keep(item, match, f"{path}{i}."))
        return result

    return incoming


def _holds_mask(value) -> bool:
    if value == SECRET_MASK:
        return True
    if isinstance(value, dict):
        return any(_holds_mask(v) for v in value.values())
    if isinstance(value, list):
        return any(_holds_mask(v) for v in value)
    return False


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
            path = f"{_prefix}{i}"
            if item == SECRET_MASK:
                # Now that a list of bare secrets is masked, a redacted backup
                # can carry one. The entry goes rather than being installed as
                # the literal mask — same rule as for a masked field.
                dropped.append(path)
                continue
            sub, sub_dropped = drop_masked_values(item, f"{path}.")
            kept.append(sub)
            dropped.extend(sub_dropped)
        return kept, dropped
    return value, []
