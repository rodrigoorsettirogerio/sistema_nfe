"""Motor de alertas de preço — mensagens de WhatsApp.

Este módulo NÃO recalcula preço médio e NÃO mexe em nenhum relatório: ele recebe a MESMA
lista de itens que os relatórios usam (`main._carregar_itens_relatorio`, que já deixa de
fora bonificação/doação e as descrições pendentes na Auditoria), compara a **última** com
a **penúltima** compra de cada produto dentro da janela escolhida pelo cliente e monta o
texto da mensagem.

Regras (as mesmas dos relatórios, para o alerta não "inventar" número):
- variação de 50% ou mais = **divergência** (a aberração do Impacto Financeiro); ela nunca
  é anunciada como aumento/economia, só como "verificar variação brusca";
- aumento/queda entram com o corte `variacao_minima` (%) da configuração do cliente;
- itens com menos de duas compras na janela ficam fora (nada mudou no período).

O envio real no WhatsApp (HTTP para a API do WhatsApp) fica para uma etapa seguinte: aqui só se
monta e se simula a mensagem. O envio por e-mail usa `backend/email_service.py` (SMTP) e o
endereço salvo em `configuracoes_alerta.email`. O token da API do WhatsApp é GLOBAL
(`configuracoes_sistema`) e só o administrador o cadastra — /api/admin/configuracoes.
"""

import re
from datetime import date, datetime, timedelta
from typing import Optional

# Mesmo limiar de aberração dos relatórios (backend/main.py: LIMIAR_ABERRACAO_PCT).
# A partir dele a linha recebe o selo "⚠️ conferir" — mas NÃO é escondida: qualquer
# variação de preço entra no alerta (decisão do cliente).
LIMIAR_DIVERGENCIA_PCT = 50.0
# Corte de variação: 0 = toda diferença de preço entra (não existe mais "mínimo").
VARIACAO_MINIMA_PADRAO = 0.0
JANELA_PADRAO_DIAS = 30
JANELAS_VALIDAS = (7, 15, 30, 60, 90)

# Chave da configuração GLOBAL (tabela configuracoes_sistema).
CHAVE_TOKEN_WHATSAPP = "whatsapp_token"

# Quantidade de itens por mensagem e o teto de caracteres: o WhatsApp recusa mensagens
# acima de 4096 caracteres, então a mensagem é dividida em partes antes de sair.
LIMITE_ITENS_POR_MENSAGEM = 8
LIMITE_CARACTERES_MENSAGEM = 3500
# Teto de produtos citados na mensagem (o resto vira "e mais N produtos"): sem isso uma
# conta grande geraria dezenas de partes e o alerta deixaria de ser lido.
LIMITE_LINHAS_ALERTA = 40

ERRO_TELEFONE_OBRIGATORIO = "Informe o WhatsApp que vai receber os alertas (DDD + número)."
ERRO_TELEFONE_INVALIDO = (
    "WhatsApp inválido: informe DDD + número (ex.: 11 99999-9999) ou o número com o "
    "código do país (ex.: 55 11 99999-9999)."
)
ERRO_EMAIL_OBRIGATORIO = "Informe o e-mail que vai receber os alertas."
ERRO_EMAIL_INVALIDO = "E-mail inválido: informe um endereço no formato nome@dominio.com."
ERRO_EMAIL_SECUNDARIO_INVALIDO = (
    "E-mail secundário inválido: informe um endereço no formato nome@dominio.com."
)
ERRO_CANAL = "Escolha o canal do alerta: WhatsApp, E-mail ou Ambos."
ERRO_VARIACAO = "A variação mínima precisa estar entre 0% e 100%."
ERRO_JANELA = "A janela do alerta precisa ser de 7, 15, 30, 60 ou 90 dias."

# Canais aceitos pela configuração do cliente. `canal` é gravado no banco e usado
# tanto pela simulação quanto pelo disparo real (quando o envio for habilitado).
CANAIS_VALIDOS = ("whatsapp", "email", "ambos")
ROTULOS_CANAIS = {
    "whatsapp": "WhatsApp",
    "email": "E-mail",
    "ambos": "WhatsApp + E-mail",
}


