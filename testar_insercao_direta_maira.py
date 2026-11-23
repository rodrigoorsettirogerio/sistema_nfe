"""Teste manual: insere uma loja direto no banco para a Maira e consulta a API.

O objetivo é separar um problema de BANCO de um problema de CADASTRO:

1. grava uma ``Empresa`` com ``usuario_id=3`` (Maira) usando uma sessão SQLAlchemy,
   sem passar por nenhuma rota de cadastro;
2. sobe uma instância temporária do FastAPI em uma porta livre (não usa a porta
   8000 e não interfere na API que você já tem aberta);
3. gera um JWT da própria Maira e chama ``GET /api/empresas/vinculadas``;
4. confirma que a loja inserida aparece na resposta;
5. remove a loja de teste no final (a não ser que ``--manter`` seja informado).

Exemplos::

    # Insere, verifica a API e limpa tudo no final
    python testar_insercao_direta_maira.py

    # Insere e deixa a loja de teste no banco para conferir a tela do Streamlit
    python testar_insercao_direta_maira.py --manter

    # Remove uma loja de teste que ficou no banco
    python testar_insercao_direta_maira.py --remover 99999999000191
"""

import argparse
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from backend import auth
from backend.database import DB_PATH, SessionLocal, init_db
from backend.models import Empresa, Usuario

USUARIO_ID = 3
EMAIL_MAIRA = "sac@portucho.com.br"
NOME_LOJA_TESTE = "LOJA TESTE INSERCAO DIRETA"


