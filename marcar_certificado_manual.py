"""Marca à mão a "validade do certificado" das lojas que JÁ estão vinculadas.

O vínculo de loja novo passa pela barreira do certificado digital (backend/certificados.py).
As lojas cadastradas antes dessa barreira existir ficaram sem os dados públicos do
certificado — e o cartão em "Minhas Lojas" mostra o aviso "Sem certificado digital
cadastrado". Este script preenche esses três campos (`certificado_titular`,
`certificado_validade`, `certificado_enviado_em`) SEM validar nenhum arquivo: é uma
marcação manual, para liberar a tela sem esperar o upload do .pfx.

O que é gravado:
- `certificado_titular`    = razão social da loja (o nome que já está no banco);
- `certificado_validade`   = data informada em --validade (padrão: hoje + 365 dias);
- `certificado_enviado_em` = "preenchimento-manual" (deixa claro que não houve upload).

Risco assumido: a data é uma informação NÃO conferida no certificado real. Assim que a
loja for vinculada de novo com o .pfx/.p12 + senha, a barreira sobrescreve estes campos
com os dados verdadeiros do certificado.

Uso:
    python marcar_certificado_manual.py                  # simulação (não grava nada)
    python marcar_certificado_manual.py --aplicar         # grava nas lojas da conta admin
    python marcar_certificado_manual.py --aplicar --cnpj 07269638000171 --cnpj 50951160000194
    python marcar_certificado_manual.py --aplicar --validade 31/12/2027
    python marcar_certificado_manual.py --aplicar --email outro@cliente.com
    python marcar_certificado_manual.py --aplicar --remover   # limpa os campos (volta o aviso)
"""

import argparse
import re
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import func

from backend.database import SessionLocal, init_db
from backend.models import Empresa, Usuario

EMAIL_PADRAO = "rodrigo@portucho.com.br"
DIAS_VALIDADE_PADRAO = 365
ORIGEM_MANUAL = "preenchimento-manual"
CAMPOS = ("certificado_titular", "certificado_validade", "certificado_enviado_em")


def _formatar_cnpj(cnpj) -> str:
    """XX.XXX.XXX/XXXX-XX (o banco guarda só os 14 dígitos)."""
    digitos = re.sub(r"\D", "", str(cnpj or ""))
    if len(digitos) != 14:
        return str(cnpj or "")
    return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"


def _ler_validade(texto) -> date:
    """DD/MM/AAAA informada em --validade, sempre no futuro."""
    try:
        validade = datetime.strptime(str(texto).strip(), "%d/%m/%Y").date()
    except ValueError:
        raise SystemExit(f"Data inválida em --validade: {texto!r} (use DD/MM/AAAA).")
    if validade <= date.today():
        raise SystemExit(
            f"A validade {validade:%d/%m/%Y} já passou; informe uma data futura."
        )
    return validade


def _titular(empresa: Empresa) -> str:
    """Nome que aparece como titular do certificado (o app não exibe, mas fica no JSON)."""
    return (
        empresa.razao_social
        or empresa.nome_loja
        or f"CNPJ {_formatar_cnpj(empresa.cnpj)}"
    )


def _lojas_alvo(db, cnpjs, email) -> list:
    """CNPJs informados em --cnpj ou, sem eles, todas as lojas da conta `email`."""
    if cnpjs:
        digitos = sorted({re.sub(r"\D", "", str(cnpj)) for cnpj in cnpjs})
        return (
            db.query(Empresa)
            .filter(Empresa.cnpj.in_(digitos))
            .order_by(Empresa.id)
            .all()
        )

    usuario = (
        db.query(Usuario).filter(func.lower(Usuario.email) == email.strip().lower()).first()
    )
    if not usuario:
        raise SystemExit(
            f"Não encontrei a conta {email!r}. Use --cnpj para escolher as lojas pelo CNPJ."
        )
    return (
        db.query(Empresa)
        .filter(Empresa.usuario_id == usuario.id)
        .order_by(Empresa.id)
        .all()
    )


def _situacao(empresa: Empresa) -> str:
    if empresa.certificado_validade:
        return f"válido até {empresa.certificado_validade}"
    return "SEM certificado"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Preenche à mão a validade do certificado das lojas já vinculadas."
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="grava no banco (sem esta opção o script só mostra o que faria)",
    )
    parser.add_argument(
        "--remover",
        action="store_true",
        help="limpa os campos de certificado (o cartão volta a pedir o .pfx)",
    )
    parser.add_argument(
        "--cnpj",
        action="append",
        default=[],
        metavar="CNPJ",
        help="CNPJ da loja (pode repetir); padrão: todas as lojas de --email",
    )
    parser.add_argument(
        "--validade",
        default=None,
        metavar="DD/MM/AAAA",
        help=f"validade a gravar (padrão: hoje + {DIAS_VALIDADE_PADRAO} dias)",
    )
    parser.add_argument(
        "--email",
        default=EMAIL_PADRAO,
        help=f"conta dona das lojas (padrão: {EMAIL_PADRAO})",
    )
    argumentos = parser.parse_args(argv)

    validade = date.today() + timedelta(days=DIAS_VALIDADE_PADRAO)
    if argumentos.validade and not argumentos.remover:
        validade = _ler_validade(argumentos.validade)

    init_db()
    db = SessionLocal()
    try:
        empresas = _lojas_alvo(db, argumentos.cnpj, argumentos.email)
        if not empresas:
            print("Nenhuma loja encontrada para os filtros informados — nada a fazer.")
            return 0

        if argumentos.remover:
            acao = "limpar os dados do certificado"
        else:
            acao = f"marcar como válido até {validade:%d/%m/%Y}"
        print(f"{'Gravando' if argumentos.aplicar else 'Simulação'} ({acao}):\n")

        for empresa in empresas:
            antes = _situacao(empresa)
            previsto = (
                "SEM certificado" if argumentos.remover else f"válido até {validade:%d/%m/%Y}"
            )
            if argumentos.aplicar:
                if argumentos.remover:
                    for campo in CAMPOS:
                        setattr(empresa, campo, None)
                else:
                    empresa.certificado_titular = _titular(empresa)
                    empresa.certificado_validade = f"{validade:%d/%m/%Y}"
                    empresa.certificado_enviado_em = ORIGEM_MANUAL
                depois = _situacao(empresa)  # relê o que ficou no objeto
            else:
                depois = previsto
            nome = empresa.nome_loja or empresa.razao_social or "(sem nome)"
            print(f"  {_formatar_cnpj(empresa.cnpj)}  {nome:<28} {antes} -> {depois}")

        if not argumentos.aplicar:
            print("\nNada foi gravado. Repita com --aplicar para valer.")
            return 0

        db.commit()
        print(f"\n{len(empresas)} loja(s) atualizada(s) no banco.")
        print(
            "Recarregue a página do app (o cartão de 'Minhas Lojas' fica em cache por 30 s) "
            "para ver a validade no lugar do aviso."
        )
        print(
            "Lembrete: estes dados são manuais. Ao vincular a loja de novo com o .pfx/.p12 "
            "+ senha, a barreira grava os dados reais do certificado."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
