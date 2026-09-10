"""Barrinha de progresso do notificador do kanban (Telegram): funções puras."""
from gateway import kanban_watchers as kw


def test_parse_stage_line():
    assert kw._progress_parse("[etapa 4/7 testar] rodou a suíte | prova: npm run test:ci | próximo: abrir PR") == (4, "testar", "abrir PR")
    assert kw._progress_parse("[etapa 2/7] reproduzido") == (2, "reproduzir", "")
    assert kw._progress_parse("[etapa 9/7 x]") is None
    assert kw._progress_parse("comentário comum") is None


def test_budget_classes():
    assert kw._progress_budget(2700) == (80, "P")
    assert kw._progress_budget(7200) == (250, "M")
    assert kw._progress_budget(14400) == (500, "G")
    assert kw._progress_budget(None) == (250, "-")


def test_render_bar_and_closeout():
    text = kw._progress_render("t_10a026a6", 4, "testar", 38, 91, 250, "M", "abrir PR")
    assert text.startswith("▰▰▰▰▱▱▱ 4/7 testar · t_10a026a6 · 38 min · 91/250 tools · classe M")
    assert text.endswith("próximo: abrir PR")
    assert kw._progress_render("t_x", 7, "entregue", 57, 64, 80, "P", done=True).startswith("▰▰▰▰▰▰▰ 7/7 entregue")


def test_recebido_with_criterion():
    text = kw._progress_recebido("t_x", "Título", "M", "corpo\nCritério de aceite: filtro por revenue funciona\n")
    assert text == "Recebido · t_x · Título · classe M\npronto quando: filtro por revenue funciona"


def test_notify_kinds_filter():
    cfg = lambda: {"kanban": {"notify_kinds": ["completed", "blocked"]}}
    assert kw._notify_kind_allowed("claimed", cfg) is False
    assert kw._notify_kind_allowed("completed", cfg) is True
    assert kw._notify_kind_allowed("claimed", lambda: {}) is True
