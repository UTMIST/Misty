"""Guard against a wrapped secret reaching code that expects a plain `str`.

Every service stores its credentials as `pydantic.SecretStr` (see this
package's README, "Credential config convention"). `SecretStr` deliberately
does not coerce to `str`, so the caller must unwrap at the boundary with
`.get_secret_value()`. Forgetting to is easy, and the resulting failure is
obscure rather than absent: `secrets.compare_digest` reports a missing
`.encode()` and `key_store_from_config` a missing `.strip()`, both of which
read like a bug in this library rather than in the service's wiring.

This module turns that into one sentence naming the parameter and the fix.

(The genuinely silent failure mode lives in the services, not here: `SecretStr`
never compares equal to a `str`, so a `verify_production_secrets` check written
as `settings.api_key == DEFAULT_DEV_API_KEY` stops firing without raising. Each
service's own test suite pins that one.)

Duck-typed on purpose: `platform_auth` is a leaf that depends only on
fastapi/starlette/argon2, and importing pydantic here just to `isinstance`
against `SecretStr` would drag a new declared dependency into it. Anything
exposing `get_secret_value` is a secret wrapper for our purposes — which also
covers `SecretBytes` and any equivalent from another library.
"""


def reject_secret_wrapper(value: object, *, param: str) -> None:
    """Raise if `value` is a secret wrapper rather than the plain `str` expected.

    Call at the boundary, before the value is used. A no-op for `str`, `None`,
    and everything else — this only catches the specific unwrap mistake.
    """
    if hasattr(value, "get_secret_value"):
        raise TypeError(
            f"{param} received {type(value).__name__}, which platform_auth cannot use. "
            f"Unwrap it at the call site with .get_secret_value() — this package "
            f"deliberately takes plain str so the SecretStr boundary stays in the "
            f"service's own config layer."
        )
