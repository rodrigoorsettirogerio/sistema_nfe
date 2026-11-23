"""Reimporta todas as notas do zero (atalho do importador oficial).

Este script é mantido só como atalho: a importação de verdade — com NCM, CFOP,
exclusão de bonificação e vínculo da nota com a loja (CNPJ destinatário) — mora em
`import_xmls.py`. Assim não existem duas versões da mesma lógica.

Uso (na raiz do projeto):
    python reimportar_zerado.py

ATENÇÃO: como o nome diz, apaga as notas/itens e importa tudo de novo a partir de
`xmls/`. Os itens da Auditoria (aprovações e regras de agrupamento) são preservados.
"""

from import_xmls import importar_com_ean_e_ncm

if __name__ == "__main__":
    importar_com_ean_e_ncm()