# ──────────────────────────────────────────────────────────────────────────────
# Formatação (padrão brasileiro, o mesmo texto exibido no app)
# ──────────────────────────────────────────────────────────────────────────────
def formatar_moeda(valor) -> str:
    """Valor em reais: 8.5 -> 'R$ 8,50'."""
    texto = f"{float(valor or 0.0):,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_pct(valor) -> str:
    """Variação com sinal e uma casa decimal: 18.123 -> '+18,1%'."""
    return f"{float(valor or 0.0):+.1f}%".replace(".", ",")


def formatar_data(valor) -> str:
    """date/ISO -> 'DD/MM/AAAA' (string vazia quando não há data)."""
    if not valor:
        return ""
    if isinstance(valor, date):
        return f"{valor:%d/%m/%Y}"
    texto = str(valor).strip()
    for formato, tamanho in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return f"{datetime.strptime(texto[:tamanho], formato):%d/%m/%Y}"
        except ValueError:
            continue
    return texto


def mascarar_token(valor) -> str:
    """Token para exibição: só os 4 últimos caracteres ('••••1a2b')."""
    token = str(valor or "")
    if not token:
        return ""
    return "••••" + token[-4:]


# ──────────────────────────────────────────────────────────────────────────────
# Telefone e validação da configuração
# ──────────────────────────────────────────────────────────────────────────────
def normalizar_telefone(valor) -> tuple:
    """Telefone do WhatsApp só com dígitos, no formato internacional.

    Aceita "11 99999-9999", "+55 11 99999-9999" ou "5511999999999". Devolve
    (telefone, None) quando válido e ("", mensagem) quando não. O "+55" é acrescentado
    quando o número vem só com DDD (10 ou 11 dígitos).
    """
    digitos = re.sub(r"\D", "", str(valor or ""))
    if not digitos:
        return "", ERRO_TELEFONE_OBRIGATORIO
    if len(digitos) <= 11:
        digitos = "55" + digitos
    if len(digitos) not in (12, 13) or not digitos.startswith("55"):
        return "", ERRO_TELEFONE_INVALIDO
    return digitos, None


def normalizar_email(valor) -> tuple:
    """E-mail do alerta em minúsculas. Devolve (email, None) ou ("", erro)."""
    texto = str(valor or "").strip().lower()
    if not texto:
        return "", ERRO_EMAIL_OBRIGATORIO
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", texto):
        return "", ERRO_EMAIL_INVALIDO
    return texto, None


def normalizar_canal(valor) -> tuple:
    """Canal escolhido -> (whatsapp|email|ambos, None) ou ("", erro)."""
    texto = str(valor or "").strip().lower()
    if not texto:
        return "whatsapp", None  # registros/telas antigas continuam no WhatsApp
    aliases = {
        "whatsapp": "whatsapp",
        "whats": "whatsapp",
        "wpp": "whatsapp",
        "email": "email",
        "e-mail": "email",
        "e_mail": "email",
        "ambos": "ambos",
        "both": "ambos",
        "todos": "ambos",
    }
    canal = aliases.get(texto)
    if canal not in CANAIS_VALIDOS:
        return "", ERRO_CANAL
    return canal, None


def canais_de_envio(canal) -> tuple:
    """Canais efetivamente usados no disparo (ex.: ``ambos`` -> whatsapp, email)."""
    normalizado, erro = normalizar_canal(canal)
    if erro:
        normalizado = "whatsapp"
    if normalizado == "ambos":
        return ("whatsapp", "email")
    return (normalizado,)


def telefone_para_exibicao(digitos) -> str:
    """'+5511999999999' -> '+55 11 99999-9999' (só para a tela)."""
    numero = re.sub(r"\D", "", str(digitos or ""))
    if len(numero) not in (12, 13):
        return numero
    ddi, ddd, resto = numero[:2], numero[2:4], numero[4:]
    if len(resto) == 9:
        return f"+{ddi} {ddd} {resto[:5]}-{resto[5:]}"
    return f"+{ddi} {ddd} {resto[:4]}-{resto[4:]}"


