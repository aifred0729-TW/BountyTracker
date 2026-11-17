from . import yeswehack

ADAPTERS = {yeswehack.NAME: yeswehack}


def resolve(text):
    """Return (adapter, slug) for a program URL or bare slug."""
    for adapter in ADAPTERS.values():
        slug = adapter.parse(text)
        if slug:
            return adapter, slug
    return None, None
