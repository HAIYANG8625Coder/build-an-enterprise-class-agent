"""Who is asking. Tokens are random, shown once, and stored only as SHA-256."""
import hashlib
import re
import secrets
import time

# names end up in file paths, so they are checked here and nowhere else may build a path from unchecked text
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class Clock:
    def now(self):
        return time.time()


def digest(token):
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def check_name(text):
    if not isinstance(text, str) or not NAME.fullmatch(text):
        raise ValueError("a name is 1 to 32 of a-z, 0-9, _ and -, and starts with a letter or a digit")
    return text


def create_user(store, user_id, role="user"):
    """user_id is name@tenant. Returns the bearer token. It cannot be read back later."""
    name, _, tenant = user_id.partition("@")
    check_name(name)
    check_name(tenant)
    token = secrets.token_urlsafe(32)
    store.add_user(user_id, tenant, role, digest(token))
    return token


def user_from_header(store, header):
    """The user for an "Authorization: Bearer <token>" header, or None."""
    kind, _, token = (header or "").partition(" ")
    if kind != "Bearer" or not token.strip():
        return None
    return store.user_by_token(digest(token.strip()))