def validar_configuracao(
    ativo,
    telefone,
    variacao_minima,
    janela_dias,
    email="",
    canal="whatsapp",
    email_secundario="",
) -> tuple:
    """Confere a configuração do cliente. Devolve (dados_normalizados, erro).

    O destino é validado conforme o `canal`: WhatsApp exige telefone, e-mail exige
    e-mail e "ambos" exige os dois. Desligar o alerta não obriga a preencher nada; um
    telefone/e-mail já digitado, porém, precisa ser válido. `email_secundario` é
    opcional (vai no "Cc" da mensagem), mas também precisa ser um endereço válido.
    """
    try:
        variacao = float(variacao_minima)
    except (TypeError, ValueError):
        return {}, ERRO_VARIACAO
    if not 0.0 <= variacao <= 100.0:
        return {}, ERRO_VARIACAO
    try:
        janela = int(janela_dias)
    except (TypeError, ValueError):
        return {}, ERRO_JANELA
    if janela not in JANELAS_VALIDAS:
        return {}, ERRO_JANELA

    canal_normalizado, erro_canal = normalizar_canal(canal)
    if erro_canal:
        return {}, erro_canal
    # Um alerta ativo precisa do destino do canal escolhido. Na simulação (`ativo=False`)
    # o destino é opcional; se o cliente digitou algo, ainda assim precisa ser válido.
    exige_whatsapp = bool(ativo) and canal_normalizado in ("whatsapp", "ambos")
    exige_email = bool(ativo) and canal_normalizado in ("email", "ambos")

    digitos = ""
    if exige_whatsapp or str(telefone or "").strip():
        digitos, erro = normalizar_telefone(telefone)
        if erro:
            return {}, erro

    email_normalizado = ""
    if exige_email or str(email or "").strip():
        email_normalizado, erro_email = normalizar_email(email)
        if erro_email:
            return {}, erro_email

    email_secundario_normalizado = ""
    if str(email_secundario or "").strip():
        email_secundario_normalizado, erro_secundario = normalizar_email(
            email_secundario
        )
        if erro_secundario:
            return {}, ERRO_EMAIL_SECUNDARIO_INVALIDO

    return (
        {
            "ativo": bool(ativo),
            "telefone": digitos,
            "email": email_normalizado,
            "email_secundario": email_secundario_normalizado,
            "canal": canal_normalizado,
            "variacao_minima": round(variacao, 2),
            "janela_dias": janela,
        },
        None,
    )



# ──────────────────────────────────────────────────────────────────────────────
# Comparação de preços (mesma base dos relatórios)
# ──────────────────────────────────────────────────────────────────────────────
def linhas_de_variacao(
    itens,
    janela_dias: int = JANELA_PADRAO_DIAS,
    referencia: Optional[date] = None,
    variacao_minima: float = VARIACAO_MINIMA_PADRAO,
) -> list:
    """Última x penúltima compra de cada produto DENTRO da janela.

    `itens` são os dicionários de `main._carregar_itens_relatorio` (chave, descricao,
    loja, fornecedor, valor, data...). Cada linha devolvida traz o tipo já classificado
    pelo SINAL da variação:

    - "aumento": preço atual maior que o da compra anterior;
    - "queda": preço atual menor.

    Regra do cliente: **qualquer** diferença de preço entra (o corte
    `variacao_minima` é 0 por padrão e só existe para quem chamar a função com outro
    valor). Variação de 50% ou mais ganha ``conferir=True`` — é um selo/selo de
    conferência no texto, nunca um filtro (unidade/cadastro podem explicar o salto).
    Produto com menos de duas compras no período não gera alerta (nada mudou).
    """
    itens = list(itens or [])
    if referencia is None:
        datas = [item["data"] for item in itens if item.get("data")]
        referencia = max(datas) if datas else None
    inicio = referencia - timedelta(days=int(janela_dias)) if referencia else None

    por_chave = {}
    for item in itens:
        if item.get("data") is None:
            continue
        if inicio is not None and item["data"] < inicio:
            continue
        por_chave.setdefault(item["chave"], []).append(item)

    linhas = []
    for chave, compras in por_chave.items():
        compras = sorted(compras, key=lambda item: (item["data"], item.get("id") or 0))
        if len(compras) < 2:
            continue
        atual, anterior = compras[-1], compras[-2]
        if anterior["valor"] <= 0 or atual["valor"] <= 0:
            continue
        variacao = (atual["valor"] / anterior["valor"] - 1) * 100
        if abs(round(atual["valor"], 4) - round(anterior["valor"], 4)) == 0:
            continue  # preço igual: nada mudou para o cliente
        if variacao >= variacao_minima and variacao > 0:
            tipo = "aumento"
        elif variacao <= -variacao_minima:
            tipo = "queda"
        else:
            continue
        linhas.append(
            {
                "chave": chave,
                "produto": _nome_produto(compras),
                "loja": atual.get("loja") or "Sem loja",
                "fornecedor": atual.get("fornecedor") or "",
                "preco_atual": round(atual["valor"], 4),
                "preco_anterior": round(anterior["valor"], 4),
                "variacao_pct": round(variacao, 2),
                "data_atual": atual["data"],
                "data_anterior": anterior["data"],
                "consumo_periodo": round(
                    sum(item.get("quantidade") or 0.0 for item in compras), 3
                ),
                "tipo": tipo,
                "conferir": abs(variacao) >= LIMIAR_DIVERGENCIA_PCT,
            }
        )
    linhas.sort(key=lambda linha: abs(linha["variacao_pct"]), reverse=True)
    return linhas


