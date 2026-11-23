"""Vincula cada nota à loja (empresa) correta: CNPJ DESTINATÁRIO do XML.

Antes este script jogava TODAS as notas na primeira empresa cadastrada, o que impedia o
filtro por loja e o comparativo entre lojas de funcionar. A regra correta (multilojas)
vive em `backfill_relatorios.py`, junto com o backfill do CFOP dos itens.

Uso (na raiz do projeto):
    python vincular_notas.py
"""

from backfill_relatorios import vincular_notas_as_empresas

if __name__ == "__main__":
    vincular_notas_as_empresas()
