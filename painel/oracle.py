"""Remetentes autorizados no Oracle Email Delivery (o relay só aceita envio de endereços cadastrados)."""
from functools import lru_cache
from pathlib import Path

CONFIG = Path.home() / ".oci" / "config"


class OracleError(RuntimeError):
    pass


def available() -> bool:
    try:
        import oci  # noqa: F401
    except ImportError:
        return False
    return CONFIG.exists()


@lru_cache(maxsize=1)
def _client():
    import oci
    cfg = oci.config.from_file(str(CONFIG))
    # timeout curto: o painel não pode travar se a Oracle demorar
    return oci.email.EmailClient(cfg, timeout=(10, 30)), cfg["tenancy"]


def _call(fn):
    import oci
    try:
        return fn()
    except oci.exceptions.ServiceError as e:
        raise OracleError(f"Oracle: {e.message}") from e
    except Exception as e:  # rede, credencial
        raise OracleError(f"Oracle indisponível: {e}") from e


def senders() -> dict[str, tuple[str, str]]:
    """{email: (id, estado)} dos remetentes cadastrados (ignora os apagados)."""
    ed, comp = _client()
    import oci
    items = _call(lambda: oci.pagination.list_call_get_all_results(
        ed.list_senders, compartment_id=comp).data)
    return {s.email_address.lower(): (s.id, s.lifecycle_state) for s in items
            if s.lifecycle_state not in ("DELETED", "DELETING")}


def authorize(email: str) -> None:
    import oci
    ed, comp = _client()
    if email.lower() in senders():
        return
    _call(lambda: ed.create_sender(oci.email.models.CreateSenderDetails(
        compartment_id=comp, email_address=email.lower())))


def revoke(email: str) -> None:
    s = senders().get(email.lower())
    if s:
        ed, _ = _client()
        _call(lambda: ed.delete_sender(s[0]))
