"""PORTAL_TITLE_20260913: título explícito do intake vence o derivado do texto; validado e limitado."""
from hermes_cli import nfos_delivery as delivery


def test_explicit_title_is_validated():
    assert delivery.explicit_request_title({"title": "  Erro: cargo 301   não puxa disciplinas\n"}) == "Erro: cargo 301 não puxa disciplinas"
    assert delivery.explicit_request_title({"title": "   "}) is None
    assert delivery.explicit_request_title({"title": 42}) is None
    assert delivery.explicit_request_title({}) is None
    long = "Erro: " + "palavra " * 60
    out = delivery.explicit_request_title({"title": long})
    assert len(out) <= delivery.REQUEST_TITLE_MAX + 1 and out.endswith("…")
