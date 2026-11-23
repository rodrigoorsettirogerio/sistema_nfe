import sys

sys.path.insert(0, "tests")

import test_integridade_relatorios as t

with t.app_test_logado(admin=True, default_timeout=240) as app:
    app.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
    print("exception:", app.exception)
    labels_expander = [elemento.label for elemento in app.expander]
    print("secoes_bruscas:", [l for l in labels_expander if "brusca" in l.lower()])
    captions = [elemento.value for elemento in app.caption]
    print("penultima:", sum("Penúltima compra" in c for c in captions))
    print("ultima:", sum("Última compra" in c for c in captions))
    botoes = [elemento.label or "" for elemento in app.button]
    print("ver_danfe:", sum("Ver DANFE" in label for label in botoes))
    vermelhos = [m.value for m in app.markdown if ":red[" in m.value]
    print("markdowns_vermelhos:", len(vermelhos))
