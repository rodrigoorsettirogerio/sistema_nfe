"""Teste manual da integração com a ACBr API (sandbox).

Uso, a partir da raiz do projeto:

    python testar_acbr.py
    python testar_acbr.py --cep 01311200

Por padrão o script só valida a autenticação (`client_credentials`), que é a
base das rotas de Empresa/Distribuição NF-e. A consulta de CEP é opcional: ela
exige a permissão `cep`, que pode não existir na credencial usada para baixar
XMLs. Nenhum segredo é exibido.
"""

import argparse
import json
import sys

from backend.acbr_client import AcbrClient, ErroAcbr


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Testa a autenticação na ACBr API.")
    parser.add_argument(
        "--cep",
        default="",
        help="Consulta também este CEP (ex.: 01311200). Exige a permissão 'cep'.",
    )
    args = parser.parse_args(argv)

    cliente = AcbrClient()

    if not cliente.configurado:
        print("❌ ACBR_CLIENT_ID/ACBR_CLIENT_SECRET não configurados no .env.")
        return 1

    print(
        f"🔐 ACBr API — ambiente={cliente.ambiente} | {cliente.api_url} "
        f"| scopes={cliente.scopes}"
    )
    try:
        token = cliente.autenticar()
        print(f"✅ Autenticado. Token recebido ({len(token)} caracteres).")
        print(
            "   As rotas /api/acbr/empresas e /api/acbr/baixar-xmls já podem "
            "usar este token."
        )

        if args.cep:
            dados = cliente.consultar_cep(args.cep)
            print(f"✅ Consulta do CEP {args.cep} concluída.")
            print("📦 Resposta da API:")
            print(json.dumps(dados, ensure_ascii=False, indent=2))
    except ErroAcbr as exc:
        print(f"❌ {exc}")
        if exc.status is not None:
            print(f"   HTTP {exc.status}")
        if exc.status == 401:
            print(
                "   Confira no Console da ACBr API (console.acbr.api.br) se a "
                "credencial é do tipo Sandbox e se o Client Secret não foi "
                "revogado/regenerado."
            )
        elif exc.status == 403:
            print(
                "   A credencial foi aceita, mas não tem a permissão do endpoint. "
                "Libere os scopes 'empresa', 'distribuicao-nfe' e, para CEP, 'cep'."
            )
        if exc.corpo is not None:
            print("   Corpo da resposta:")
            print(json.dumps(exc.corpo, ensure_ascii=False, indent=2))
        return 1
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