def _nome_produto(compras) -> str:
    """Descrição mais frequente do grupo (o mesmo rótulo das tabelas dos relatórios)."""
    contagem = {}
    for item in compras:
        descricao = str(item.get("descricao") or "").strip() or "Produto sem descrição"
        contagem[descricao] = contagem.get(descricao, 0) + 1
    return sorted(contagem.items(), key=lambda par: (-par[1], -len(par[0])))[0][0]



# ──────────────────────────────────────────────────────────────────────────────
# Montagem da mensagem
# ──────────────────────────────────────────────────────────────────────────────
def _linha_texto(indice: int, linha: dict) -> str:
    """Uma linha da mensagem: produto, de/por preço, variação e origem da compra."""
    icone = {"aumento": "🔺", "queda": "🔻"}.get(linha["tipo"], "•")
    partes = [
        f"{indice}. {icone} *{linha['produto']}*",
        f"   {formatar_moeda(linha['preco_anterior'])} → "
        f"{formatar_moeda(linha['preco_atual'])} ({formatar_pct(linha['variacao_pct'])})",
    ]
    if linha.get("conferir"):
        partes.append(
            "   _⚠️ conferir: variação acima de 50% — confira unidade/cadastro antes "
            "de usar o preço._"
        )
    complemento = " • ".join(
        texto
        for texto in (
            linha.get("loja") if linha.get("loja") not in (None, "", "Sem loja") else "",
            linha.get("fornecedor") or "",
            f"compra de {formatar_data(linha.get('data_atual'))}"
            if linha.get("data_atual")
            else "",
        )
        if texto
    )
    if complemento:
        partes.append(f"   _{complemento}_")
    return "\n".join(partes)


def _cabecalho(contexto: dict) -> str:
    """Cabeçalho da mensagem: lojas, janela e corte da variação."""
    rotulo = contexto.get("lojas_rotulo") or "Minhas lojas"
    periodo = (
        f"{formatar_data(contexto.get('inicio'))} a "
        f"{formatar_data(contexto.get('referencia'))}"
        if contexto.get("inicio") and contexto.get("referencia")
        else "todo o histórico"
    )
    minimo = str(round(float(contexto.get("variacao_minima") or 0.0), 1)).replace(".", ",")
    return (
        f"*🔔 Alerta de Preços — {rotulo}*\n"
        f"_Período: {periodo} • toda variação em relação à última compra "
        f"(corte atual {minimo}%)_"
    )