def _cnpj_valido_aleatorio() -> str:
    """CNPJ fictício com dígitos verificadores válidos (prefixo 99)."""
    base = "99" + "".join(str(random.randint(0, 9)) for _ in range(6)) + "0001"

    def digito(parcial: str) -> str:
        pesos = list(range(len(parcial) - 7, 1, -1))
        pesos += list(range(9, 1, -1))
        soma = sum(int(numero) * peso for numero, peso in zip(parcial, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    primeiro = digito(base)
    return base + primeiro + digito(base + primeiro)


def _porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mostrar_log(caminho_log: str) -> None:
    try:
        with open(caminho_log, encoding="utf-8") as arquivo:
            conteudo = arquivo.read().strip()
    except OSError:
        return
    if conteudo:
        print("\n--- últimas linhas do log da API temporária ---")
        print(conteudo[-4000:])


def _aguardar_api(base_api: str, processo: subprocess.Popen, caminho_log: str) -> None:
    limite = time.time() + 30
    while time.time() < limite:
        if processo.poll() is not None:
            raise RuntimeError(
                f"A API temporária encerrou com código {processo.returncode}."
            )
        try:
            resposta = requests.get(base_api + "/", timeout=1)
            if resposta.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError("A API temporária não respondeu em 30 segundos.")


def _remover_loja(cnpj: str) -> int:
    cnpj_limpo = "".join(filter(str.isdigit, cnpj or ""))
    db = SessionLocal()
    try:
        empresas = (
            db.query(Empresa)
            .filter(Empresa.cnpj == cnpj_limpo, Empresa.usuario_id == USUARIO_ID)
            .all()
        )
        if not empresas:
            print(
                f"ℹ️ Nenhuma loja de teste com CNPJ {cnpj_limpo} "
                f"e usuario_id={USUARIO_ID} encontrada."
            )
            return 0
        for empresa in empresas:
            db.delete(empresa)
        db.commit()
        print(
            f"🧹 Removida(s) {len(empresas)} loja(s) com CNPJ {cnpj_limpo} "
            f"do usuário {USUARIO_ID}."
        )
        return 0
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Insere uma empresa direto no banco para a Maira e valida a API."
    )
    parser.add_argument(
        "--manter",
        action="store_true",
        help="Não remove a loja de teste no final (para conferir a tela).",
    )
    parser.add_argument(
        "--remover",
        metavar="CNPJ",
        help="Apenas remove a loja de teste indicada (não insere nada).",
    )
    args = parser.parse_args(argv)

    if args.remover:
        return _remover_loja(args.remover)

    init_db()
    print(f"📂 Banco usado: {DB_PATH}")

    db = SessionLocal()
    empresa_id = None
    sucesso = False
    processo = None
    caminho_log = ""
    try:
        usuario = db.query(Usuario).filter(Usuario.id == USUARIO_ID).first()
        if usuario is None:
            print(f"❌ Usuário id={USUARIO_ID} não encontrado.")
            return 1
        if (usuario.email or "").strip().lower() != EMAIL_MAIRA:
            print(
                f"❌ O usuário id={USUARIO_ID} é {usuario.email!r}, "
                f"não a Maira ({EMAIL_MAIRA!r}). Nada foi inserido."
            )
            return 1

        cnpj = _cnpj_valido_aleatorio()
        while db.query(Empresa).filter(Empresa.cnpj == cnpj).first() is not None:
            cnpj = _cnpj_valido_aleatorio()

        empresa = Empresa(
            cnpj=cnpj,
            razao_social="EMPRESA TESTE INSERCAO DIRETA LTDA",
            nome_loja=NOME_LOJA_TESTE,
            municipio="SAO PAULO",
            logradouro="RUA DO TESTE",
            numero="100",
            bairro="CENTRO",
            codigo_municipio="3550308",
            uf="SP",
            cep="01001000",
            usuario_id=USUARIO_ID,
            ultimo_nsu_acbr="0",
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
        empresa_id = empresa.id

        print("✅ Empresa inserida DIRETO no banco:")
        print(f"   id={empresa.id}")
        print(f"   CNPJ={empresa.cnpj}")
        print(
            f"   usuario_id={empresa.usuario_id} "
            f"({usuario.nome} / {usuario.email})"
        )

        token, _ = auth.criar_token(usuario)
        porta = _porta_livre()
        base_api = f"http://127.0.0.1:{porta}"
        descritor, caminho_log = tempfile.mkstemp(prefix="api_maira_", suffix=".log")
        os.close(descritor)
        with open(caminho_log, "w", encoding="utf-8") as log:
            processo = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(porta),
                    "--log-level",
                    "warning",
                ],
                cwd=str(RAIZ),
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        _aguardar_api(base_api, processo, caminho_log)
        print(f"🌐 API temporária ativa em {base_api}")

        resposta = requests.get(
            base_api + "/api/empresas/vinculadas",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resposta.status_code != 200:
            raise RuntimeError(
                f"GET /api/empresas/vinculadas respondeu HTTP "
                f"{resposta.status_code}: {resposta.text[:500]}"
            )
        lojas = resposta.json()
        if not isinstance(lojas, list):
            raise RuntimeError(f"Resposta inesperada da API: {lojas!r}")

        encontrada = next(
            (loja for loja in lojas if str(loja.get("cnpj")) == cnpj), None
        )
        if encontrada is None:
            raise RuntimeError(
                f"A loja {cnpj} foi inserida no banco, mas não apareceu em "
                f"GET /api/empresas/vinculadas. Lojas retornadas: "
                f"{[loja.get('cnpj') for loja in lojas]}"
            )
        if encontrada.get("usuario_id") != USUARIO_ID:
            raise RuntimeError(
                f"A loja apareceu com usuario_id={encontrada.get('usuario_id')}, "
                f"esperado {USUARIO_ID}."
            )

        print("✅ GET /api/empresas/vinculadas retornou a loja inserida:")
        print(
            f"   id={encontrada.get('id')} | CNPJ={encontrada.get('cnpj')} | "
            f"usuario_id={encontrada.get('usuario_id')} | "
            f"nome={encontrada.get('nome_loja')!r}"
        )
        print(f"   Total de lojas devolvidas para a Maira: {len(lojas)}")
        sucesso = True

        if args.manter:
            print(
                "\n⚠️ --manter informado: a loja de teste continua no banco. "
                "Para removê-la depois, rode:\n"
                f"   python testar_insercao_direta_maira.py --remover {cnpj}"
            )
            return 0

        db.delete(empresa)
        db.commit()
        empresa_id = None
        print("🧹 Loja de teste removida do banco.")

        conferencia = requests.get(
            base_api + "/api/empresas/vinculadas",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if conferencia.status_code == 200:
            restantes = {
                str(loja.get("cnpj")) for loja in conferencia.json() or []
            }
            if cnpj in restantes:
                raise RuntimeError(
                    "A loja de teste ainda aparece na API depois da remoção."
                )
            print(
                "✅ GET /api/empresas/vinculadas não retorna mais a loja de teste "
                f"(total atual: {len(restantes)})."
            )
        return 0
    except Exception as erro:
        print(f"\n❌ Falha no teste: {erro}")
        _mostrar_log(caminho_log)
        return 1
    finally:
        if empresa_id is not None and not (args.manter and sucesso):
            limpeza = SessionLocal()
            try:
                restante = (
                    limpeza.query(Empresa)
                    .filter(Empresa.id == empresa_id)
                    .first()
                )
                if restante is not None:
                    limpeza.delete(restante)
                    limpeza.commit()
                    print("🧹 Loja de teste removida na limpeza final.")
            finally:
                limpeza.close()
        db.close()
        if processo is not None:
            processo.terminate()
            try:
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                processo.kill()
                processo.wait(timeout=5)
        if caminho_log and os.path.exists(caminho_log):
            try:
                os.remove(caminho_log)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

