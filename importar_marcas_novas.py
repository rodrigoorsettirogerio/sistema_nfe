"""Importa a lista de `marcas_novas.txt` para a tabela `marcas_master`.

O arquivo tem uma marca por linha. A importação é IDEMPOTENTE: nomes já
existentes (comparados sem acento, sem diferenciar maiúsculas e com espaços
normalizados) são ignorados, então o script pode ser executado quantas vezes
forem necessárias sem criar duplicatas.

Uso (na raiz do projeto):
    python importar_marcas_novas.py
    python importar_marcas_novas.py --dry-run   # mostra o resumo sem gravar
    python importar_marcas_novas.py --arquivo outra_lista.txt

O script fica no projeto para permitir novas expansões do catálogo: basta
acrescentar nomes ao `marcas_novas.txt` e executar de novo — a carga é
idempotente e coberta por teste (`tests/test_marcas_master.py`).
"""

import argparse
import unicodedata
from pathlib import Path

from backend.database import SessionLocal, init_db
from backend.models import MarcaMaster

RAIZ_PROJETO = Path(__file__).resolve().parent
ARQUIVO_PADRAO = RAIZ_PROJETO / "marcas_novas.txt"
CATEGORIA_PADRAO = "Catálogo expandido"


def normalizar(nome) -> str:
    """Chave de comparação: sem acento, sem caixa e com espaços colapsados."""
    texto = unicodedata.normalize("NFKD", str(nome or ""))
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return " ".join(texto.casefold().split())


def ler_marcas(caminho=ARQUIVO_PADRAO):
    """Lê o arquivo e devolve [(nome, linha)] na ordem, ignorando vazios/comentários."""
    caminho = Path(caminho)
    if not caminho.is_file():
        raise FileNotFoundError(f"Arquivo de marcas não encontrado: {caminho}")
    marcas = []
    for numero, linha in enumerate(
        caminho.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        nome = " ".join(linha.strip().split())
        if not nome or nome.startswith("#"):
            continue
        marcas.append((nome, numero))
    return marcas


def marcas_unicas_do_arquivo(caminho=ARQUIVO_PADRAO):
    """(únicas, repetidas): preserva a primeira grafia encontrada de cada marca."""
    unicas, repetidas, vistos = [], 0, set()
    for nome, _linha in ler_marcas(caminho):
        chave = normalizar(nome)
        if not chave:
            continue
        if chave in vistos:
            repetidas += 1
            continue
        vistos.add(chave)
        unicas.append(nome)
    return unicas, repetidas


def importar_marcas_novas(
    caminho=ARQUIVO_PADRAO,
    db=None,
    categoria=CATEGORIA_PADRAO,
    criar_tabelas=True,
):
    """Insere as marcas novas e devolve o resumo da operação (não duplica)."""
    if criar_tabelas:
        init_db()

    unicas, repetidas_no_arquivo = marcas_unicas_do_arquivo(caminho)
    propria_sessao = db is None
    sessao = db or SessionLocal()
    try:
        existentes = {
            normalizar(nome)
            for (nome,) in sessao.query(MarcaMaster.nome_marca).all()
        }
        ja_existentes = 0
        inseridas = 0
        for nome in unicas:
            if normalizar(nome) in existentes:
                ja_existentes += 1
                continue
            sessao.add(MarcaMaster(nome_marca=nome, categoria=categoria))
            existentes.add(normalizar(nome))
            inseridas += 1
        sessao.commit()
        total_banco = sessao.query(MarcaMaster).count()
        return {
            "arquivo": str(Path(caminho)),
            "linhas_validas": len(ler_marcas(caminho)),
            "marcas_unicas_arquivo": len(unicas),
            "repetidas_no_arquivo": repetidas_no_arquivo,
            "ja_existentes": ja_existentes,
            "inseridas": inseridas,
            "total_banco": total_banco,
        }
    finally:
        if propria_sessao:
            sessao.close()


def main():
    parser = argparse.ArgumentParser(
        description="Importa marcas novas do arquivo de texto para marcas_master."
    )
    parser.add_argument(
        "--arquivo",
        default=str(ARQUIVO_PADRAO),
        help="Arquivo com uma marca por linha (padrão: marcas_novas.txt).",
    )
    parser.add_argument(
        "--categoria",
        default=CATEGORIA_PADRAO,
        help=f"Categoria gravada nas marcas novas (padrão: {CATEGORIA_PADRAO}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o resumo sem gravar no banco.",
    )
    argumentos = parser.parse_args()

    if argumentos.dry_run:
        unicas, repetidas = marcas_unicas_do_arquivo(argumentos.arquivo)
        init_db()
        sessao = SessionLocal()
        try:
            existentes = {
                normalizar(nome)
                for (nome,) in sessao.query(MarcaMaster.nome_marca).all()
            }
            total_banco = len(existentes)
        finally:
            sessao.close()
        novas = [nome for nome in unicas if normalizar(nome) not in existentes]
        print("SIMULAÇÃO (nenhuma marca foi gravada)")
        print(f"  Arquivo: {Path(argumentos.arquivo)}")
        print(f"  Marcas únicas no arquivo: {len(unicas)}")
        print(f"  Repetidas dentro do arquivo: {repetidas}")
        print(f"  Já existentes no banco: {len(unicas) - len(novas)}")
        print(f"  Seriam inseridas: {len(novas)}")
        print(f"  Total atual no banco: {total_banco}")
        print(f"  Total depois da carga: {total_banco + len(novas)}")
        return 0

    resumo = importar_marcas_novas(argumentos.arquivo, categoria=argumentos.categoria)
    print("✅ Importação de marcas concluída.")
    print(f"  Arquivo: {resumo['arquivo']}")
    print(f"  Linhas válidas: {resumo['linhas_validas']}")
    print(f"  Marcas únicas no arquivo: {resumo['marcas_unicas_arquivo']}")
    print(f"  Repetidas dentro do arquivo: {resumo['repetidas_no_arquivo']}")
    print(f"  Já existentes na tabela (ignoradas): {resumo['ja_existentes']}")
    print(f"  Inseridas agora: {resumo['inseridas']}")
    print(f"  Total de marcas únicas na tabela: {resumo['total_banco']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