def _dividir_em_partes(cabecalho: str, blocos: list) -> list:
    """Divide as linhas em mensagens abaixo do limite de caracteres do WhatsApp.

    Cada parte repete o cabeçalho e recebe "(parte X de Y)" — assim uma lista grande
    chega inteira sem ser recusada pela API (limite de 4096 caracteres por mensagem).
    """
    partes = []
    atual = [cabecalho]
    contador = 0
    for bloco in blocos:
        candidato = "\n\n".join(atual + [bloco])
        if len(atual) > 1 and (
            len(candidato) > LIMITE_CARACTERES_MENSAGEM
            or contador >= LIMITE_ITENS_POR_MENSAGEM
        ):
            partes.append("\n\n".join(atual))
            atual = [cabecalho]
            contador = 0
        atual.append(bloco)
        contador += 1
    partes.append("\n\n".join(atual))
    if len(partes) == 1:
        return partes
    return [
        f"{parte}\n\n_(parte {indice} de {len(partes)})_"
        for indice, parte in enumerate(partes, 1)
    ]


def montar_mensagens(
    linhas, contexto: Optional[dict] = None, config: Optional[dict] = None
) -> dict:
    """Mensagens de WhatsApp + resumo numérico a partir das linhas de variação.

    Regra do cliente: **toda** variação entra (aumentos e quedas). `incluir_aumentos`
    desliga a lista inteira (usado pela simulação); `incluir_divergencias` continua
    aceito por compatibilidade, mas NÃO esconde mais as variações de 50%+ — elas
    aparecem com o selo "⚠️ conferir". O resumo conta tudo o que foi encontrado.
    """
    contexto = contexto or {}
    config = config or {}
    incluir_aumentos = bool(config.get("incluir_aumentos", True))

    aumentos = [linha for linha in linhas if linha["tipo"] == "aumento"]
    quedas = [linha for linha in linhas if linha["tipo"] == "queda"]
    divergencias = [linha for linha in linhas if linha.get("conferir")]

    # Teto de produtos citados: aumentos/quedas do maior para o menor impacto. O que
    # sobra vira a linha "e mais N produto(s)".
    limite = max(1, int(config.get("limite_linhas") or LIMITE_LINHAS_ALERTA))
    visiveis = list(aumentos) + list(quedas) if incluir_aumentos else []
    exibidas = visiveis[:limite]
    ocultas = len(visiveis) - len(exibidas)

    blocos = []
    if incluir_aumentos and aumentos:
        exibir = [linha for linha in exibidas if linha["tipo"] == "aumento"]
        if exibir:
            blocos.append(f"*📈 Aumentos ({len(aumentos)})*")
            blocos.extend(
                _linha_texto(indice, linha) for indice, linha in enumerate(exibir, 1)
            )
    if incluir_aumentos and quedas:
        exibir = [linha for linha in exibidas if linha["tipo"] == "queda"]
        if exibir:
            blocos.append(f"*📉 Quedas ({len(quedas)})*")
            blocos.extend(
                _linha_texto(indice, linha) for indice, linha in enumerate(exibir, 1)
            )
    if ocultas:
        blocos.append(
            f"_… e mais {ocultas} produto(s) com variação no período "
            f"(mensagem limitada a {limite} itens)._"
        )

    if not blocos:
        blocos = [
            "✅ Nenhuma variação de preço no período. Nada para você resolver agora."
        ]

    resumo = {
        "aumentos": len(aumentos),
        "quedas": len(quedas),
        # `divergencias` mantém o nome antigo da API: agora é a CONTAGEM de linhas com
        # o selo "conferir" (elas também estão em aumentos/quedas).
        "divergencias": len(divergencias),
        "conferir": len(divergencias),
        "total": len(aumentos) + len(quedas),
        "linhas_exibidas": len(exibidas),
        "linhas_omitidas": max(0, ocultas),
    }
    rodape = (
        f"_{resumo['aumentos']} aumento(s) • {resumo['quedas']} queda(s) • "
        f"{resumo['divergencias']} para conferir_"
    )
    textos = _dividir_em_partes(_cabecalho(contexto), blocos)
    mensagens = [
        {
            "titulo": f"Alerta de Preços ({indice}/{len(textos)})",
            "texto": f"{texto}\n\n{rodape}",
            "parte": indice,
            "total_partes": len(textos),
            "caracteres": len(f"{texto}\n\n{rodape}"),
        }
        for indice, texto in enumerate(textos, 1)
    ]
    return {"mensagens": mensagens, "resumo": resumo}

