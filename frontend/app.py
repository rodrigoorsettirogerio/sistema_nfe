"""Front-end Streamlit do sistema de inteligência de custos.

Organização das telas (menu lateral):
- 📊 Relatórios (Cliente): evolução de preços/impacto e comparativo entre lojas e mercado.
- 🔍 Auditoria (Admin): aprovação de descrições e agrupamento de produtos (uso interno).
- 🏬 Minhas Lojas: CNPJs vinculados à conta e cadastro de novas lojas.
- 🔔 Configurar Alertas: relatórios de compras por **e-mail** — diário (06:00), resumo
  semanal (segunda 06:00) e fechamento mensal (dia 1º 06:00) — com toda variação de preço
  em relação à última compra (diário) ou à média do período anterior (semanal/mensal),
  impacto mensal em R$, produtos novos/recomprados e índice de
  inflação da cesta. A tela tem a prévia do relatório e o envio manual (SMTP) para o
  endereço salvo na conta.

Acesso: sem sessão em `st.session_state` a página mostra a tela de Login/Cadastro
(usuário e token vêm de /api/auth/login). Conta comum entra só com e-mail e senha; o
administrador é FAIL-CLOSED: o login responde com um desafio de 2FA e a sessão só nasce
depois do código de 6 dígitos do autenticador (o primeiro acesso mostra o QR Code ali
mesmo, antes de abrir o sistema). O mesmo código libera a Auditoria (`X-2FA-Token` nas
chamadas). Os relatórios somam TODOS os CNPJs da conta: o recorte por loja é feito nos
filtros de cada aba (Evolução, Comparativo e Impacto).
"""

import base64
import difflib
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Configuração da página e constantes
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Inteligência de Custos", page_icon="📊", layout="wide")

# Base única da API local. `API_URL` mantém o nome histórico e aponta para a base;
# o endpoint usado pelos relatórios fica em `RESUMO_PRECOS_URL`.
API_BASE = os.getenv("API_URL", "http://127.0.0.1:8000")
API_URL = API_BASE
RESUMO_PRECOS_URL = f"{API_BASE}/api/resumo-precos"
AUDIT_URL = f"{API_BASE}/api/auditar"
AUDIT_PENDENTE_URL = f"{API_BASE}/api/auditar/pendente"
LIST_AUDIT_URL = f"{API_BASE}/api/auditados"
LIST_AUDIT_PENDENTE_URL = f"{API_BASE}/api/auditados/pendentes"
REGRAS_URL = f"{API_BASE}/api/regras-agrupamento"
# Memória de aprendizado (De-Para fornecedor + cProd) e embalagens — Auditoria.
APRENDIZADO_URL = f"{API_BASE}/api/aprendizado-produtos"
APRENDIZADO_REPROCESSAR_URL = f"{APRENDIZADO_URL}/reprocessar"
NOTA_URL = f"{API_BASE}/api/nota"
# Relatórios gerenciais: a agregação acontece no backend e o front recebe só o resultado.
RELATORIO_EVOLUCAO_URL = f"{API_BASE}/api/relatorio/evolucao"
RELATORIO_COMPARATIVO_URL = f"{API_BASE}/api/relatorio/comparativo"
RELATORIO_IMPACTO_URL = f"{API_BASE}/api/relatorio/impacto"



# Autenticação e empresas do usuário logado (Etapa de login + multilojas).
AUTH_REGISTRAR_URL = f"{API_BASE}/api/auth/registrar"
AUTH_LOGIN_URL = f"{API_BASE}/api/auth/login"
AUTH_2FA_SETUP_URL = f"{API_BASE}/api/auth/2fa/setup"
AUTH_2FA_VERIFY_URL = f"{API_BASE}/api/auth/2fa/verify"
# Cadastro com confirmação de e-mail: o código de 6 dígitos chega por SMTP e o token
# (sessão) só é emitido em /api/auth/validar-email.
AUTH_VALIDAR_EMAIL_URL = f"{API_BASE}/api/auth/validar-email"
AUTH_REENVIAR_CODIGO_URL = f"{API_BASE}/api/auth/reenviar-codigo"
EMPRESAS_VINCULADAS_URL = f"{API_BASE}/api/empresas/vinculadas"
EMPRESA_CADASTRAR_URL = f"{API_BASE}/api/empresas/cadastrar"
# Autopreenchimento do cadastro: razão social + endereço fiscal direto da Receita
# (BrasilAPI -> minhareceita -> cnpj.ws) e, quando a loja já existe na conta, do
# endereço gravado no banco.
EMPRESA_CONSULTAR_CNPJ_URL = f"{API_BASE}/api/empresas/consultar-cnpj"
# Endereço pelo CEP (ViaCEP -> BrasilAPI CEP): completa rua/bairro/município/UF quando
# a Receita não devolveu o endereço da loja.
EMPRESA_CONSULTAR_CEP_URL = f"{API_BASE}/api/empresas/consultar-cep"
# Depois de vincular a loja, o mesmo certificado é enviado à ACBr. Essa rota
# agenda o download + importação automáticos ("zero cliques").
EMPRESA_ACBR_URL = f"{API_BASE}/api/acbr/empresas"
# Diagnóstico/força do download da Distribuição NF-e por loja (tela Minhas Lojas).
ACBR_BAIXAR_XMLS_URL = f"{API_BASE}/api/acbr/baixar-xmls"
ACBR_STATUS_URL = f"{API_BASE}/api/acbr/status"
# Fonte das notas por loja (fase 3a do frontend): Omie (API de recebimentos) ou
# certificado digital A1 + Distribuição NF-e da SEFAZ. A troca de fonte e a
# concordância obrigatória do aviso da SEFAZ passam por /fonte-notas.
EMPRESA_FONTE_NOTAS_URL = f"{API_BASE}/api/empresas"
OMIE_CREDENCIAIS_URL = f"{API_BASE}/api/omie/credenciais"
OMIE_SINCRONIZAR_URL = f"{API_BASE}/api/omie/sincronizar"
OMIE_STATUS_URL = f"{API_BASE}/api/omie/status"
# Motor de alertas (WhatsApp): preferências do cliente, simulador do disparo e o token
# global da API (este último só o administrador, na aba ⚙️ Configurações da Auditoria).
ALERTAS_CONFIG_URL = f"{API_BASE}/api/alertas/configurar"
# Relatório de compras por e-mail (diário/semanal/mensal). A prévia usa o MESMO lote
# que o envio real; o envio só marca as notas como cobertas depois do SMTP confirmar.
ALERTAS_RELATORIO_PREVIA_URL = f"{API_BASE}/api/alertas/relatorio/previa"
ALERTAS_RELATORIO_ENVIAR_URL = f"{API_BASE}/api/alertas/relatorio/enviar"
# Memória de cálculo em Excel: a conta aberta (produto a produto, em fórmulas) do
# mesmo mês fechado do e-mail. O cliente confere o número célula por célula.
ALERTAS_MEMORIA_CALCULO_URL = f"{API_BASE}/api/alertas/relatorio/memoria-calculo"
ADMIN_CONFIG_URL = f"{API_BASE}/api/admin/configuracoes"
# Raio-x da base: Excel com TODOS os grupos que têm 2+ descrições (rota de admin + 2FA).
EXPORTAR_GRUPOS_URL = f"{API_BASE}/api/relatorios/exportar-grupos-completos"
# Catálogo MASTER: TODAS as descrições da base, agrupadas por Produto + Marca.
EXPORTAR_CATALOGO_URL = f"{API_BASE}/api/relatorios/exportar-base-completa"
# Consenso com IA (DeepSeek): segunda opinião da Auditoria (admin + 2FA).
IA_RESOLVER_URL = f"{API_BASE}/api/auditoria/ia/resolver-ambiguidade"
IA_VALIDAR_MARCAS_URL = f"{API_BASE}/api/auditoria/ia/validar-marcas"
# Auditoria em massa: a IA grava sugestões pendentes; só o "Aplicar" muda a base.
IA_AUDITAR_BASE_URL = f"{API_BASE}/api/ia/auditar-base-completa"
IA_SUGESTOES_URL = f"{API_BASE}/api/ia/sugestoes"
IA_APLICAR_SUGESTAO_URL = f"{API_BASE}/api/ia/aplicar-sugestao"
IA_IGNORAR_SUGESTAO_URL = f"{API_BASE}/api/ia/ignorar-sugestao"
# Memória de marcas da IA: descrições únicas, processamento em lotes (progresso) e
# aplicação do cache em `itens_nota.marca` sem gastar tokens.
MARCAS_IA_DESCRICOES_URL = f"{API_BASE}/api/marcas/ia/descricoes"
MARCAS_IA_PROCESSAR_URL = f"{API_BASE}/api/marcas/ia/processar"
MARCAS_IA_APLICAR_URL = f"{API_BASE}/api/marcas/ia/aplicar"
# Mesmo teto do backend (`MAXIMO_LOTE_MARCAS`): o lote pequeno desenha a barra sem
# deixar uma única requisição gigante aberta.
LOTE_MARCAS_IA = 50

PAGINA_RELATORIOS = "📊 Relatórios (Cliente)"
PAGINA_AUDITORIA = "🔍 Auditoria (Admin)"
PAGINA_MINHAS_LOJAS = "🏬 Minhas Lojas"
PAGINA_ALERTAS = "🔔 Configurar Alertas"
PAGINAS = [PAGINA_RELATORIOS, PAGINA_AUDITORIA, PAGINA_MINHAS_LOJAS, PAGINA_ALERTAS]

# Sessão do usuário (st.session_state). O token vem de /api/auth/login e é o que
# identifica a conta nas rotas protegidas (Minhas Lojas / cadastro de CNPJ).
SESSAO_USUARIO = "usuario_logado"
SESSAO_TOKEN = "token_api"
# 2FA: token curto devolvido por /api/auth/2fa/verify e enviado em X-2FA-Token.
SESSAO_2FA_TOKEN = "token_2fa"
SESSAO_2FA_EXPIRA = "token_2fa_expira"
# Login em duas etapas: dados da sessão aguardando o código do autenticador.
SESSAO_LOGIN_2FA = "_login_2fa_pendente"
# Cadastro em duas etapas: e-mail aguardando o código de confirmação enviado por SMTP.
# Enquanto esta chave existe, a tela de entrada mostra o campo do código (e o botão
# "Reenviar código"), nunca o formulário de login/cadastro.
SESSAO_VERIFICACAO_EMAIL = "_verificacao_email_pendente"
SESSAO_PAGINA = "pagina_atual"
NAVEGACAO_PENDENTE = "_navegacao_pendente"
MENSAGEM_SAIDA = "_mensagem_saida"
CHAVE_MENU = "menu_principal"
CHAVE_MOVER_PENDENTE = "_auditoria_mover_pendente"
# Página espelhada na URL (`?pagina=minhas-lojas`). A URL é **somente leitura**:
# `?pagina=` é consultado por `pagina_da_url()` quando o `st.session_state` se perde
# (recarga do navegador, reconexão do websocket, tela de login no meio do caminho),
# mas o app NUNCA escreve nela. Escrever em `st.query_params` durante o run faz o
# Streamlit disparar um rerun extra com o estado dos widgets do run anterior, e era
# isso que devolvia o menu para "Relatórios" no primeiro submit do cadastro de loja
# (comprovado no streamlit.log: `run=4` com o rádio de volta em Relatórios).
CHAVE_QUERY_PAGINA = "pagina"
URL_PAGINAS = {
    "relatorios": PAGINA_RELATORIOS,
    "auditoria": PAGINA_AUDITORIA,
    "minhas-lojas": PAGINA_MINHAS_LOJAS,
    "alertas": PAGINA_ALERTAS,
}
# Contador de runs do menu: o log `[menu]` separa "login silencioso" (contador
# volta a 1) de "estado perdido no meio do run" (contador continua crescendo).
CHAVE_RODADAS_MENU = "_menu_rodadas"
# Última página vista pelo widget: se o run seguinte chegar com outra página, o log
# imprime "[menu] AVISO estado do widget divergiu" (troca sem clique no menu).
CHAVE_PAGINA_ANTERIOR = "_menu_pagina_anterior"
# Raio-x exportado: guarda os bytes do Excel entre reruns (o download_button precisa do
# conteúdo já em memória) e o resumo para a tela.
CHAVE_EXPORTACAO_GRUPOS = "_auditoria_exportacao_grupos"
# Catálogo MASTER (Produto + Marca): mesmo padrão de guardar os bytes entre reruns.
CHAVE_EXPORTACAO_CATALOGO = "_auditoria_exportacao_catalogo"
# Resultado do consenso da IA por grupo (prefixo + chave do produto), para o veredito
# continuar visível depois do rerun do botão.
PREFIXO_IA_AMBIGUIDADE = "_auditoria_ia_ambiguidade_"
# Resumo da última rodada da memória de marcas (sobrevive ao st.rerun do botão).
CHAVE_RESUMO_MARCAS_IA = "_memoria_marcas_ia_resumo"

# Alerta (e-mail): o "salvo com sucesso" precisa sobreviver ao `st.rerun()` do
# salvamento (ele limpa a tela), então fica na sessão e é exibido no run seguinte; a
# prévia do relatório também fica guardada para continuar visível depois de salvar.
MENSAGEM_ALERTAS = "_mensagem_alertas"
SESSAO_PREVIA_RELATORIO_ALERTA = "_previa_relatorio_alerta"
# Excel da memória de cálculo já gerado: guarda os bytes entre reruns (o
# `st.download_button` precisa do conteúdo em memória, sem baixar de novo).
SESSAO_MEMORIA_CALCULO = "_memoria_calculo_alerta"
# Tipo escolhido na prévia (Diário/Semanal/Mensal).
CHAVE_TIPO_RELATORIO_ALERTA = "_tipo_relatorio_alerta"
# Confirmação do cadastro de loja: aparece na hora (`st.success`, sem `st.rerun()`)
# e também é guardada na sessão para reaparecer no próximo run.
MENSAGEM_MINHAS_LOJAS = "_mensagem_minhas_lojas"
# Marca que o formulário deve nascer vazio no próximo run (CNPJ, arquivo e senha).
LIMPAR_FORM_MINHAS_LOJAS = "_limpar_form_minhas_lojas"
# Endereço fiscal do cadastro da loja: fallback quando a Receita (BrasilAPI) não
# responde — é o contrato (campo do formulário -> o que já está no banco -> Receita)
# que evita o 400 "Complete o cadastro da empresa para a ACBr". O valor é o campo do
# payload da ACBr; a chave é o widget do cadastro (limpo no próximo run).
# `codigo_municipio` NÃO tem widget: é preenchido pela Receita/CEP na sessão e lido
# no submit (a ACBr exige os 7 dígitos do IBGE, mas o cliente não precisa digitá-los).
CAMPOS_ENDERECO_MINHAS_LOJAS = {
    "cep": "nova_loja_cep",
    "logradouro": "nova_loja_logradouro",
    "numero": "nova_loja_numero",
    "complemento": "nova_loja_complemento",
    "bairro": "nova_loja_bairro",
    "cidade": "nova_loja_municipio",
    "uf": "nova_loja_uf",
    "codigo_municipio": "nova_loja_codigo_municipio",
}
# Resultado de `GET /api/empresas/consultar-cnpj` guardado na sessão: razão social,
# fonte (Receita/indisponível), aviso e endereço devolvido. O campo CNPJ fica FORA do
# `st.form` justamente para consultar a Receita ao completar os 14 dígitos e preencher
# os widgets de endereço antes de eles existirem no run (o Streamlit não deixa alterar
# widget já instanciado).
DADOS_CNPJ_MINHAS_LOJAS = "_dados_cnpj_minhas_lojas"
# Resultado da consulta do CEP (mensagem de sucesso/erro exibida uma vez, após o
# callback preencher o endereço).
DADOS_CEP_MINHAS_LOJAS = "_dados_cep_minhas_lojas"
# Campos que o CEP preencheu automaticamente: numa troca de CEP esses valores podem
# ser substituídos, mas o que o cliente digitou à mão é preservado.
CAMPOS_PREENCHIDOS_PELO_CEP = "_campos_preenchidos_pelo_cep"
# Pede que o expander "📍 Endereço fiscal" nasça aberto no próximo run (ACBr recusou o
# cadastro por endereço incompleto, por exemplo).
ABRIR_ENDERECO_MINHAS_LOJAS = "_abrir_endereco_minhas_lojas"
# Resultado do último cadastro (sucesso), mostrado depois do único `st.rerun()` que
# recarrega a lista de lojas.
RESULTADO_CADASTRO_MINHAS_LOJAS = "_resultado_cadastro_minhas_lojas"
# `st.file_uploader` mantém o arquivo em memória e não pode ser esvaziado apenas
# alterando/removendo a chave no `session_state`. A versão abaixo é incrementada
# após o sucesso para o próximo run criar um widget NOVO (sem arquivo anexado).
CHAVE_UPLOADER_CERTIFICADO = "uploader_key"
PREFIXO_UPLOADER_CERTIFICADO = "uploader_certificado_"
# Prefixo das confirmações de exclusão do certificado (uma por loja). O sufixo é o id
# da empresa + a versão do widget, então a limpeza precisa varrer as chaves.
PREFIXO_CONFIRMACAO_EXCLUSAO_CERT = "confirmar_excluir_cert_"
# Versão das confirmações: remover a chave não basta para o Streamlit recriar um
# checkbox desmarcado. Incrementar a versão força um widget NOVO no próximo run.
CHAVE_CONFIRMACAO_EXCLUSAO_CERT = "confirmacao_exclusao_cert_key"
# Marca que os checkboxes "Confirmar exclusão" devem ser desmarcados no próximo run
# (depois de excluir o certificado ou de concluir o cadastro de uma loja).
LIMPAR_CONFIRMACAO_EXCLUSAO_CERT = "_limpar_confirmacao_exclusao_cert"
# ── Fonte das notas da loja (Omie x certificado A1) ───────────────────────────
# Rótulos do seletor. O dicionário converte o rótulo exibido no radio para o
# valor que o backend grava em `empresas.fonte_notas`.
ROTULO_FONTE_OMIE = "Omie (API de recebimentos)"
ROTULO_FONTE_CERTIFICADO = "Certificado digital A1 (baixar direto da SEFAZ)"
FONTES_NOTAS = {
    ROTULO_FONTE_OMIE: "omie",
    ROTULO_FONTE_CERTIFICADO: "certificado",
}
# MESMO texto de backend/certificados.py (TEXTO_CONCORDANCIA_SEFAZ): a tela mostra
# no checkbox e o backend cobra/grava a concordância. Manter os dois iguais.
TEXTO_CONCORDANCIA_SEFAZ = (
    "Confirmo que as notas fiscais desta empresa NÃO são baixadas em nenhum outro "
    "sistema (contabilidade, sistema de retaguarda, etc.), para evitar conflito na "
    "distribuição da SEFAZ."
)
# Loja escolhida no seletor de fonte; versão dos widgets (radio/checkbox) para
# forçar a recriação com o valor gravado depois de salvar a fonte, e mensagem que
# sobrevive ao `st.rerun`.
SESSAO_LOJA_FONTE_NOTAS = "fonte_notas_loja_selecionada"
PREFIXO_WIDGET_FONTE_NOTAS = "fonte_notas_loja_"
CHAVE_VERSAO_FONTE_NOTAS = "_fonte_notas_versao"
MENSAGEM_FONTE_NOTAS = "_mensagem_fonte_notas"
# App Secret digitado no painel Omie. A chave do widget leva o id da loja e o
# envio bem-sucedido marca `LIMPAR_APP_SECRET_OMIE`: o run seguinte apaga o
# valor da sessão ANTES de recriar o campo (o Streamlit não permite limpar um
# widget já instanciado no mesmo run). O segredo nunca é pré-preenchido.
PREFIXO_APP_SECRET_OMIE = "omie_app_secret_"
LIMPAR_APP_SECRET_OMIE = "_limpar_app_secret_omie"
# Erro do último envio de credenciais, exibido no run seguinte (o submit sempre
# dispara `st.rerun` para o campo do segredo voltar vazio).
ERRO_CREDENCIAIS_OMIE = "_erro_credenciais_omie"
# Fonte escolhida no formulário "Vincular novo CNPJ" (fora do `st.form`, porque o
# radio precisa de run para mostrar os campos certos) e o aceite do aviso da SEFAZ.
NOVA_LOJA_FONTE_NOTAS = "nova_loja_fonte_notas"
NOVA_LOJA_CONCORDANCIA_SEFAZ = "nova_loja_concordancia_sefaz"
NOVA_LOJA_OMIE_APP_KEY = "nova_loja_omie_app_key"
NOVA_LOJA_OMIE_APP_SECRET = "nova_loja_omie_app_secret"
# Clique em "Enviar e-mail real": guarda a confirmação entre o submit do formulário e
# os botões Confirmar/Cancelar (o Streamlit reexecuta a página a cada clique).
SESSAO_CONFIRMAR_EMAIL_ALERTA = "_confirmar_email_alerta"
ROTULO_EMPRESA_TODAS = "Todas as minhas lojas"
# Mesmo mínimo do backend (backend/auth.py); validado no formulário antes do POST.
SENHA_MINIMA_UI = 6

# Legenda curta de cada item do menu lateral.
LEGENDAS_PAGINAS = {
    PAGINA_RELATORIOS: "📊 Evolução, impacto e comparativo entre lojas/mercado.",
    PAGINA_AUDITORIA: "🔒 Área interna: aprovação de descrições e agrupamento de produtos.",
    PAGINA_MINHAS_LOJAS: "🏬 CNPJs da conta e cadastro de novas lojas.",
    PAGINA_ALERTAS: "🔔 Avisos de aumentos e divergências de preço por e-mail.",
}

CAPITULOS_NCM_NOME = {
    "01": "Animais Vivos",
    "02": "Carnes e Miudezas",
    "03": "Peixes e Moluscos",
    "04": "Laticínios, Ovos e Mel",
    "07": "Hortaliças e Raízes",
    "08": "Frutas",
    "09": "Café, Chá e Especiarias",
    "10": "Cereais",
    "11": "Farinhas",
    "15": "Óleos e Gorduras",
    "16": "Preparações de Carnes",
    "17": "Açúcares",
    "18": "Chocolates",
    "19": "Pães e Massas",
    "20": "Conservas",
    "21": "Molhos e Temperos",
    "22": "Bebidas",
    "33": "Cosméticos",
    "34": "Produtos de Limpeza",
    "39": "Plásticos",
}
TERMOS_EXCLUSIVOS = ["CREMOSA", "TOLETE", "PURE", "PALITO", "INTEIRA"]
EANS_SEM_CODIGO = ["SEM_EAN", "SEM GTIN", "", "0", "None"]
EANS_INVALIDOS = {str(valor).strip().upper() for valor in EANS_SEM_CODIGO} | {"NAN", "NULL"}
# Regra de ouro da auditoria (ver grupo_e_excecao):
# - COM EAN válido o código de barras dá confiança, então o texto pode variar:
#   o grupo só volta para a auditoria se a similaridade com a descrição padrão
#   (a mais antiga) ficar ABAIXO deste limiar — ex.: "LEITE" virou "MANTEIGA".
LIMIAR_SIMILARIDADE_EAN = 0.70
# - SEM EAN não existe âncora, então o rigor é no texto: só exibe o grupo quando
#   houver 2+ descrições com similaridade ENTRE SI abaixo deste limiar — ex.:
#   "MANDIOCA CREMOSA" x "MANDIOCA TOLETE".
LIMIAR_SIMILARIDADE_SEM_EAN = 0.85
# Métrica híbrida: além do ratio de caracteres (difflib), compara as PALAVRAS
# principais (mais de 3 caracteres) ignorando a ordem. A partir deste limiar de
# palavras em comum ("LIMPOL NEUTRO" x "DETERGENTE NEUTRO LIMPOL" = 100%) a
# descrição é tratada como o MESMO produto e o grupo sai da auditoria.
LIMIAR_TOKENS_MESMO_PRODUTO = 0.80
TAMANHO_MINIMO_TOKEN = 4
# Conectores que invertem o produto e por isso bloqueiam o veredito "mesmo produto".
QUALIFICADORES_PRODUTO = frozenset({"COM", "SEM", "CON", "SIN"})
# Quando a trava reprova (COM x SEM na mesma comparação) a similaridade fica limitada a
# este valor, para o grupo aparecer na auditoria mesmo com o texto muito parecido.
LIMIAR_TRAVA_CONFLITANTE = 0.50
# Zona cinzenta (consenso com IA): grupos com 2+ descrições e similaridade entre 50% e
# 90% não são obviamente iguais nem obviamente diferentes. Nesse intervalo a tela oferece
# o botão "🤖 Consultar IA", que pede uma segunda opinião ao DeepSeek. De 90% para cima a
# regra automática já considera o mesmo produto (sai da auditoria); abaixo de 50% é
# divergência clara e o auditor decide sem gastar cota de IA.
LIMIAR_ZONA_CINZENTA_MIN = 0.50
LIMIAR_ZONA_CINZENTA_MAX = 0.90

# Trava de medidas: se as duas descrições citam peso/volume/comprimento e os valores não
# batem depois da conversão para a unidade base (g / ml / mm), são embalagens diferentes
# ("ARROZ 5KG" x "ARROZ 1KG", "1 L" x "900 ML") e o grupo TEM de aparecer na auditoria
# mesmo com o nome igual. Contagens de embalagem (UN, CX, PCT, FD) ficam de fora para não
# gerar ruído, e números sem unidade ("TP1", "C 74,2400") também são ignorados.
UNIDADES_MEDIDA = {
    "MG": ("MASSA", 0.001),
    "G": ("MASSA", 1.0),
    "GR": ("MASSA", 1.0),
    "GRS": ("MASSA", 1.0),
    "KG": ("MASSA", 1000.0),
    "KGS": ("MASSA", 1000.0),
    "ML": ("VOLUME", 1.0),
    "CL": ("VOLUME", 10.0),
    "L": ("VOLUME", 1000.0),
    "LT": ("VOLUME", 1000.0),
    "LTS": ("VOLUME", 1000.0),
    "MM": ("COMPRIMENTO", 1.0),
    "CM": ("COMPRIMENTO", 10.0),
    "M": ("COMPRIMENTO", 1000.0),
    "MT": ("COMPRIMENTO", 1000.0),
    "MTS": ("COMPRIMENTO", 1000.0),
}
# Folga na comparação das medidas (em g/ml/mm) para absorver arredondamento de conversão.
TOLERANCIA_MEDIDA = 0.5
# "LT" é ambíguo no varejo (litro x lata). Depois de uma palavra de embalagem ("PCT 12 LT",
# "CX 24 LT") ele conta LATAS, não litros: esses casos são contagem e ficam fora da trava.
UNIDADES_AMBIGUAS = frozenset({"LT"})
PALAVRAS_EMBALAGEM = frozenset({"PCT", "CX", "FD", "EMB", "DZ"})
# Alerta de duplicidade na auditoria: dois GRUPOS diferentes que repetem a mesma descrição
# (ou similaridade >= LIMIAR_DUPLICIDADE) são o mesmo produto. O card mostra um aviso no
# topo e um botão para juntar os dois grupos em 1 clique.
LIMIAR_DUPLICIDADE = 0.95
MAXIMO_SUGESTOES_DUPLICIDADE = 3
# Histórico de compras: quantas linhas (com o botão "Ver Nota") são desenhadas por vez.
LIMITE_HISTORICO_COMPRAS = 100
# Mesma regra do backend (`LIMIAR_ABERRACAO_PCT`): variação de 50% ou mais entre a
# penúltima e a última compra é divergência e fica destacada em vermelho na Auditoria.
LIMIAR_VARIACAO_BRUSCA = 50.0
MODOS_EXIBICAO = ["Mostrar Apenas Pendentes", "Mostrar Todos"]
# "Mover para outro grupo" também permite criar um grupo NOVO só com a descrição escolhida:
# é o remédio para quando o agrupamento automático juntou produtos diferentes e o grupo
# correto ainda não existe. A opção aparece como a PRIMEIRA da lista de destinos.
OPCAO_CRIAR_GRUPO = "➕ Criar novo grupo com esta descrição"
PREFIXO_GRUPO_NOVO = "NOVO_"
# Sugestão da IA com o seletor de destino aberto (mesmo fluxo do "Mover" da Auditoria).
CHAVE_IA_SUGESTAO_DESTINO = "_ia_sugestao_destino_aberta"
CHAVE_IA_SUGESTAO_FLASH = "_ia_sugestao_flash"

# Janelas de análise (em dias) e periodicidades aceitas por /api/relatorio/*.
DIAS_RELATORIO = [7, 15, 30, 90, 180, 360]
PERIODICIDADES_RELATORIO = {
    "Diária": "diaria",
    "Semanal": "semanal",
    "Quinzenal": "quinzenal",
    "Mensal": "mensal",
    "Bimestral": "bimestral",
    "Trimestral": "trimestral",
}
# Pivot tables (formato Excel) das abas Evolução e Comparativo: o backend devolve todas as
# linhas produto x período de uma vez (o limite dele é 20.000) e a tabela HTML é desenhada
# no front. Para não travar o navegador com uma tabela gigante, exibimos os últimos N
# períodos e quantos produtos couberem no orçamento de células (LIMITE_CELULAS_PIVOT) —
# sempre avisando na tela quando houver corte.
LIMITE_LINHAS_PIVOT = 20000
LIMITE_PRODUTOS_PIVOT = 400
LIMITE_PERIODOS_PIVOT = 40
LIMITE_CELULAS_PIVOT = 6000

# Filtro de loja dos relatórios: "Minhas Lojas (Todas)" agrupa as compras de todos os
# CNPJs do grupo; escolher um nome filtra aquela loja. O mesmo texto é entendido pelo
# backend (que trata todos estes apelidos como "todas as lojas").
ROTULO_TODAS_LOJAS = "Minhas Lojas (Todas)"
LOJAS_TODAS_ALIASES = {
    "todas",
    "todas as lojas",
    "todas as empresas",
    "minhas lojas",
    "minhas lojas (todas)",
}
# Motivo da pendência gravada na Auditoria (o backend usa os mesmos valores).
MOTIVO_PENDENCIA_MANUAL = "manual"
MOTIVO_PENDENCIA_ABERRACAO = "aberrante"

# Botão de desfazer aprovação (vermelho) — o Streamlit não tem "danger" para buttons.
st.markdown(
    """
    <style>
    div[class*="st-key-btn_desfazer"] button {
        background-color: #d32f2f;
        border-color: #d32f2f;
        color: #ffffff;
    }
    div[class*="st-key-btn_desfazer"] button:hover {
        background-color: #b71c1c;
        border-color: #b71c1c;
        color: #ffffff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ──────────────────────────────────────────────────────────────────────────────
# Acesso à API
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=10)
def carregar_dados():
    try:
        response = requests.get(RESUMO_PRECOS_URL)
        if response.status_code == 200:
            data = response.json()
            return pd.DataFrame(data) if data else pd.DataFrame()
    except Exception:
        pass
    return pd.DataFrame()


# ── Sessão do usuário e rotas de autenticação ──────────────────────────────────
def cabecalhos_api(token=None):
    """Cabeçalhos das rotas protegidas: `Authorization` + `X-2FA-Token` (quando houver).

    O token de 2FA vale por 2 horas e só é conferido nas rotas de auditoria; incluí-lo
    sempre nas chamadas mantém o resto do front sem exceções.
    """
    token = token if token is not None else st.session_state.get(SESSAO_TOKEN)
    cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
    token_2fa = st.session_state.get(SESSAO_2FA_TOKEN)
    if token_2fa:
        cabecalhos["X-2FA-Token"] = token_2fa
    return cabecalhos


def _tipo_excecao_api(erro):
    """Nome exato da falha de rede (ConnectionError, Timeout, ...)."""
    if isinstance(erro, requests.Timeout):
        return "Timeout"
    if isinstance(erro, requests.ConnectionError):
        return "ConnectionError"
    if isinstance(erro, requests.RequestException):
        return "RequestException"
    return type(erro).__name__


def _resumo_corpo_api(resposta, limite=500):
    """Corpo da resposta em uma linha, curto o bastante para o diagnóstico."""
    try:
        corpo = resposta.json()
    except Exception:
        texto = resposta.text or ""
    else:
        try:
            texto = json.dumps(corpo, ensure_ascii=False, default=str)
        except Exception:
            texto = str(corpo)
    return " ".join(str(texto).split())[:limite]


def _log_chamada_api(
    metodo,
    url,
    *,
    status_code=None,
    elapsed_ms=None,
    erro=None,
    corpo="",
):
    """Escreve no console do Streamlit a URL e o resultado de cada chamada.

    O token nunca entra no log: só a URL, o status, o tempo e um trecho da resposta.
    """
    prefixo = f"[Streamlit→API] {metodo} {url}"
    if erro is not None:
        print(
            f"{prefixo} FALHOU | {_tipo_excecao_api(erro)}: {erro!r}",
            flush=True,
        )
        return
    partes = [f"{prefixo} -> HTTP {status_code}"]
    if elapsed_ms is not None:
        partes.append(f"{elapsed_ms} ms")
    if corpo:
        partes.append(f"corpo={corpo}")
    print(" | ".join(partes), flush=True)


def _registrar_etapa(
    diagnostico,
    metodo,
    url,
    *,
    status_code=None,
    elapsed_ms=None,
    corpo="",
    erro=None,
):
    """Guarda uma etapa para o expander de diagnóstico da tela."""
    etapa = {
        "metodo": metodo,
        "url": url,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "corpo": corpo,
    }
    if erro is not None:
        etapa["tipo_erro"] = _tipo_excecao_api(erro)
        etapa["erro"] = repr(erro)
    if diagnostico is not None:
        diagnostico.setdefault("etapas", []).append(etapa)
    return etapa


def usuario_logado():
    """Dados do usuário logado (dict) ou None quando não há sessão ativa."""
    return st.session_state.get(SESSAO_USUARIO)


def usuario_e_admin():
    """True quando a conta logada é administradora (flag `is_admin` devolvida pela API).

    Serve só para ESCONDER o que a conta não pode usar: a autorização de verdade está no
    backend (`auth.get_admin_user`), que responde 403 nas rotas de auditoria.
    """
    return bool((usuario_logado() or {}).get("is_admin"))


def paginas_disponiveis():
    """Itens do menu lateral liberados para o perfil da conta logada."""
    if usuario_e_admin():
        return list(PAGINAS)
    return [PAGINA_RELATORIOS, PAGINA_MINHAS_LOJAS, PAGINA_ALERTAS]


def mensagem_de_erro(resposta):
    """Mensagem amigável das respostas de erro do FastAPI (campo `detail`)."""
    try:
        detalhe = resposta.json().get("detail")
    except Exception:
        detalhe = None
    if isinstance(detalhe, list):  # erro de validação do Pydantic
        detalhe = " ".join(
            str(item.get("msg", "")) for item in detalhe if isinstance(item, dict)
        )
    if isinstance(detalhe, dict):
        # 2FA: o login devolve {"motivo", "mensagem", "desafio"...} em vez de texto puro.
        detalhe = detalhe.get("mensagem") or detalhe.get("detail")
    return str(detalhe or f"Erro {resposta.status_code} na comunicação com o servidor.")


def mensagem_erro_http(resposta):
    """`Erro: <status> - <detail>` — o formato usado na tela de cadastro de loja.

    Mostra o `detail` do FastAPI exatamente como veio (inclusive erro de validação
    do Pydantic, que chega como lista). Sem corpo JSON, sem a chave `detail` ou com
    `detail` vazio, devolve "Erro desconhecido" — a tela nunca fica sem mensagem.
    """
    try:
        corpo = resposta.json()
    except Exception:
        corpo = None
    detalhe = corpo.get("detail") if isinstance(corpo, dict) else None
    if isinstance(detalhe, list):
        detalhe = " ".join(
            str(item.get("msg", "")) for item in detalhe if isinstance(item, dict)
        )
    if isinstance(detalhe, dict):
        detalhe = detalhe.get("mensagem") or detalhe.get("detail")
    texto = str(detalhe).strip() if detalhe is not None else ""
    return f"Erro: {resposta.status_code} - {texto or 'Erro desconhecido'}"


def detalhe_de_erro(resposta):
    """`detail` ESTRUTURADO (dict) da resposta de erro — {} quando é só texto.

    O login do administrador usa isto para saber que a senha estava certa e que falta o
    segundo fator (`desafio`, `codigo_2fa_necessario`, `configuracao_2fa_necessaria`).
    """
    try:
        detalhe = resposta.json().get("detail")
    except Exception:
        return {}
    return detalhe if isinstance(detalhe, dict) else {}


def post_auth(url, corpo):
    """POST nas rotas de autenticação. Retorna (dados, erro, detalhe) — nunca levanta."""
    try:
        resposta = requests.post(url, json=corpo, timeout=60)
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}", {}
    if resposta.status_code == 200:
        return resposta.json(), None, {}
    return None, mensagem_de_erro(resposta), detalhe_de_erro(resposta)


# ── Autenticação em dois fatores (2FA/TOTP) ────────────────────────────────────
def _limpar_sessao_2fa():
    """Esquece o token 2FA da sessão (logout, expiração ou troca de conta)."""
    st.session_state.pop(SESSAO_2FA_TOKEN, None)
    st.session_state.pop(SESSAO_2FA_EXPIRA, None)


def _2fa_desbloqueado():
    """True quando a sessão guarda um token 2FA ainda válido (emitido pelo backend)."""
    if not st.session_state.get(SESSAO_2FA_TOKEN):
        return False
    expira = st.session_state.get(SESSAO_2FA_EXPIRA)
    if expira:
        try:
            limite = datetime.fromisoformat(str(expira).replace("Z", "+00:00"))
        except ValueError:
            limite = None
        if limite is not None and datetime.now(timezone.utc) >= limite:
            _limpar_sessao_2fa()
            return False
    return True


def _guardar_sessao_2fa(resposta):
    """Guarda o token 2FA devolvido por /api/auth/2fa/verify e atualiza o usuário."""
    st.session_state[SESSAO_2FA_TOKEN] = resposta.get("token_2fa") or ""
    st.session_state[SESSAO_2FA_EXPIRA] = resposta.get("expira_em") or ""
    usuario = usuario_logado()
    if usuario is not None and resposta.get("usuario"):
        st.session_state[SESSAO_USUARIO] = {**usuario, **resposta["usuario"]}
    st.cache_data.clear()


def _post_2fa(url, corpo, token=None):
    """POST nas rotas de 2FA. Retorna (dados, erro) — nunca levanta exceção."""
    try:
        resposta = requests.post(url, json=corpo, headers=cabecalhos_api(token), timeout=60)
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        return resposta.json(), None
    return None, mensagem_de_erro(resposta)


def carregar_setup_2fa(reiniciar=False, token=None):
    """Segredo + URL/QR Code do autenticador (POST /api/auth/2fa/setup).

    `token` é o desafio do login (primeiro acesso do admin, antes de existir sessão) ou
    None para usar o token da sessão.
    """
    return _post_2fa(AUTH_2FA_SETUP_URL, {"reiniciar": bool(reiniciar)}, token=token)


def entrar_no_sistema(dados_sessao):
    """Guarda usuário/token na sessão e reinicia a navegação já logado."""
    st.session_state[SESSAO_USUARIO] = dados_sessao.get("usuario") or {}
    st.session_state[SESSAO_TOKEN] = dados_sessao.get("token") or ""
    # A URL manda: um login disparado no meio do cadastro (sessão descartada pelo
    # navegador) devolve o usuário para a tela onde ele estava, não para Relatórios.
    st.session_state[SESSAO_PAGINA] = pagina_da_url() or PAGINA_RELATORIOS
    st.session_state.pop(SESSAO_LOGIN_2FA, None)
    _limpar_sessao_2fa()
    for chave in (CHAVE_MENU, NAVEGACAO_PENDENTE):
        st.session_state.pop(chave, None)
    print(
        "[sessao] login "
        f"usuario_id={(st.session_state[SESSAO_USUARIO] or {}).get('id')} "
        f"email={(st.session_state[SESSAO_USUARIO] or {}).get('email')} "
        f"pagina_inicial={st.session_state[SESSAO_PAGINA]!r}",
        flush=True,
    )
    st.cache_data.clear()


def sair_do_sistema(mensagem=None):
    """Encerra a sessão (logout ou sessão expirada) e volta para a tela de login."""
    print(
        "[sessao] saida "
        f"usuario_id={(usuario_logado() or {}).get('id')} "
        f"motivo={mensagem or 'logout'}",
        flush=True,
    )
    for chave in (
        SESSAO_USUARIO,
        SESSAO_TOKEN,
        SESSAO_2FA_TOKEN,
        SESSAO_2FA_EXPIRA,
        SESSAO_LOGIN_2FA,
        SESSAO_VERIFICACAO_EMAIL,
        SESSAO_PAGINA,
        CHAVE_MENU,
        NAVEGACAO_PENDENTE,
    ):
        st.session_state.pop(chave, None)
    if mensagem:
        st.session_state[MENSAGEM_SAIDA] = mensagem
    st.cache_data.clear()


@st.cache_data(ttl=30, show_spinner=False)
def carregar_empresas_vinculadas(token):
    """Empresas (CNPJs) da conta logada. Retorna (empresas, erro, sessão_inválida)."""
    try:
        resposta = requests.get(
            EMPRESAS_VINCULADAS_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except Exception as erro:
        return [], f"Não foi possível carregar as suas lojas: {erro}", False
    if resposta.status_code == 200:
        try:
            dados = resposta.json() or []
        except ValueError:
            return (
                [],
                "A API respondeu às lojas vinculadas em formato que não é JSON válido.",
                False,
            )
        if not isinstance(dados, list):
            return (
                [],
                "A API respondeu às lojas vinculadas em formato inesperado "
                f"(esperava uma lista): {str(dados)[:300]}",
                False,
            )
        return dados, None, False
    if resposta.status_code == 401:
        return [], mensagem_de_erro(resposta), True
    return [], mensagem_de_erro(resposta), False


def _renderizar_diagnostico_cadastro(diagnostico):
    """Expander com URL, status HTTP e erro exato de cada POST do cadastro."""
    if not diagnostico:
        return
    etapas = diagnostico.get("etapas") or []
    with st.expander("🛠 Diagnóstico do envio do cadastro", expanded=False):
        st.write(f"**API base:** `{diagnostico.get('api_base')}`")
        st.write(
            f"**CNPJ:** `{diagnostico.get('cnpj')}` | "
            f"**Certificado:** `{diagnostico.get('certificado') or '(sem arquivo)'}` "
            f"({diagnostico.get('certificado_bytes') or 0} bytes) | "
            f"**Senha informada:** {diagnostico.get('senha_informada')} | "
            f"**Token enviado:** {diagnostico.get('token_enviado')}"
        )
        if diagnostico.get("endereco_informado"):
            st.caption(
                "Endereço enviado à ACBr: "
                + ", ".join(
                    f"{campo}={valor}"
                    for campo, valor in diagnostico["endereco_informado"].items()
                )
            )
        if not etapas:
            st.warning("Nenhum POST foi executado: a validação da tela parou antes.")
            return
        for etapa in etapas:
            status = etapa.get("status_code")
            tempo = etapa.get("elapsed_ms")
            if status is None:
                st.error(
                    f"❌ **{etapa.get('metodo')}** `{etapa.get('url')}` — "
                    f"**{etapa.get('tipo_erro')}**: {etapa.get('erro')}"
                )
                continue
            st.write(
                f"**{etapa.get('metodo')}** `{etapa.get('url')}` → "
                f"**HTTP {status}** ({tempo} ms)"
            )
            if etapa.get("corpo"):
                st.code(etapa["corpo"], language="json")
        st.caption(
            "A mesma linha [Streamlit→API] foi impressa no terminal onde o "
            "Streamlit está rodando."
        )


def cadastrar_loja(
    cnpj,
    arquivo=None,
    senha_certificado="",
    diagnostico=None,
    enviar_acbr=True,
    fonte_notas="",
    concordancia_sefaz=False,
):
    """Vincula um CNPJ à conta e envia o certificado à ACBr.

    Retorna ``(empresa, mensagem, erro)``. A primeira chamada
    (``/api/empresas/cadastrar``) cria/adota a loja e valida o certificado; a
    segunda (``/api/acbr/empresas``) envia o mesmo `.pfx/.p12` à ACBr e agenda o
    download/importação em background. O arquivo e a senha não são persistidos.

    ``enviar_acbr=False`` executa só o vínculo. A tela "Minhas Lojas" usa isso
    para fazer o ``POST /api/acbr/empresas`` por conta própria, dentro do seu
    próprio ``try/except``, mostrando na tela o status e o JSON crus da resposta.

    ``fonte_notas="omie"`` vincula a loja SEM certificado (as notas vêm da API
    do Omie); ``concordancia_sefaz=True`` registra o aceite do aviso da SEFAZ
    junto com o certificado — o backend recusa o envio com ``false``.

    ``diagnostico`` é um dict opcional preenchido com URL, status HTTP, tempo e
    trecho da resposta de cada POST, além dos ``requests.Response`` crus em
    ``diagnostico["respostas"]`` (chaves ``vinculo`` e ``acbr``). A tela usa
    esses dados para mostrar onde a chamada parou; o mesmo resumo também vai
    para o console do Streamlit.
    """
    if diagnostico is None:
        diagnostico = {}
    nome_certificado = ""
    conteudo_certificado = None
    if arquivo is not None:
        nome_certificado = getattr(arquivo, "name", "certificado.pfx")
        conteudo_certificado = (
            arquivo.getvalue() if hasattr(arquivo, "getvalue") else arquivo
        )
    diagnostico.update(
        {
            "api_base": API_BASE,
            "cnpj": cnpj,
            "url_vinculo": EMPRESA_CADASTRAR_URL,
            "url_acbr": EMPRESA_ACBR_URL,
            "certificado": nome_certificado,
            "certificado_bytes": len(conteudo_certificado or b""),
            "senha_informada": bool((senha_certificado or "").strip()),
            "fonte_notas": fonte_notas or "certificado",
            "concordancia_sefaz": bool(concordancia_sefaz),
            "token_enviado": bool(st.session_state.get(SESSAO_TOKEN)),
            "etapas": [],
        }
    )
    dados = {
        "cnpj": cnpj,
        "senha_certificado": senha_certificado or "",
        "fonte_notas": fonte_notas or "",
    }
    if concordancia_sefaz:
        # A tela SEMPRE envia quando o checkbox está marcado; omitir = chamada
        # legada (o backend mantém compatibilidade com scripts/testes).
        dados["concordancia_sefaz"] = "true"
    arquivos = None
    if arquivo is not None:
        arquivos = {
            "certificado": (
                nome_certificado,
                conteudo_certificado,
                "application/x-pkcs12",
            )
        }

    inicio = time.perf_counter()
    try:
        resposta = requests.post(
            EMPRESA_CADASTRAR_URL,
            data=dados,
            files=arquivos,
            headers=cabecalhos_api(),
            timeout=90,
        )
    except Exception as erro:
        elapsed_ms = int((time.perf_counter() - inicio) * 1000)
        _registrar_etapa(
            diagnostico,
            "POST",
            EMPRESA_CADASTRAR_URL,
            elapsed_ms=elapsed_ms,
            erro=erro,
        )
        _log_chamada_api(
            "POST", EMPRESA_CADASTRAR_URL, elapsed_ms=elapsed_ms, erro=erro
        )
        tipo = _tipo_excecao_api(erro)
        return (
            None,
            None,
            f"Não foi possível conectar ao servidor da API ({tipo}) em "
            f"{EMPRESA_CADASTRAR_URL}: {erro}",
        )

    elapsed_ms = int((time.perf_counter() - inicio) * 1000)
    corpo_resposta = _resumo_corpo_api(resposta)
    _registrar_etapa(
        diagnostico,
        "POST",
        EMPRESA_CADASTRAR_URL,
        status_code=resposta.status_code,
        elapsed_ms=elapsed_ms,
        corpo=corpo_resposta,
    )
    _log_chamada_api(
        "POST",
        EMPRESA_CADASTRAR_URL,
        status_code=resposta.status_code,
        elapsed_ms=elapsed_ms,
        corpo=corpo_resposta,
    )
    # A resposta crua fica disponível mesmo quando o status não é 200: a tela
    # exibe `Status:` + corpo de CADA POST, inclusive nos erros.
    diagnostico.setdefault("respostas", {})["vinculo"] = resposta
    if resposta.status_code != 200:
        # A tela mostra exatamente o `detail` devolvido pelo FastAPI; a URL, o
        # status e o corpo completo continuam no expander de diagnóstico.
        return None, None, mensagem_de_erro(resposta)

    try:
        corpo = resposta.json() or {}
    except ValueError:
        return (
            None,
            None,
            f"POST {EMPRESA_CADASTRAR_URL} respondeu HTTP 200, mas o corpo não é "
            "JSON válido. O formulário foi mantido para você tentar novamente.",
        )
    if corpo.get("sucesso") is False:
        return (
            None,
            None,
            "A API respondeu HTTP 200, mas informou que o cadastro não foi "
            f"concluído: {corpo.get('mensagem') or corpo.get('detail') or corpo}",
        )
    empresa = corpo.get("empresa")
    if (
        not isinstance(empresa, dict)
        or not empresa.get("id")
        or not empresa.get("cnpj")
    ):
        return (
            None,
            None,
            "A API respondeu HTTP 200, mas não devolveu a loja criada "
            f"(`empresa` ausente/inválida em {EMPRESA_CADASTRAR_URL}). "
            "O formulário foi mantido para você tentar novamente.",
        )

    if not enviar_acbr:
        # Quem chamou já vai fazer o POST /api/acbr/empresas separadamente,
        # com status + JSON na tela (é o que o botão "Salvar e vincular loja" faz).
        return empresa, corpo.get("mensagem"), None

    # A loja já existe; agora o certificado segue para a ACBr. É essa segunda
    # chamada que dispara o download e a importação automáticos.
    inicio = time.perf_counter()
    try:
        resposta_acbr = requests.post(
            EMPRESA_ACBR_URL,
            data={
                "cnpj": cnpj,
                "senha_certificado": senha_certificado or "",
                **({"concordancia_sefaz": "true"} if concordancia_sefaz else {}),
            },
            files=arquivos,
            headers=cabecalhos_api(),
            timeout=90,
        )
    except Exception as erro:
        elapsed_ms = int((time.perf_counter() - inicio) * 1000)
        _registrar_etapa(
            diagnostico,
            "POST",
            EMPRESA_ACBR_URL,
            elapsed_ms=elapsed_ms,
            erro=erro,
        )
        _log_chamada_api("POST", EMPRESA_ACBR_URL, elapsed_ms=elapsed_ms, erro=erro)
        tipo = _tipo_excecao_api(erro)
        return (
            empresa,
            None,
            "Loja vinculada, mas não foi possível enviar o certificado à ACBr "
            f"({tipo}) em {EMPRESA_ACBR_URL}: {erro}",
        )

    elapsed_ms = int((time.perf_counter() - inicio) * 1000)
    corpo_resposta = _resumo_corpo_api(resposta_acbr)
    _registrar_etapa(
        diagnostico,
        "POST",
        EMPRESA_ACBR_URL,
        status_code=resposta_acbr.status_code,
        elapsed_ms=elapsed_ms,
        corpo=corpo_resposta,
    )
    _log_chamada_api(
        "POST",
        EMPRESA_ACBR_URL,
        status_code=resposta_acbr.status_code,
        elapsed_ms=elapsed_ms,
        corpo=corpo_resposta,
    )
    diagnostico.setdefault("respostas", {})["acbr"] = resposta_acbr
    if resposta_acbr.status_code != 200:
        # `detail` do JSON 500 ("Erro ao cadastrar: ...") vai direto para a tela;
        # o diagnóstico mantém URL/status/corpo para o suporte.
        return empresa, None, mensagem_de_erro(resposta_acbr)
    try:
        corpo_acbr = resposta_acbr.json() or {}
    except ValueError:
        return (
            empresa,
            None,
            f"POST {EMPRESA_ACBR_URL} respondeu HTTP 200, mas o corpo não é "
            "JSON válido. O cadastro na ACBr não pode ser confirmado.",
        )
    if corpo_acbr.get("sucesso") is False:
        return (
            empresa,
            None,
            "A ACBr não confirmou o cadastro: "
            f"{corpo_acbr.get('mensagem') or corpo_acbr.get('aviso') or corpo_acbr}",
        )
    empresa_confirmada = corpo_acbr.get("empresa")
    if (
        not isinstance(empresa_confirmada, dict)
        or not empresa_confirmada.get("id")
        or not empresa_confirmada.get("cnpj")
    ):
        return (
            empresa,
            None,
            f"POST {EMPRESA_ACBR_URL} respondeu HTTP 200, mas não devolveu a "
            "empresa salva no banco. O formulário foi mantido para reenvio.",
        )
    return (
        empresa_confirmada,
        corpo_acbr.get("mensagem")
        or (
            "Certificado enviado! Iniciando download e importação em background. "
            "Acompanhe em Minhas Lojas > Consultar Status ACBr."
        ),
        None,
    )


def consultar_status_acbr(cnpj):
    """Compara o NSU salvo na loja com o teto atual da fila da ACBr.

    Retorna ``(dados, erro)``. A rota responde 200 mesmo quando a consulta à
    ACBr falha (``sucesso: false``); nesse caso o motivo real vem em ``erro``
    para a tela exibir.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    try:
        resposta = requests.get(
            f"{ACBR_STATUS_URL}/{quote(cnpj_limpo, safe='')}",
            headers=cabecalhos_api(),
            timeout=120,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        try:
            return resposta.json() or {}, None
        except ValueError:
            return None, "A API respondeu ao status da ACBr em formato inválido."
    return None, (
        f"Erro ao consultar o status da ACBr ({resposta.status_code}): "
        f"{mensagem_de_erro(resposta)}"
    )


def forcar_download_acbr(cnpj):
    """Pede o download manual dos XMLs de uma loja.

    A rota usa o NSU salvo na empresa; o período enviado só recorta os
    documentos. O primeiro download continua limitado pela janela configurada
    no backend (``DIAS_PRIMEIRO_DOWNLOAD``). Retorna ``(dados, erro)``.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    corpo = {
        "cnpj": cnpj_limpo,
        # Período aberto: em downloads seguintes o avanço é pelo NSU e um XML
        # novo pode ter emissão mais antiga. No primeiro download o backend
        # aplica a janela de 7 dias (ou a configurada no .env).
        "data_inicial": date(2000, 1, 1).isoformat(),
        "data_final": (date.today() + timedelta(days=1)).isoformat(),
        "limite_documentos": 500,
        "salvar_arquivos": True,
        "ignorar_tempo_espera": False,
    }
    try:
        resposta = requests.post(
            ACBR_BAIXAR_XMLS_URL,
            json=corpo,
            headers=cabecalhos_api(),
            timeout=600,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        try:
            return resposta.json() or {}, None
        except ValueError:
            return None, "A API respondeu ao download em formato inválido."
    return None, (
        f"Erro ao forçar o download ({resposta.status_code}): "
        f"{mensagem_de_erro(resposta)}"
    )


def excluir_certificado_loja(empresa_id):
    """Exclui o certificado de uma loja da conta. Retorna (mensagem, erro).

    O backend apaga só os dados públicos do certificado (titular/validade/data): a loja,
    o dono e o histórico de notas ficam intactos.
    """
    try:
        resposta = requests.delete(
            f"{API_BASE}/api/empresas/{int(empresa_id)}/certificado",
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        corpo = resposta.json() or {}
        return corpo.get("mensagem") or "Certificado excluído.", None
    return None, mensagem_de_erro(resposta)

def _formatar_data_hora_brasilia(valor):
    """ISO 8601 (UTC) no horário de Brasília — usado nos carimbos da tela.

    O backend grava `omie_sync_em` e a concordância em UTC. Mostrar o horário
    local evita a impressão de que a sincronização aconteceu "no futuro".
    """
    if not valor:
        return "—"
    try:
        momento = pd.Timestamp(valor)
        if momento.tzinfo is not None:
            momento = momento.tz_convert("America/Sao_Paulo")
        return momento.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(valor)


def _incrementar_versao_fonte_notas():
    """Força a recriação do radio/checkbox do seletor de fonte no próximo run.

    O Streamlit mantém o valor de um widget cuja chave continua na sessão; depois
    de salvar a fonte precisamos que o widget volte com o valor GRAVADO no banco.
    """
    versao = st.session_state.get(CHAVE_VERSAO_FONTE_NOTAS, 0)
    if not isinstance(versao, int):
        versao = 0
    st.session_state[CHAVE_VERSAO_FONTE_NOTAS] = versao + 1


def _limpar_app_secret_omie():
    """Remove da sessão qualquer App Secret digitado no painel Omie.

    Executado no início do run (antes dos widgets) depois de um envio
    bem-sucedido. Sem isso o valor continuaria no ``session_state`` e o
    ``type="password"`` do Streamlit permitiria revelá-lo pelo ícone do olho.
    O App Key não é segredo e permanece.
    """
    for chave in list(st.session_state.keys()):
        if str(chave).startswith(PREFIXO_APP_SECRET_OMIE):
            st.session_state.pop(chave, None)


def salvar_credenciais_omie(cnpj, app_key, app_secret):
    """POST ``/api/omie/credenciais``: grava o par cifrado no backend.

    Retorna ``(dados, erro)``. O backend nunca devolve o segredo inteiro — só a
    máscara dos 4 últimos caracteres; a tela usa isso como confirmação.
    """
    try:
        resposta = requests.post(
            OMIE_CREDENCIAIS_URL,
            json={
                "cnpj": re.sub(r"\D", "", str(cnpj or "")),
                "app_key": str(app_key or "").strip(),
                "app_secret": str(app_secret or "").strip(),
            },
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


def sincronizar_omie(cnpj, simular=True):
    """POST ``/api/omie/sincronizar``: importa os recebimentos da loja.

    ``simular=True`` roda tudo e desfaz no fim (nada é gravado). O backend usa a
    janela/etapas padrão quando o corpo não traz datas. Retorna ``(dados, erro)``.
    """
    try:
        resposta = requests.post(
            OMIE_SINCRONIZAR_URL,
            json={
                "cnpj": re.sub(r"\D", "", str(cnpj or "")),
                "simular": bool(simular),
            },
            headers=cabecalhos_api(),
            timeout=900,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


@st.cache_data(ttl=30, show_spinner=False)
def consultar_status_omie(cnpj, token):
    """GET ``/api/omie/status/{cnpj}``: última sync, contagens e credencial.

    O cache de 30 s evita repetir a consulta a cada interação da tela (o
    Streamlit reexecuta o script inteiro a cada clique). Retorna ``(dados, erro)``;
    a resposta nunca inclui o app_secret.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    try:
        resposta = requests.get(
            f"{OMIE_STATUS_URL}/{quote(cnpj_limpo, safe='')}",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=60,
        )
    except Exception as erro:
        return None, f"Não foi possível consultar o status do Omie: {erro}"
    if resposta.status_code == 200:
        try:
            return resposta.json() or {}, None
        except ValueError:
            return None, "A API respondeu ao status do Omie em formato inválido."
    return None, mensagem_de_erro(resposta)


def definir_fonte_notas_loja(empresa_id, fonte, concordancia_sefaz=False):
    """POST ``/api/empresas/{id}/fonte-notas``: ativa Omie ou certificado A1.

    A fonte ``"certificado"`` só é aceita com ``concordancia_sefaz=True`` — o
    backend grava data/hora + e-mail de quem confirmou o aviso. Retorna
    ``(dados, erro)``.
    """
    try:
        resposta = requests.post(
            f"{EMPRESA_FONTE_NOTAS_URL}/{int(empresa_id)}/fonte-notas",
            json={
                "fonte": str(fonte or "").strip().lower(),
                "concordancia_sefaz": bool(concordancia_sefaz),
            },
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)




@st.cache_data(ttl=15, show_spinner=False)
def carregar_config_alertas(token):
    """Preferências de alerta da conta. Retorna (dados, erro)."""
    try:
        resposta = requests.get(
            ALERTAS_CONFIG_URL, headers=cabecalhos_api(token), timeout=30
        )
    except Exception as erro:
        return None, f"Não foi possível carregar a configuração dos alertas: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


def _corpo_do_alerta(
    ativo,
    telefone,
    variacao_minima,
    janela_dias,
    incluir_aumentos,
    incluir_divergencias,
    lojas,
    email="",
    canal="whatsapp",
    email_secundario="",
    alerta_diario=True,
    alerta_semanal=True,
    alerta_mensal=True,
):
    """Corpo JSON comum das rotas de alerta (salvar e simulação)."""
    return {
        "ativo": bool(ativo),
        "telefone": str(telefone or ""),
        "email": str(email or ""),
        "email_secundario": str(email_secundario or ""),
        "canal": str(canal or "whatsapp"),
        "variacao_minima": float(variacao_minima),
        "janela_dias": int(janela_dias),
        "incluir_aumentos": bool(incluir_aumentos),
        "incluir_divergencias": bool(incluir_divergencias),
        "lojas": [str(cnpj) for cnpj in (lojas or [])],
        # Tipos do relatório automático (06:00, segunda e dia 1º).
        "alerta_diario": bool(alerta_diario),
        "alerta_semanal": bool(alerta_semanal),
        "alerta_mensal": bool(alerta_mensal),
    }


def salvar_config_alertas(
    ativo,
    telefone,
    variacao_minima,
    janela_dias,
    incluir_aumentos,
    incluir_divergencias,
    lojas,
    email="",
    canal="whatsapp",
    email_secundario="",
    alerta_diario=True,
    alerta_semanal=True,
    alerta_mensal=True,
):
    """Grava as preferências de alerta. Retorna (dados, erro)."""
    try:
        resposta = requests.post(
            ALERTAS_CONFIG_URL,
            json=_corpo_do_alerta(
                ativo,
                telefone,
                variacao_minima,
                janela_dias,
                incluir_aumentos,
                incluir_divergencias,
                lojas,
                email,
                canal,
                email_secundario,
                alerta_diario,
                alerta_semanal,
                alerta_mensal,
            ),
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


def prever_relatorio_alertas(tipo="diario", lojas=None):
    """POST /api/alertas/relatorio/previa: relatório do período (nada é enviado).

    Devolve ``(relatorio, erro)``. O backend monta o MESMO lote que o envio real
    (notas novas/semana/mês + atrasadas) e devolve resumo, variações, novos produtos
    e o índice de inflação — a tela só exibe.
    """
    try:
        resposta = requests.post(
            ALERTAS_RELATORIO_PREVIA_URL,
            json={
                "tipo": str(tipo or "diario"),
                "lojas": [str(cnpj) for cnpj in (lojas or [])],
            },
            headers=cabecalhos_api(),
            timeout=180,
        )
    except Exception as erro:
        return None, f"Não foi possível gerar a prévia do relatório: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


def carregar_memoria_calculo_alertas(tipo="mensal"):
    """GET /api/alertas/relatorio/memoria-calculo: Excel da conta do mês fechado.

    Devolve ``(conteudo_bytes, nome_arquivo, mes_referencia, erro)`` — nunca levanta
    exceção. O backend monta o arquivo com a MESMA base da prévia/envio; aqui só
    guardamos os bytes para o `st.download_button` servir o mesmo .xlsx em cada rerun.
    """
    try:
        resposta = requests.get(
            ALERTAS_MEMORIA_CALCULO_URL,
            params={"tipo": str(tipo or "mensal")},
            headers=cabecalhos_api(),
            timeout=300,
        )
    except Exception as erro:
        return None, None, None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        if not resposta.content:
            return None, None, None, "O backend devolveu um arquivo vazio."
        nome_arquivo = "memoria_calculo_inflacao.xlsx"
        disposicao = resposta.headers.get("Content-Disposition", "")
        encontrado = re.search(r'filename="?([^";]+)"?', disposicao)
        if encontrado:
            nome_arquivo = encontrado.group(1).strip() or nome_arquivo
        return (
            resposta.content,
            nome_arquivo,
            resposta.headers.get("X-Mes-Referencia"),
            None,
        )
    return None, None, None, mensagem_de_erro(resposta)


def enviar_relatorio_alertas(tipo="diario", lojas=None):
    """POST /api/alertas/relatorio/enviar: envia o relatório por e-mail agora.

    O destinatário é decidido no backend (`configuracoes_alerta.email`); o front
    manda apenas o tipo/período. A senha SMTP nunca passa pelo navegador.
    """
    try:
        resposta = requests.post(
            ALERTAS_RELATORIO_ENVIAR_URL,
            json={
                "tipo": str(tipo or "diario"),
                "lojas": [str(cnpj) for cnpj in (lojas or [])],
            },
            headers=cabecalhos_api(),
            timeout=180,
        )
    except Exception as erro:
        return None, f"Não foi possível enviar o relatório: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    # Inclui o status HTTP para deixar claro que a mensagem veio da API. O backend
    # devolve em `detail` o erro SMTP real (classe/código/resposta do Gmail).
    return None, f"HTTP {resposta.status_code} — {mensagem_de_erro(resposta)}"



@st.cache_data(ttl=15, show_spinner=False)
def carregar_config_admin(token):
    """Token da API do WhatsApp (só admin + 2FA). Retorna (dados, erro)."""
    try:
        resposta = requests.get(
            ADMIN_CONFIG_URL, headers=cabecalhos_api(token), timeout=30
        )
    except Exception as erro:
        return None, f"Não foi possível carregar as configurações: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


def salvar_config_admin(token_whatsapp):
    """Grava (ou remove, com campo vazio) o token da API do WhatsApp."""
    try:
        resposta = requests.post(
            ADMIN_CONFIG_URL,
            json={"whatsapp_token": str(token_whatsapp or "")},
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return None, f"Não foi possível conectar ao servidor da API: {erro}"
    if resposta.status_code == 200:
        return resposta.json() or {}, None
    return None, mensagem_de_erro(resposta)


def pagina_da_url():
    """Página pedida na URL (`?pagina=minhas-lojas`) ou None quando inválida.

    Devolve None também para páginas que o perfil não pode ver (o menu já esconde
    a Auditoria de quem não é admin — a URL não pode furar essa regra).
    """
    try:
        valor = str(st.query_params.get(CHAVE_QUERY_PAGINA) or "").strip().lower()
    except Exception:
        return None
    pagina = URL_PAGINAS.get(valor)
    if pagina not in paginas_disponiveis():
        return None
    return pagina


def restaurar_pagina_da_url():
    """Completa a página da sessão com a URL ANTES do widget do menu.

    A URL é **fallback, nunca autoridade**: ela só entra quando a sessão perdeu a
    página (recarga do navegador, reconexão do websocket, `st.session_state`
    descartado). Com `SESSAO_PAGINA` preenchida, quem manda é a sessão — é ela que
    guarda a navegação explícita recém-aplicada (`ir_para`/`aplicar_navegacao_pendente`).

    REGRA: nada neste app escreve em `st.query_params` durante o run. Escrever na URL
    faz o Streamlit enfileirar um rerun extra com o estado de widgets do run anterior
    (`UPDATE_FROM_QUERY_PARAMS` -> `sendRerunBackMsg`) e era a causa do menu voltar
    para "Relatórios" no primeiro submit do cadastro de loja. Ver `URL_PAGINAS`.
    """
    pagina = pagina_da_url()
    if not pagina:
        return
    atual = st.session_state.get(SESSAO_PAGINA)
    if atual and atual in paginas_disponiveis():
        return
    st.session_state[SESSAO_PAGINA] = pagina
    if st.session_state.get(CHAVE_MENU) != pagina:
        st.session_state.pop(CHAVE_MENU, None)


def _diagnosticar_menu(paginas, pagina):
    """Duas linhas por run no console do Streamlit.

    Serve para identificar a origem de uma troca inesperada de tela: `run` voltando a
    1 no meio do cadastro = sessão refeita (login silencioso); `run` crescendo com a
    página mudando sem clique = estado de widget descartado.
    """
    try:
        rodadas = int(st.session_state.get(CHAVE_RODADAS_MENU) or 0) + 1
    except Exception:
        rodadas = 1
    st.session_state[CHAVE_RODADAS_MENU] = rodadas
    print(
        "[menu] "
        f"run={rodadas} "
        f"pagina={pagina!r} "
        f"sessao_pagina={st.session_state.get(SESSAO_PAGINA)!r} "
        f"widget={st.session_state.get(CHAVE_MENU)!r} "
        f"url={st.query_params.get(CHAVE_QUERY_PAGINA)!r} "
        f"opcoes={len(paginas)} "
        f"admin={usuario_e_admin()}",
        flush=True,
    )
    anterior = st.session_state.get(CHAVE_PAGINA_ANTERIOR)
    if anterior in paginas and anterior != pagina:
        print(
            "[menu] AVISO estado do widget divergiu da pagina anterior: "
            f"anterior={anterior!r} atual={pagina!r} run={rodadas} — "
            "troca sem clique no menu (widget recriado/estado descartado).",
            flush=True,
        )
    st.session_state[CHAVE_PAGINA_ANTERIOR] = pagina


def ir_para(pagina):
    """Pede a troca de página: aplicada no próximo run, antes dos widgets da sidebar."""
    st.session_state[NAVEGACAO_PENDENTE] = {"pagina": pagina}
    st.rerun()


def aplicar_navegacao_pendente():
    """Aplica a navegação pedida no run anterior.

    Precisa rodar ANTES do widget do menu: apagar a chave dele força o `index` novo (o
    Streamlit não deixa alterar o estado de um widget já criado).
    """
    destino = st.session_state.pop(NAVEGACAO_PENDENTE, None)
    if not destino:
        return
    st.session_state.pop(CHAVE_MENU, None)
    pagina = destino.get("pagina")
    st.session_state[SESSAO_PAGINA] = (
        pagina if pagina in paginas_disponiveis() else PAGINA_RELATORIOS
    )


def _tratar_sessao_2fa_expirada(response):
    """Token 2FA vencido/recusado: limpa a sessão para a tela pedir o código de novo."""
    if response.status_code in (401, 403) and (
        response.headers.get("X-2FA-Required") or "2fa" in response.text.lower()
    ):
        _limpar_sessao_2fa()
        return True
    return False


def carregar_auditados():
    """Chaves com descrição aprovada. Rota de admin + 2FA: vai com os dois tokens."""
    try:
        response = requests.get(LIST_AUDIT_URL, headers=cabecalhos_api(), timeout=30)
        if response.status_code == 200:
            return response.json()
        _tratar_sessao_2fa_expirada(response)
    except Exception:
        pass
    return []


def carregar_pendentes_auditoria(motivo=None):
    """Chaves marcadas como "Pendente de Aprovação" na auditoria.

    `motivo="aberrante"` = variações bruscas enviadas automaticamente pelo Impacto;
    `motivo="manual"` = itens enviados à mão (saem dos relatórios); None = todas.
    """
    parametros = {"motivo": motivo} if motivo else None
    try:
        response = requests.get(
            LIST_AUDIT_PENDENTE_URL,
            params=parametros,
            headers=cabecalhos_api(),
            timeout=30,
        )
        if response.status_code == 200:
            return response.json()
        _tratar_sessao_2fa_expirada(response)
    except Exception:
        pass
    return []


def enviar_pendentes_auditoria(chaves, motivo=MOTIVO_PENDENCIA_MANUAL):
    """Marca chaves como pendentes na auditoria. Retorna (quantidade, erro).

    `motivo="aberrante"` registra a variação brusca sem tirar o item da tabela (ele fica
    fora dos totais e aguardando validação); `motivo="manual"` remove o item dos
    relatórios até o auditor validar.
    """
    chaves = [str(chave) for chave in (chaves or []) if chave]
    if not chaves:
        return 0, "Nenhuma chave para enviar à auditoria."
    try:
        resposta = requests.post(
            AUDIT_PENDENTE_URL,
            json={"chaves": chaves, "motivo": motivo},
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return 0, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code != 200:
        return 0, f"Erro ao enviar para a auditoria ({resposta.status_code}): {resposta.text}"
    st.cache_data.clear()  # relatórios e auditoria mudam na hora
    try:
        return int(resposta.json().get("marcadas") or len(chaves)), None
    except Exception:
        return len(chaves), None


def descrever_chave_pendente(chave):
    """Descrição legível de uma chave DESC_... (o texto limpo vem depois do último '_')."""
    texto = str(chave or "")
    if texto.startswith("DESC_"):
        descricao = texto.rsplit("_", 1)[-1].strip()
        if descricao:
            return descricao
    return texto


def carregar_regras_agrupamento():
    """Regras de-para do catálogo (rota de admin: vai com o token da sessão)."""
    try:
        response = requests.get(REGRAS_URL, headers=cabecalhos_api(), timeout=30)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return []


def carregar_aprendizado(busca="", limite=300):
    """De-Para aprendido (fornecedor + cProd) + contadores de embalagem.

    Rota de admin + 2FA. Devolve o dicionário do backend ou um dicionário vazio com a
    chave `erro` (a tela mostra o aviso em vez de quebrar).
    """
    parametros = {"limite": int(limite)}
    if busca:
        parametros["busca"] = busca
    try:
        resposta = requests.get(
            APRENDIZADO_URL, params=parametros, headers=cabecalhos_api(), timeout=60
        )
    except Exception as erro:
        return {"erro": f"Erro de conexão com o backend: {erro}"}
    if resposta.status_code == 200:
        return resposta.json()
    if not _tratar_sessao_2fa_expirada(resposta):
        try:
            detalhe = resposta.json().get("detail", resposta.text)
        except Exception:
            detalhe = resposta.text
        return {"erro": f"Erro ao carregar o aprendizado ({resposta.status_code}): {detalhe}"}
    return {"erro": "Sessão 2FA expirada: informe o código do autenticador novamente."}


def salvar_aprendizado(cnpj, c_prod, ean="", descricao="", ncm="", fator=None, unidade="UN"):
    """Corrige um De-Para manualmente (a importação não sobrescreve depois).

    Retorna (dados, erro). `fator` None mantém a embalagem atual.
    """
    corpo = {
        "cnpj_fornecedor": str(cnpj or "").strip(),
        "c_prod": str(c_prod or "").strip(),
        "ean_padrao": str(ean or "").strip(),
        "descricao_padrao": str(descricao or "").strip(),
        "ncm_gravado": str(ncm or "").strip(),
        "unidade_base": str(unidade or "UN").strip().upper() or "UN",
    }
    if fator is not None:
        corpo["qtd_por_embalagem"] = float(fator)
    try:
        resposta = requests.put(
            APRENDIZADO_URL, json=corpo, headers=cabecalhos_api(), timeout=60
        )
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        st.cache_data.clear()
        return resposta.json(), None
    try:
        detalhe = resposta.json().get("detail", resposta.text)
    except Exception:
        detalhe = resposta.text
    return None, f"Erro ao salvar o de-para ({resposta.status_code}): {detalhe}"


def reprocessar_aprendizado():
    """Roda o backfill da memória/embalagens pela API (mesmo motor do script)."""
    try:
        resposta = requests.post(
            APRENDIZADO_REPROCESSAR_URL, headers=cabecalhos_api(), timeout=300
        )
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        st.cache_data.clear()
        return resposta.json(), None
    if _tratar_sessao_2fa_expirada(resposta):
        return None, "Sessão 2FA expirada: informe o código do autenticador novamente."
    try:
        detalhe = resposta.json().get("detail", resposta.text)
    except Exception:
        detalhe = resposta.text
    return None, f"Erro ao reprocessar ({resposta.status_code}): {detalhe}"


# ── Memória de marcas da IA (tabela marcas_ia) ────────────────────────────────
def _detalhe_erro_api(resposta):
    try:
        return resposta.json().get("detail", resposta.text)
    except Exception:
        return resposta.text


def carregar_descricoes_marcas_ia():
    """Descrições únicas normalizadas da base + resumo do cache de marcas (admin/2FA)."""
    try:
        resposta = requests.get(
            MARCAS_IA_DESCRICOES_URL, headers=cabecalhos_api(), timeout=120
        )
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        return resposta.json(), None
    if _tratar_sessao_2fa_expirada(resposta):
        return None, "Sessão 2FA expirada: informe o código do autenticador novamente."
    return None, (
        f"Erro ao carregar as descrições ({resposta.status_code}): "
        f"{_detalhe_erro_api(resposta)}"
    )


def processar_marcas_ia_lote(descricoes, usar_ia=True):
    """Analisa um lote: cache primeiro; DeepSeek só nas descrições inéditas."""
    corpo = {"descricoes": list(descricoes), "usar_ia": bool(usar_ia)}
    try:
        resposta = requests.post(
            MARCAS_IA_PROCESSAR_URL,
            json=corpo,
            headers=cabecalhos_api(),
            timeout=600,
        )
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        return resposta.json(), None
    if _tratar_sessao_2fa_expirada(resposta):
        return None, "Sessão 2FA expirada: informe o código do autenticador novamente."
    return None, (
        f"Erro ao processar o lote de marcas ({resposta.status_code}): "
        f"{_detalhe_erro_api(resposta)}"
    )


def aplicar_memoria_marcas_ia():
    """Recalcula `itens_nota.marca` usando SOMENTE a memória + heurística local."""
    try:
        resposta = requests.post(
            MARCAS_IA_APLICAR_URL, headers=cabecalhos_api(), timeout=600
        )
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        st.cache_data.clear()
        return resposta.json(), None
    if _tratar_sessao_2fa_expirada(resposta):
        return None, "Sessão 2FA expirada: informe o código do autenticador novamente."
    return None, (
        f"Erro ao aplicar as marcas ({resposta.status_code}): "
        f"{_detalhe_erro_api(resposta)}"
    )


@st.cache_data(ttl=300, show_spinner=False)
def carregar_nota(item_id):
    """Busca no backend o DANFE do item. Retorna (dados, erro) — nunca levanta exceção."""
    try:
        response = requests.get(
            f"{NOTA_URL}/{int(item_id)}", headers=cabecalhos_api(), timeout=30
        )
    except Exception as e:
        return None, f"Erro de conexão com o backend: {e}"
    if response.status_code != 200:
        try:
            detalhe = response.json().get("detail", response.text)
        except Exception:
            detalhe = response.text
        return None, f"Erro ao carregar a nota ({response.status_code}): {detalhe}"
    try:
        return response.json(), None
    except Exception as e:
        return None, f"Resposta inválida do backend: {e}"


@st.cache_data(ttl=60, show_spinner=False)
def carregar_relatorio(url, parametros_json):
    """Consulta um endpoint de relatório. Retorna (dados, erro) — nunca levanta exceção.

    `parametros_json` é o JSON dos parâmetros em string porque é hashável: é isso que
    permite o @st.cache_data reaproveitar a resposta entre reruns do Streamlit (em vez
    de recalcular a cada clique). Listas (ex.: lojas) viram parâmetros repetidos no GET.
    """
    parametros = json.loads(parametros_json)
    try:
        resposta = requests.get(url, params=parametros, timeout=120)
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code != 200:
        try:
            detalhe = resposta.json().get("detail", resposta.text)
        except Exception:
            detalhe = resposta.text
        return None, f"Erro ao carregar o relatório ({resposta.status_code}): {detalhe}"
    try:
        return resposta.json(), None
    except Exception as erro:
        return None, f"Resposta inválida do backend: {erro}"


@st.cache_data(ttl=30, show_spinner=False)
def carregar_contexto_aberracoes():
    """Contexto das variações bruscas: grupo, última e penúltima compra.

    Usa o próprio relatório de Impacto (fonte única dos números) e indexa pela
    `chave_descricao`, que é a mesma chave gravada em `itens_auditados`. Devolve `{}` em
    caso de erro: a Auditoria continua funcionando e mostra só a descrição da pendência.
    """
    dados, erro = carregar_relatorio(
        RELATORIO_IMPACTO_URL,
        json.dumps({"meses_consumo": 3, "limite": 5000}, sort_keys=True),
    )
    if erro or not isinstance(dados, dict):
        return {}
    contexto = {}
    for linha in dados.get("linhas") or []:
        if linha.get("chave_descricao"):
            contexto[str(linha["chave_descricao"])] = linha
        # A descrição que gerou a pendência pode ser outra do mesmo grupo; a chave do
        # produto é o elo estável entre o relatório e a Auditoria.
        if linha.get("chave"):
            contexto[str(linha["chave"])] = linha
    return contexto


def consultar_ia_ambiguidade(texto1, texto2):
    """Pede ao backend o veredito do DeepSeek para um par de descrições.

    Retorna `(resultado, erro)` — nunca levanta exceção. A rota exige admin + 2FA;
    token vencido limpa a sessão para a tela pedir o código novamente.
    """
    try:
        resposta = requests.post(
            IA_RESOLVER_URL,
            json={"texto1": str(texto1 or ""), "texto2": str(texto2 or "")},
            headers=cabecalhos_api(),
            timeout=120,
        )
    except Exception as erro:
        return None, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        try:
            return resposta.json(), None
        except Exception as erro:
            return None, f"Resposta inválida da IA: {erro}"
    if _tratar_sessao_2fa_expirada(resposta):
        return None, "Sessão 2FA expirada: informe o código do autenticador novamente."
    try:
        detalhe = resposta.json().get("detail", resposta.text)
    except Exception:
        detalhe = resposta.text
    return None, f"A IA não respondeu ({resposta.status_code}): {detalhe}"


def carregar_exportacao_grupos_completos():
    """Baixa o raio-x de TODA a base em Excel.

    Retorna `(conteudo_bytes, nome_arquivo, total_grupos, erro)` — nunca levanta
    exceção. O arquivo fica em memória para o `st.download_button` servir o mesmo
    conteúdo em cada rerun (sem baixar de novo só porque a tela atualizou).
    """
    try:
        resposta = requests.get(
            EXPORTAR_GRUPOS_URL, headers=cabecalhos_api(), timeout=300
        )
    except Exception as erro:
        return None, None, 0, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        if not resposta.content:
            return None, None, 0, "O backend devolveu um arquivo vazio."
        nome_arquivo = "grupos_com_variacoes.xlsx"
        disposicao = resposta.headers.get("Content-Disposition", "")
        encontrado = re.search(r'filename="?([^";]+)"?', disposicao)
        if encontrado:
            nome_arquivo = encontrado.group(1).strip() or nome_arquivo
        try:
            total_grupos = int(resposta.headers.get("X-Total-Grupos") or 0)
        except (TypeError, ValueError):
            total_grupos = 0
        return resposta.content, nome_arquivo, total_grupos, None
    if _tratar_sessao_2fa_expirada(resposta):
        return None, None, 0, "Sessão 2FA expirada: informe o código do autenticador novamente."
    try:
        detalhe = resposta.json().get("detail", resposta.text)
    except Exception:
        detalhe = resposta.text
    return (
        None,
        None,
        0,
        f"Erro ao gerar a exportação ({resposta.status_code}): {detalhe}",
    )


def carregar_exportacao_catalogo_completo():
    """Baixa o catálogo MASTER (todas as descrições, por Produto + Marca).

    Retorna `(conteudo_bytes, nome_arquivo, total_produtos, total_descricoes,
    erro)` — nunca levanta exceção. O conteúdo fica em memória para o
    `st.download_button` servir o mesmo arquivo em cada rerun.
    """
    try:
        resposta = requests.get(
            EXPORTAR_CATALOGO_URL, headers=cabecalhos_api(), timeout=300
        )
    except Exception as erro:
        return None, None, 0, 0, f"Erro de conexão com o backend: {erro}"
    if resposta.status_code == 200:
        if not resposta.content:
            return None, None, 0, 0, "O backend devolveu um arquivo vazio."
        nome_arquivo = "catalogo_completo.xlsx"
        disposicao = resposta.headers.get("Content-Disposition", "")
        encontrado = re.search(r'filename="?([^";]+)"?', disposicao)
        if encontrado:
            nome_arquivo = encontrado.group(1).strip() or nome_arquivo
        try:
            total_produtos = int(resposta.headers.get("X-Total-Produtos") or 0)
        except (TypeError, ValueError):
            total_produtos = 0
        try:
            total_descricoes = int(resposta.headers.get("X-Total-Descricoes") or 0)
        except (TypeError, ValueError):
            total_descricoes = 0
        return (
            resposta.content,
            nome_arquivo,
            total_produtos,
            total_descricoes,
            None,
        )
    if _tratar_sessao_2fa_expirada(resposta):
        return (
            None,
            None,
            0,
            0,
            "Sessão 2FA expirada: informe o código do autenticador novamente.",
        )
    try:
        detalhe = resposta.json().get("detail", resposta.text)
    except Exception:
        detalhe = resposta.text
    return (
        None,
        None,
        0,
        0,
        f"Erro ao gerar o catálogo ({resposta.status_code}): {detalhe}",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Preparação dos dados
# ──────────────────────────────────────────────────────────────────────────────
def preparar_dataframe(df):
    """Normaliza tipos, datas e colunas derivadas usadas pelas telas."""
    df = df.copy()
    if "descricao" in df.columns and "descricao_produto" not in df.columns:
        df["descricao_produto"] = df["descricao"]
    elif "descricao_produto" not in df.columns:
        df["descricao_produto"] = "Produto sem descrição"

    # uCom do XML: entra na chave como identidade da embalagem (CX24 x UN).
    # Bases antigas sem a coluna continuam agrupando pela descrição.
    if "unidade_compra" not in df.columns:
        df["unidade_compra"] = ""
    else:
        df["unidade_compra"] = df["unidade_compra"].fillna("").astype(str)

    if "ncm" in df.columns:
        df["capitulo_ncm"] = df["ncm"].astype(str).str.zfill(8).str[:2]
    else:
        df["capitulo_ncm"] = "00"
        df["ncm"] = "00000000"

    # Marca inferida (backend/marcas.py). Sem a coluna (base antiga/sem backfill),
    # o agrupamento cai no nome base. "NÃO IDENTIFICADA" vira None.
    if "marca" not in df.columns:
        df["marca"] = None
    else:
        # Usa a MESMA função da chave: normaliza acento e trata nula/"NÃO
        # IDENTIFICADA" como ausência de marca (o nome base agrupa).
        df["marca"] = [
            str(marca).strip() if _marca_para_chave(marca) else None
            for marca in df["marca"]
        ]

    df["data_emissao"] = pd.to_datetime(df["data_emissao"], errors="coerce")
    for coluna in ("valor_unitario", "quantidade", "valor_total"):
        if coluna in df.columns:
            df[coluna] = pd.to_numeric(df[coluna], errors="coerce").fillna(0.0)
    df["mes"] = df["data_emissao"].dt.to_period("M").dt.to_timestamp()
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Formatação (padrão brasileiro: DD/MM/AAAA e R$ 1.234,56)
# ──────────────────────────────────────────────────────────────────────────────
def formatar_moeda(valor):
    """Formata números como moeda brasileira (R$ 1.357.622,96)."""
    if valor is None or pd.isna(valor):
        return "—"
    texto = f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def formatar_numero(valor):
    """Formata números inteiros com separador de milhar brasileiro (5.327)."""
    if valor is None or pd.isna(valor):
        return "—"
    return f"{float(valor):,.0f}".replace(",", ".")


def formatar_percentual(valor):
    """Formata percentuais com sinal e uma casa decimal (+12,3%)."""
    if valor is None or pd.isna(valor):
        return "—"
    return f"{float(valor):+.1f}%".replace(".", ",")


def formatar_data(valor, formato="%d/%m/%Y"):
    """Formata datas no padrão brasileiro, sem o horário 00:00:00."""
    if valor is None or pd.isna(valor):
        return "—"
    return pd.Timestamp(valor).strftime(formato)


def formatar_quantidade(valor):
    """Quantidade com até 3 decimais no padrão brasileiro (2,5 / 1.234,75)."""
    if valor is None or pd.isna(valor):
        return "—"
    texto = f"{float(valor):,.3f}".rstrip("0").rstrip(".")
    if not texto:
        texto = "0"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_cnpj(cnpj):
    """CNPJ com máscara 00.000.000/0000-00 (mantém o valor original se não tiver 14 dígitos)."""
    digitos = re.sub(r"\D", "", str(cnpj or ""))
    if len(digitos) != 14:
        return str(cnpj or "—")
    return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"


def formatar_chave_acesso(chave):
    """Chave de acesso da NFe em grupos de 4 dígitos, como no DANFE."""
    digitos = re.sub(r"\D", "", str(chave or ""))
    if not digitos:
        return "—"
    return " ".join(digitos[i : i + 4] for i in range(0, len(digitos), 4))


# ──────────────────────────────────────────────────────────────────────────────
# Chaves inteligentes de produto (auditoria / agrupamento)
# ──────────────────────────────────────────────────────────────────────────────
def limpar_texto(texto):
    nfkd = unicodedata.normalize("NFKD", str(texto))
    limpo = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = re.sub(r"[^A-Z0-9\s]", "", limpo.upper())
    return " ".join(limpo.split())


def _remover_numero_controle(descricao):
    """Remove sufixos de controle do fornecedor ("- 1", "- 2") antes de agrupar.

    O texto original continua intacto (é o que aparece na tela/exportação);
    apenas a chave inteligente usa a versão sem o número.
    """
    original = str(descricao or "").strip()
    sem_controle = re.sub(r"(?:\s*[-–—]\s*\d{1,3})+\s*$", "", original).strip()
    return sem_controle or original


def _descricoes_de_chaves_pendentes(chaves, descricoes):
    """Mapeia chaves ``DESC_...`` para as descrições REAIS da base.

    A chave carrega a descrição no fim (``DESC_<grupo>_<descrição normalizada>``), mas
    o prefixo do grupo muda quando entra uma regra nova de marca/volume/embalagem. Uma
    variação brusca gravada com a chave antiga deixava de casar com o card e o aviso
    amarelo contava itens que a lista não mostrava. Comparar pelo SUFIXO (a descrição)
    mantém aviso e lista consistentes mesmo com chaves legadas.

    Devolve um conjunto de descrições normalizadas (sem acento/pontuação).
    """
    normalizadas = set()
    for descricao in descricoes or []:
        normalizada = limpar_texto(str(descricao or "").strip())
        if normalizada:
            normalizadas.add(normalizada)
    encontradas = set()
    for chave in chaves or []:
        texto_chave = str(chave or "").strip()
        if not texto_chave:
            continue
        for normalizada in normalizadas:
            if texto_chave.endswith("_" + normalizada):
                encontradas.add(normalizada)
    return encontradas


# Regra obrigatória de volume/peso — o MESMO bloco existe em backend/main.py
# (`extrair_volume`). Qualquer mudança aqui precisa ser replicada lá, senão o
# front e os relatórios geram chaves diferentes para o mesmo produto.
_UNIDADES_VOLUME = (
    r"MILILITROS?|MILILITRO|ML|LITROS?|LITRO|LTS?|LT|L|"
    r"QUILOGRAMAS?|QUILOGRAMA|QUILOS?|QUILO|KGS?|KG|"
    r"MILIGRAMAS?|MILIGRAMA|MGS?|MG|GRAMAS?|GRAMA|GRS?|GR|G"
)
_VOLUME_RE = re.compile(
    rf"(?<![A-Z0-9])(?:(?P<multiplicador>\d+(?:[.,]\d+)?)\s*[Xx]\s*)?"
    rf"(?P<quantidade>\d+(?:[.,]\d+)?)\s*(?P<unidade>{_UNIDADES_VOLUME})(?![A-Z])",
    re.IGNORECASE,
)
_VOLUME_SUFIXO_RE = re.compile(r"(?:_VOL_[^_]+)+$")
# Produto + Marca: sufixo obrigatório na chave (mesmo token do backend).
_MARCA_SUFIXO_RE = re.compile(r"(?:_MARCA_[^_]+)+$")
# Embalagem como identidade do produto (CX24 x UN). Mesmo token do backend.
_EMB_SUFIXO_RE = re.compile(r"(?:_EMB_[^_]+)+$")
_TIPOS_EMBALAGEM_CANONICOS = {
    "CAIXA": "CX", "CXA": "CX", "CXS": "CX", "CX": "CX",
    "FARDO": "FD", "FRD": "FD", "FDO": "FD", "FD": "FD",
    "PACOTE": "PCT", "PCTE": "PCT", "PCT": "PCT", "PACK": "PCT", "PAC": "PCT",
    "BANDEJA": "BG", "BG": "BG", "BD": "BD",
    "SACARIA": "SC", "SACO": "SC", "SC": "SC",
    "POTE": "PT", "TP": "PT", "PT": "PT",
    "GALAO": "GL", "GL": "GL",
    "GARRAFA": "GF", "GRF": "GF", "GF": "GF",
    "DUZIA": "DZ", "DZ": "DZ",
    "DISPLAY": "DISPLAY", "ENGRADADO": "ENGRADADO",
    "ROLO": "RO", "RO": "RO", "BOLSA": "BO", "BO": "BO",
    "FRASCO": "FR", "FR": "FR", "MACO": "MC", "MC": "MC",
}
_PALAVRAS_EMBALAGEM_RE = re.compile(
    r"\b(CAIXA|CXA|CXS|CX|FARDO|FRD|FDO|FD|PACOTE|PCTE|PCT|PACK|PAC|"
    r"BANDEJA|BG|BD|SACARIA|SACO|SC|POTE|TP|PT|GALAO|GL|GARRAFA|GRF|GF|"
    r"DUZIA|DZ|DISPLAY|ENGRADADO|ROLO|RO|BOLSA|BO|FRASCO|FR|MACO|MC)\b"
)
_UNIDADES_EMBALAGEM = (
    r"KG|KGS|G|GR|GRS|MG|L|LT|LTS|ML|CL"
)
MARCAS_NAO_IDENTIFICADAS = {
    "",
    "0",
    "NAN",
    "NONE",
    "NULL",
    "SEM MARCA",
    "NAO IDENTIFICADA",
    "NAOIDENTIFICADA",
    "MARCA NAO IDENTIFICADA",
}


def _decimal_medida(valor):
    """Converte "1,5"/"1.5" em Decimal; None quando o texto não é numérico."""
    try:
        return Decimal(str(valor).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _formatar_medida(valor):
    """Formata a medida sem zeros inúteis (1.500 -> 1.5; 20.0 -> 20)."""
    texto = format(valor.normalize(), "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto or "0"


def _tipo_embalagem_canonico(codigo):
    """Tipo canônico da uCom (CXA1 -> CX, FARDO -> FD) ou ""."""
    codigo = re.sub(r"[^A-Z0-9]", "", str(codigo or "").upper())
    for prefixo in sorted(_TIPOS_EMBALAGEM_CANONICOS, key=len, reverse=True):
        if codigo.startswith(prefixo):
            return _TIPOS_EMBALAGEM_CANONICOS[prefixo]
    return ""


def _tipo_embalagem_na_descricao(texto):
    """Primeiro tipo de embalagem citado na descrição normalizada."""
    achado = _PALAVRAS_EMBALAGEM_RE.search(texto or "")
    return _TIPOS_EMBALAGEM_CANONICOS.get(achado.group(1), "") if achado else ""


def _numero_token_embalagem(valor):
    """Número curto do token (24.0 -> 24; 1.5 -> 1.5)."""
    numero = float(valor or 0.0)
    texto = f"{numero:.3f}".rstrip("0").rstrip(".")
    return texto or "0"


def _normalizar_texto_embalagem(texto):
    """Normaliza preservando vírgula/ponto (15,000KG) — espelho de normalizar_texto."""
    nfkd = unicodedata.normalize("NFKD", str(texto or ""))
    limpo = "".join(c for c in nfkd if not unicodedata.combining(c)).upper()
    limpo = re.sub(r"[^A-Z0-9,./]+", " ", limpo)
    return " ".join(limpo.split())


def normalizar_embalagem(unidade_compra="", descricao=""):
    """Token canônico da embalagem (CX24, UN, FD10X1KG) — espelho do backend.

    A embalagem é IDENTIDADE do produto e NÃO converte preço/quantidade:
    "ORIGINAL 600ML CX C/24" e "ORIGINAL 600ML UN" são grupos diferentes.
    """
    unidade = str(unidade_compra or "").strip()
    descricao = str(descricao or "").strip()
    texto_unidade = _normalizar_texto_embalagem(unidade)

    if not descricao and (
        _tipo_embalagem_na_descricao(texto_unidade)
        or "UNIDADE" in texto_unidade
        or re.search(r"(?<![A-Z0-9])1\s*X\s*\d", texto_unidade)
    ):
        descricao, unidade = unidade, ""

    codigo_ucom = re.sub(r"[^A-Z0-9]", "", unidade.upper())
    texto = _normalizar_texto_embalagem(descricao)
    tipo = _tipo_embalagem_canonico(codigo_ucom) or _tipo_embalagem_na_descricao(texto)
    quantidade = None

    sufixo = re.match(
        r"^(" + "|".join(
            sorted(
                {
                    re.escape(codigo)
                    for codigo in (
                        *_TIPOS_EMBALAGEM_CANONICOS,
                        "UNID", "UND", "UN",
                    )
                },
                key=len,
                reverse=True,
            )
        ) + r")(\d{1,4})$",
        codigo_ucom,
    )
    if sufixo:
        tipo = _TIPOS_EMBALAGEM_CANONICOS.get(sufixo.group(1), "") or tipo
        numero = int(sufixo.group(2))
        if 1 < numero <= 1000:
            quantidade = numero

    achado = re.search(
        r"(?<![A-Z0-9])(\d{1,4})\s*X\s*(\d+(?:[.,]\d+)?)\s*"
        r"(" + _UNIDADES_EMBALAGEM + r")\b",
        texto,
    )
    if achado and quantidade is None:
        numero = int(achado.group(1))
        valor_conteudo = _decimal_medida(achado.group(2))
        unidade_conteudo = achado.group(3).upper()
        familia = UNIDADES_MEDIDA.get(unidade_conteudo)
        if 1 <= numero <= 1000 and valor_conteudo is not None and familia:
            unidade_token = {
                "ML": "ML", "L": "L", "LT": "L", "LTS": "L", "CL": "CL",
                "G": "G", "GR": "G", "GRS": "G", "KG": "KG", "KGS": "KG",
                "MG": "MG",
            }.get(unidade_conteudo, unidade_conteudo)
            return (
                f"{tipo or 'CX'}{numero}X{_numero_token_embalagem(valor_conteudo)}"
                f"{unidade_token}"
            )

    if quantidade is None:
        achado = re.search(
            r"\b(?:" + "|".join(
                sorted(map(re.escape, _TIPOS_EMBALAGEM_CANONICOS), key=len, reverse=True)
            ) + r")\b(?:\s*[./-]*\s*(?:COM|C|CONTENDO|DE))?\s*[./-]*\s*"
            r"(\d{1,4})(?!\d)(?!\s*,\s*\d)(?!\s*[./-]*\s*(?:"
            + _UNIDADES_EMBALAGEM + r")\b)(?!\s*[./-]*\s*X\s*\d)",
            texto,
        )
        if achado:
            numero = int(achado.group(1))
            if 1 < numero <= 1000:
                quantidade = numero
                tipo = tipo or "CX"

    if quantidade is None and tipo:
        achado = re.search(
            r"(?<![A-Z0-9])(\d{1,4})(?!\d)\s*(?:UN|UND|UNID|UNIDADES)\b",
            texto,
        )
        if achado:
            numero = int(achado.group(1))
            if 1 < numero <= 1000:
                quantidade = numero

    if quantidade is None:
        achado = re.search(r"(?<![A-Z0-9])1\s*X\s*(\d{1,4})(?!\d)", texto)
        if achado:
            numero = int(achado.group(1))
            if numero == 1:
                return "UN"
            if numero <= 1000:
                quantidade = numero
                tipo = tipo or "CX"

    if quantidade is None:
        achado = re.search(
            r"\b(?:" + "|".join(
                sorted(map(re.escape, _TIPOS_EMBALAGEM_CANONICOS), key=len, reverse=True)
            ) + r")\b(?:\s*[./-]*\s*(?:COM|C|CONTENDO|DE))?\s*[./-]*\s*"
            r"(\d+(?:[.,]\d+)?)\s*(" + _UNIDADES_EMBALAGEM + r")\b",
            texto,
        )
        if achado:
            valor_conteudo = _decimal_medida(achado.group(1))
            unidade_conteudo = achado.group(2).upper()
            familia = UNIDADES_MEDIDA.get(unidade_conteudo)
            if valor_conteudo is not None and familia:
                unidade_token = {
                    "ML": "ML", "L": "L", "LT": "L", "LTS": "L", "CL": "CL",
                    "G": "G", "GR": "G", "GRS": "G", "KG": "KG", "KGS": "KG",
                    "MG": "MG",
                }.get(unidade_conteudo, unidade_conteudo)
                return (
                    f"{tipo or 'CX'}{_numero_token_embalagem(valor_conteudo)}"
                    f"{unidade_token}"
                )

    if quantidade is not None:
        return f"{tipo or 'UN'}{quantidade}"
    if tipo:
        return tipo

    if codigo_ucom in {"UN", "UND", "UNID", "UNIDADE", "UNIDADES"}:
        return "UN"
    familia = UNIDADES_MEDIDA.get(codigo_ucom)
    if familia:
        # Mesma unidade-base do backend (`_medida`): G/MG -> KG, ML/CL -> L,
        # CM/MM -> M. O token só precisa ser estável entre front e relatórios.
        if familia[0] == "MASSA":
            return "KG"
        if familia[0] == "VOLUME":
            return "L"
        if familia[0] == "COMPRIMENTO":
            return "M"
    return codigo_ucom or "UN"


def embalagem_para_chave(unidade_compra="", descricao=""):
    """Sufixo de embalagem da chave ("" para UN/KG/L...) — espelho do backend."""
    token = normalizar_embalagem(unidade_compra, descricao)
    if token in {"", "UN", "KG", "L", "M", "M2", "M3"}:
        return ""
    return token


def extrair_volume(descricao):
    """Extrai a medida de volume/peso em formato canônico (espelho do backend).

    "CHOPP 20L" -> "20L"; "1,5KG" -> "1.5KG"; "12X350ML" -> "4.2L". Medidas
    diferentes DEVEM gerar chaves diferentes, mesmo com o mesmo EAN.
    """
    texto = str(descricao if descricao is not None else "").upper()
    medidas = []
    for encontro in _VOLUME_RE.finditer(texto):
        quantidade = _decimal_medida(encontro.group("quantidade"))
        if quantidade is None:
            continue
        multiplicador = _decimal_medida(encontro.group("multiplicador"))
        if multiplicador is not None:
            quantidade *= multiplicador
        unidade = encontro.group("unidade").upper()

        if unidade.startswith("MILILITRO") or unidade in {"ML"}:
            mililitros = quantidade
            if mililitros >= 1000:
                medidas.append(f"{_formatar_medida(mililitros / 1000)}L")
            else:
                medidas.append(f"{_formatar_medida(mililitros)}ML")
        elif unidade.startswith("LITRO") or unidade in {"L", "LT", "LTS"}:
            mililitros = quantidade * 1000
            if mililitros >= 1000:
                medidas.append(f"{_formatar_medida(mililitros / 1000)}L")
            else:
                medidas.append(f"{_formatar_medida(mililitros)}ML")
        elif unidade.startswith("MILIGRAMA") or unidade in {"MG", "MGS"}:
            gramas = quantidade / 1000
            if gramas < 1:
                medidas.append(f"{_formatar_medida(gramas * 1000)}MG")
            else:
                medidas.append(f"{_formatar_medida(gramas)}G")
        elif unidade.startswith(("QUILOGRAMA", "QUILO")) or unidade in {"KG", "KGS"}:
            gramas = quantidade * 1000
            if gramas >= 1000:
                medidas.append(f"{_formatar_medida(gramas / 1000)}KG")
            else:
                medidas.append(f"{_formatar_medida(gramas)}G")
        elif unidade.startswith("GRAMA") or unidade in {"G", "GR", "GRS"}:
            gramas = quantidade
            if gramas >= 1000:
                medidas.append(f"{_formatar_medida(gramas / 1000)}KG")
            else:
                medidas.append(f"{_formatar_medida(gramas)}G")
    if not medidas:
        return None
    return "+".join(sorted(set(medidas)))


def _aplicar_volume_na_chave(chave, descricao):
    """Acrescenta/garante o sufixo _VOL_<medida> na chave do grupo."""
    volume = extrair_volume(descricao)
    if not volume:
        return str(chave or "")
    base = _VOLUME_SUFIXO_RE.sub("", str(chave or "").rstrip("_"))
    return f"{base}_VOL_{volume}"


def _aplicar_embalagem_na_chave(chave, unidade_compra="", descricao=""):
    """Acrescenta/garante o sufixo _EMB_<token> (CX24 x UN são produtos diferentes)."""
    token = embalagem_para_chave(unidade_compra, descricao)
    base = _EMB_SUFIXO_RE.sub("", str(chave or "").rstrip("_"))
    if not token:
        return base
    return f"{base}_EMB_{token}"


def _marca_para_chave(marca):
    """Token alfanumérico da marca; None quando ela não foi identificada."""
    if marca is None or str(marca).strip().upper() in {"NAN", "<NA>", "NONE", "NULL"}:
        return None
    texto = limpar_texto(marca)
    if not texto or texto in MARCAS_NAO_IDENTIFICADAS:
        return None
    token = re.sub(r"[^A-Z0-9]", "", texto)
    if not token or token in {"NAOIDENTIFICADA", "SEMARCA"}:
        return None
    return token


def _aplicar_marca_na_chave(chave, marca):
    """Acrescenta/garante o sufixo `_MARCA_<token>` na chave do grupo.

    Marca nula ou "NÃO IDENTIFICADA" não gera sufixo: o grupo continua sendo
    formado pelo nome base, como antes.
    """
    base = _MARCA_SUFIXO_RE.sub("", str(chave or "").rstrip("_"))
    token = _marca_para_chave(marca)
    if not token:
        return base
    return f"{base}_MARCA_{token}"


def _aplicar_contexto_na_chave(chave, descricao, marca=None, unidade_compra=""):
    """Reaplica marca + volume + embalagem no FINAL da chave, após as regras.

    Garante que regras antigas não fundam marcas (Arrico x El Chef), medidas
    (20L x 30L) nem embalagens (CX24 x UN): cada descrição volta para a chave
    do seu próprio produto.
    """
    base = str(chave or "").rstrip("_")
    base = _EMB_SUFIXO_RE.sub("", base).rstrip("_")
    base = _VOLUME_SUFIXO_RE.sub("", base).rstrip("_")
    base = _MARCA_SUFIXO_RE.sub("", base).rstrip("_")
    base = _aplicar_marca_na_chave(base, marca)
    base = _aplicar_volume_na_chave(base, descricao)
    return _aplicar_embalagem_na_chave(base, unidade_compra, descricao)


def _bases_legado_chave(chave):
    """Formas antigas da chave (sem marca, volume e/ou embalagem)."""
    bruta = str(chave or "").rstrip("_")
    sem_emb = _EMB_SUFIXO_RE.sub("", bruta).rstrip("_")
    sem_volume = _VOLUME_SUFIXO_RE.sub("", bruta).rstrip("_")
    sem_marca = _MARCA_SUFIXO_RE.sub("", bruta).rstrip("_")
    variantes = [bruta, sem_emb, sem_volume, sem_marca]
    for base in (sem_emb, sem_volume, sem_marca):
        sem_volume_emb = _VOLUME_SUFIXO_RE.sub("", base).rstrip("_")
        sem_marca_emb = _MARCA_SUFIXO_RE.sub("", base).rstrip("_")
        sem_ambos = _MARCA_SUFIXO_RE.sub("", sem_volume_emb).rstrip("_")
        variantes.extend([sem_volume_emb, sem_marca_emb, sem_ambos])
    bases = []
    for base in variantes:
        if base and base not in bases:
            bases.append(base)
    return bases


def tem_ean_valido(ean):
    """True quando o item tem código de barras real (não é vazio, '0' ou placeholder)."""
    if ean is None:
        return False
    try:
        if pd.isna(ean):
            return False
    except (TypeError, ValueError):
        pass
    return str(ean).strip().upper() not in EANS_INVALIDOS


def similaridade_textos(texto_a, texto_b):
    """Similaridade (0 a 1) por CARACTERES entre dois textos já normalizados (difflib)."""
    return difflib.SequenceMatcher(None, texto_a, texto_b, autojunk=False).ratio()


def tokens_texto(descricao):
    """Tokens da descrição sem acento/pontuação — a pontuação vira separador.

    Feito assim para não colar abreviações ("DET.LIQ.LIMPOL" -> DET, LIQ, LIMPOL), o que
    não acontece em limpar_texto() porque lá o texto vira chave de produto no banco.
    """
    nfkd = unicodedata.normalize("NFKD", str(descricao))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c)).upper()
    return re.sub(r"[^A-Z0-9]+", " ", sem_acento).split()


def tokens_principais(descricao):
    """Palavras principais (mais de 3 caracteres) do produto, sem ordem."""
    return {token for token in tokens_texto(descricao) if len(token) >= TAMANHO_MINIMO_TOKEN}


def qualificadores_descricao(descricao):
    """Conectores que invertem o produto: "com osso" x "sem osso", "com gelo" x "sem gelo"."""
    return {token for token in tokens_texto(descricao) if token in QUALIFICADORES_PRODUTO}


def qualificadores_compativeis(descricao_a, descricao_b):
    """COM/SEM não podem divergir quando os dois textos os citam (se só um cita, nada a concluir)."""
    qualificadores_a = qualificadores_descricao(descricao_a)
    qualificadores_b = qualificadores_descricao(descricao_b)
    return not (qualificadores_a and qualificadores_b and qualificadores_a != qualificadores_b)


def correspondencia_tokens(descricao_a, descricao_b):
    """Quanto das palavras principais do texto MENOR aparece no outro (0 a 1), sem ordem.

    Ex.: "LIMPOL NEUTRO" x "DETERGENTE NEUTRO LIMPOL" -> 1.0.
    Exige 2+ palavras em comum quando os dois textos têm 2+ palavras principais, para
    que uma palavra genérica ("NEUTRO") não esconda produtos diferentes.
    """
    tokens_a = tokens_principais(descricao_a)
    tokens_b = tokens_principais(descricao_b)
    if not tokens_a or not tokens_b:
        return 0.0
    em_comum = tokens_a & tokens_b
    if not em_comum:
        return 0.0
    menor = min(len(tokens_a), len(tokens_b))
    if menor >= 2 and len(em_comum) < 2:
        return 0.0
    return len(em_comum) / menor


def tokens_com_numeros(descricao):
    """Tokens preservando números ("1,7KG" -> "1,7", "KG") para ler medidas da descrição."""
    nfkd = unicodedata.normalize("NFKD", str(descricao))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c)).upper()
    return re.findall(r"[0-9]+(?:[.,][0-9]+)*|[A-Z]+", sem_acento)


def numero_do_token(token):
    """Converte o número do token no padrão brasileiro ("1,7" e "1.700,50" viram float)."""
    if "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(",", ".")
    if token.count(".") > 1:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def medidas_descricao(descricao):
    """Medidas físicas citadas na descrição, por família e convertidas para a base.

    Retorna {"MASSA": (gramas...), "VOLUME": (ml...), "COMPRIMENTO": (mm...)}.
    Entende multiplicador de embalagem ("10 X 1 KG" -> 10000 g) e ignora números sem
    unidade ("TP1", "C 74,2400") e contagens de embalagem (UN, CX, PCT, FD).
    """
    tokens = tokens_com_numeros(descricao)
    medidas = []
    for indice, token in enumerate(tokens):
        unidade = UNIDADES_MEDIDA.get(token)
        if not unidade or indice == 0:
            continue
        quantidade = numero_do_token(tokens[indice - 1])
        if quantidade is None:
            continue
        familia, fator = unidade
        multiplicador = None
        if indice >= 3 and tokens[indice - 2] == "X":
            multiplicador = numero_do_token(tokens[indice - 3])
        if (
            multiplicador is None
            and indice >= 2
            and token in UNIDADES_AMBIGUAS
            and tokens[indice - 2] in PALAVRAS_EMBALAGEM
        ):
            continue  # "PCT 12 LT" = 12 latas (contagem de embalagem, fora da trava)
        if multiplicador is not None:
            quantidade *= multiplicador
        medidas.append((familia, round(quantidade * fator, 3)))
    por_familia = {}
    for familia, valor in medidas:
        por_familia.setdefault(familia, []).append(valor)
    return {familia: tuple(sorted(valores)) for familia, valores in por_familia.items()}


def medidas_compativeis(descricao_a, descricao_b):
    """True quando as medidas citadas não se contradizem (trava de segurança).

    Só restringe quando as DUAS descrições citam medidas: "ARROZ 5KG" x "ARROZ 1KG" reprova,
    "ARROZ 5KG" x "ARROZ" aprova (falta informação de um lado, não há divergência).
    Compara apenas as famílias citadas nos DOIS textos: se uma descrição também informa uma
    capacidade que a outra omite ("SACO 200L ... 5KG" x "... 5KG") não há contradição.
    """
    medidas_a = medidas_descricao(descricao_a)
    medidas_b = medidas_descricao(descricao_b)
    if not medidas_a or not medidas_b:
        return True
    familias_comuns = set(medidas_a) & set(medidas_b)
    if not familias_comuns:
        return False  # famílias diferentes: peso x volume, peso x comprimento...
    for familia in familias_comuns:
        valores_a = medidas_a[familia]
        valores_b = medidas_b[familia]
        if len(valores_a) != len(valores_b):
            return False
        if any(abs(a - b) > TOLERANCIA_MEDIDA for a, b in zip(valores_a, valores_b)):
            return False
    return True


def travas_mesmo_produto(descricao_a, descricao_b):
    """Travas de segurança do veredito "mesmo produto" (só restringem, nunca liberam).

    1. Qualificadores COM/SEM não podem divergir quando os dois textos os citam:
       "com osso" x "sem osso" são produtos diferentes.
    2. Medidas físicas não podem divergir: "ARROZ 5KG" x "ARROZ 1KG" e "1 L" x "900 ML" são
       embalagens diferentes, então o grupo tem de aparecer na auditoria com o nome igual.
    """
    return qualificadores_compativeis(descricao_a, descricao_b) and medidas_compativeis(
        descricao_a, descricao_b
    )


def similaridade_descricoes(descricao_a, descricao_b):
    """Similaridade híbrida (0 a 1): caracteres (difflib) + palavras principais em comum.

    - Trava reprovada (COM x SEM, ou medidas diferentes como "5KG" x "1KG"): produtos
      diferentes, então a similaridade fica limitada a LIMIAR_TRAVA_CONFLITANTE mesmo que
      o texto seja parecido.
    - Palavras principais >= LIMIAR_TOKENS_MESMO_PRODUTO (80%) e trava aprovada: MESMO
      produto (1.0), mesmo com palavras fora de ordem ou abreviadas.
    - Caso contrário: o melhor entre o ratio de caracteres e a correspondência de palavras.
    """
    por_caracteres = similaridade_textos(limpar_texto(descricao_a), limpar_texto(descricao_b))
    por_palavras = correspondencia_tokens(descricao_a, descricao_b)
    if not travas_mesmo_produto(descricao_a, descricao_b):
        return min(max(por_caracteres, por_palavras), LIMIAR_TRAVA_CONFLITANTE)
    if por_palavras >= LIMIAR_TOKENS_MESMO_PRODUTO:
        return 1.0
    return max(por_caracteres, por_palavras)


def similaridade_com_padrao(descricao_padrao, descricoes):
    """Menor similaridade entre a descrição padrão do grupo e todas as suas descrições."""
    return min((similaridade_descricoes(descricao_padrao, d) for d in descricoes), default=1.0)


def similaridade_entre_descricoes(descricoes):
    """Menor similaridade entre TODOS os pares de descrições do grupo (rigor sem EAN)."""
    menor = 1.0
    for indice, descricao_a in enumerate(descricoes):
        for descricao_b in descricoes[indice + 1 :]:
            valor = similaridade_descricoes(descricao_a, descricao_b)
            if valor < menor:
                menor = valor
    return menor


def similaridade_auditoria_grupo(prod):
    """Similaridade que a Auditoria usa para decidir se o grupo é exceção.

    Com EAN válido: similaridade contra a descrição padrão. Sem EAN: o pior par entre
    as descrições (mesma métrica de `grupo_e_excecao`).
    """
    return prod["similaridade_padrao"] if prod["tem_ean"] else prod["similaridade_minima"]


def grupo_na_zona_cinzenta(prod):
    """True quando vale a pena pedir a segunda opinião da IA para este grupo.

    Exige 2+ descrições e similaridade no intervalo [50%, 90%): abaixo disso a
    divergência é evidente e acima a regra automática já considera o mesmo produto.
    """
    if len(prod.get("descricoes_unicas") or []) < 2:
        return False
    similaridade = similaridade_auditoria_grupo(prod)
    return LIMIAR_ZONA_CINZENTA_MIN <= similaridade < LIMIAR_ZONA_CINZENTA_MAX


def par_ambiguidade_grupo(prod):
    """Par de descrições que explica a zona cinzenta: `(texto1, texto2)`.

    Com EAN, o par é a descrição padrão contra a mais diferente dela. Sem EAN, é o
    par de menor similaridade entre todas as descrições — exatamente o par que a
    regra de ouro usou para mandar o grupo à auditoria.
    """
    descricoes = list(prod.get("descricoes_unicas") or [])
    padrao = str(prod.get("descricao_padrao") or (descricoes[0] if descricoes else ""))
    if len(descricoes) < 2:
        return padrao, padrao
    if prod.get("tem_ean"):
        candidatos = [d for d in descricoes if d != padrao]
        if not candidatos:
            return padrao, padrao
        return padrao, min(candidatos, key=lambda d: similaridade_descricoes(padrao, d))

    par, menor = (descricoes[0], descricoes[1]), 2.0
    for indice, descricao_a in enumerate(descricoes):
        for descricao_b in descricoes[indice + 1 :]:
            valor = similaridade_descricoes(descricao_a, descricao_b)
            if valor < menor:
                menor = valor
                par = (descricao_a, descricao_b)
    return par



def gerar_chave_produto(row):
    """Chave inteligente: PRODUTO + MARCA + VOLUME/PESO + EMBALAGEM.

    A marca vem do item (`itens_nota.marca`, inferida no backend). "POLENTA
    PALITO ARRICO" e "POLENTA PALITO EL CHEF" ficam em grupos diferentes mesmo
    com o mesmo EAN. Marca nula/"NÃO IDENTIFICADA" não entra na chave: o nome
    base agrupa. Números de controle ("- 1", "- 2") saem antes do nome base.
    A embalagem (CX24 x UN) é IDENTIDADE: não há conversão de quantidade/preço.
    """
    ean = row.get("ean", "SEM_EAN")
    descricao = row.get("descricao_produto", "")
    marca = row.get("marca") if hasattr(row, "get") else None
    unidade_compra = row.get("unidade_compra", "") if hasattr(row, "get") else ""
    descricao_base = _remover_numero_controle(descricao)
    desc = limpar_texto(descricao_base)
    encontrados = [t for t in TERMOS_EXCLUSIVOS if t in desc]
    subtipo_extra = "_".join(encontrados) if encontrados else ""
    if not tem_ean_valido(ean):
        palavras = [p for p in desc.split() if len(p) > 2]
        subtipo = "_".join(palavras[:2]) if palavras else "GERAL"
        chave = f"SEM_EAN_{subtipo}_{subtipo_extra}"
    else:
        chave = f"EAN_{str(ean).strip()}_{subtipo_extra}"
    chave = _aplicar_marca_na_chave(chave, marca)
    # Volume/peso e embalagem são sufixos obrigatórios: mesmo EAN com 20L e
    # 30L ou CX24 e UN -> chaves diferentes. `chave_final_produto` reaplica
    # depois das regras de-para.
    chave = _aplicar_volume_na_chave(chave, descricao_base)
    return _aplicar_embalagem_na_chave(chave, unidade_compra, descricao_base)


def resolver_chave(chave, mapa_regras):
    """Aplica as regras de agrupamento (de-para) em cadeia, com proteção contra ciclos.

    Regras antigas (gravadas antes das regras de marca/volume) não têm os sufixos
    `_MARCA_...`/`_VOL_...`; quando a chave nova não tem regra exata, tentamos as
    formas antigas para a correção continuar valendo.
    """
    atual = chave
    visitados = set()
    while atual not in visitados:
        # Registra o estado atual antes de aplicar a forma legada: uma regra
        # antiga (sem `_VOL_`/`_MARCA_`) pode apontar de volta para a chave nova
        # e, sem esta marcação, o laço alternaria entre as duas para sempre.
        visitados.add(atual)
        candidato = atual
        if candidato not in mapa_regras:
            for base in _bases_legado_chave(atual)[1:]:
                for alternativa in (base, f"{base}_"):
                    if alternativa in mapa_regras:
                        candidato = alternativa
                        break
                if candidato in mapa_regras:
                    break
        if candidato not in mapa_regras:
            return atual
        visitados.add(candidato)
        atual = mapa_regras[candidato]
    return atual


def chave_descricao(chave_produto, descricao):
    return f"DESC_{chave_produto}_{limpar_texto(descricao)}"


def chave_descricao_aprovada(chave_produto, descricao, itens_auditados):
    """True se a descrição foi aprovada na chave atual OU em uma chave legada.

    A inclusão do sufixo `_EMB_` (e das regras de marca/volume) mudou a chave de
    alguns grupos. Aprovações antigas continuam valendo para a MESMA descrição.
    """
    auditados = itens_auditados or set()
    return any(
        chave_descricao(base, descricao) in auditados
        for base in _bases_legado_chave(chave_produto)
    )


def chave_grupo_novo(descricao):
    """Chave inteligente exclusiva para isolar uma descrição em um grupo próprio.

    É determinística de propósito: repetir a ação com a mesma descrição reaproveita a mesma
    chave, então o card novo não duplica entre reruns. O sufixo com hash curto evita colisão
    entre descrições que ficam parecidas depois da normalização (acentos/pontuação/corte).
    """
    texto = str(descricao or "").strip()
    base = limpar_texto(texto)[:60].strip() or "PRODUTO"
    resumo = hashlib.sha1(texto.encode("utf-8")).hexdigest()[:6].upper()
    return f"{PREFIXO_GRUPO_NOVO}{base.replace(' ', '_')}_{resumo}"


def chave_final_produto(
    chave_base, descricao, mapa_regras, mapa_descricoes, marca=None, unidade_compra=""
):
    """Chave do grupo considerando regras de grupo (juntar) e de descrição (mover).

    As regras de descrição são aplicadas em cadeia: mover a descrição para um grupo novo
    (NOVO_...) e depois dali para outro grupo continua funcionando. A proteção por
    `visitados` evita laço quando uma regra aponta de volta para um grupo já percorrido.

    Marca, volume/peso e embalagem são reaplicados no FINAL, depois das regras:
    mesmo que uma regra "juntar" aponte Arrico para o grupo do El Chef (ou 20L
    para o do 30L, ou CX24 para UN), cada descrição volta para a chave do seu
    próprio produto/medida/embalagem.
    """
    chave = resolver_chave(chave_base, mapa_regras)
    visitados = set()
    while True:
        candidatas = []
        # Compatibilidade com regras antigas gravadas sem marca e/ou volume.
        for base in _bases_legado_chave(chave):
            for base_regra in (base, f"{base}_"):
                candidata = chave_descricao(base_regra, descricao)
                if candidata not in candidatas:
                    candidatas.append(candidata)
        aplicada = None
        for chave_desc in candidatas:
            if chave_desc in mapa_descricoes and chave_desc not in visitados:
                aplicada = chave_desc
                break
        if aplicada:
            visitados.add(aplicada)
            chave = resolver_chave(mapa_descricoes[aplicada], mapa_regras)
            continue
        return _aplicar_contexto_na_chave(chave, descricao, marca, unidade_compra)


def montar_grupos(df, mapa_regras, mapa_descricoes, itens_auditados):
    """Agrupa os itens pela chave inteligente já com as regras de grupo e de descrição aplicadas."""
    df_grupos = df.copy()
    df_grupos["chave_base"] = df_grupos.apply(gerar_chave_produto, axis=1)
    marcas = (
        df_grupos["marca"].tolist()
        if "marca" in df_grupos.columns
        else [None] * len(df_grupos)
    )
    unidades_compra = (
        df_grupos["unidade_compra"].fillna("").tolist()
        if "unidade_compra" in df_grupos.columns
        else [""] * len(df_grupos)
    )
    df_grupos["chave_inteligente"] = [
        chave_final_produto(
            base, descricao, mapa_regras, mapa_descricoes, marca, unidade
        )
        for base, descricao, marca, unidade in zip(
            df_grupos["chave_base"],
            df_grupos["descricao_produto"],
            marcas,
            unidades_compra,
        )
    ]

    grupos = []
    for chave, grupo in df_grupos.groupby("chave_inteligente"):
        descricoes = grupo["descricao_produto"].unique().tolist()
        mais_frequente = grupo["descricao_produto"].value_counts().index[0]
        # Padrão de comparação = descrição MAIS ANTIGA do grupo (primeira data de emissão).
        por_data = grupo.sort_values("data_emissao", na_position="last", kind="stable")
        descricao_padrao = por_data["descricao_produto"].iloc[0]
        tem_ean = tem_ean_valido(grupo["ean"].iloc[0])
        similaridade_padrao = similaridade_com_padrao(descricao_padrao, descricoes)
        grupos.append(
            {
                "chave": chave,
                "grupo": grupo,
                "nome_padrao": mais_frequente,
                "descricao_padrao": descricao_padrao,
                "descricoes_unicas": descricoes,
                "ean": grupo["ean"].iloc[0],
                "ncm": grupo["ncm"].iloc[0],
                "capitulo": grupo["capitulo_ncm"].iloc[0],
                "preco_medio": grupo["valor_unitario"].mean(),
                "registros": len(grupo),
                "tem_ean": tem_ean,
                # Com EAN a comparação é contra o padrão; sem EAN, par a par (rigor máximo).
                "similaridade_padrao": similaridade_padrao,
                "similaridade_minima": (
                    similaridade_padrao
                    if tem_ean or len(descricoes) < 2
                    else similaridade_entre_descricoes(descricoes)
                ),
            }
        )
    grupos.sort(key=lambda g: g["nome_padrao"].upper())
    for grupo in grupos:
        grupo["descricoes_pendentes"] = [
            d
            for d in grupo["descricoes_unicas"]
            if not chave_descricao_aprovada(grupo["chave"], d, itens_auditados)
        ]
    return grupos


def grupo_e_excecao(grupo):
    """Regra de ouro: só vai para a auditoria o que realmente foge do padrão.

    - COM EAN válido (o código de barras dá a confiança): descrição única não tem o que
      auditar; com 2+, tolera variações de escrita até LIMIAR_SIMILARIDADE_EAN (70%) de
      similaridade com a descrição padrão (a mais antiga do grupo). Abaixo disso é
      indício de erro grave do fornecedor (ex.: "LEITE" cadastrado como "MANTEIGA").
    - SEM EAN (nenhuma âncora de código): rigor no texto — exige 2+ descrições e só exibe
      quando algum par delas fica abaixo de LIMIAR_SIMILARIDADE_SEM_EAN (85%), para que
      "MANDIOCA CREMOSA" e "MANDIOCA TOLETE" não passem batido.
    """
    descricoes = grupo["descricoes_unicas"]
    # Grupo criado à mão em "➕ Criar novo grupo com esta descrição": o auditor abriu este card
    # de propósito para isolar uma descrição, então ele aparece na auditoria mesmo com 1 só
    # descrição (a regra de ouro abaixo esconderia um grupo de descrição única).
    if str(grupo.get("chave") or "").startswith(PREFIXO_GRUPO_NOVO):
        return True
    if len(descricoes) < 2:
        return False
    if grupo["tem_ean"]:
        return grupo["similaridade_padrao"] < LIMIAR_SIMILARIDADE_EAN
    return grupo["similaridade_minima"] < LIMIAR_SIMILARIDADE_SEM_EAN


def tokens_nome_produto(descricao):
    """Palavras que identificam o produto, sem números/medidas ("10X1KG", "900ML", "300G").

    Anos/safras (4 dígitos entre 1900 e 2100) continuam valendo: "MALBEC 2023" e
    "MALBEC 2024" são produtos diferentes.
    """
    tokens = set()
    for token in tokens_principais(descricao):
        if token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2100:
            tokens.add(token)
        elif not any(caractere.isdigit() for caractere in token):
            tokens.add(token)
    return tokens


def duplicidade_entre_descricoes(descricao_a, descricao_b):
    """(é duplicidade?, similaridade) entre duas descrições de grupos DIFERENTES.

    Critérios (todos exigem as travas aprovadas — COM x SEM e medidas divergentes ficam
    de fora, então "5KG x 1KG" ou "com osso x sem osso" nunca viram duplicidade):
    - texto normalizado idêntico (a mesma descrição em dois grupos);
    - mesmas palavras do NOME do produto (números/medidas descartados), em qualquer
      ordem: "ACUCAR UNIAO REFINADO 10X1KG" x "ACUCAR REFINADO UNIAO";
    - similaridade de caracteres >= LIMIAR_DUPLICIDADE (95%), para erros de digitação.
    Não usa a métrica híbrida de propósito: ela trata como 100% textos que só têm as
    palavras em comum ("AGUA MINERAL C/GAS" x "AGUA MINERAL NATURAL"), o que geraria
    alertas falsos em quase todo o catálogo.
    """
    if not travas_mesmo_produto(descricao_a, descricao_b):
        return False, 0.0
    normalizada_a = limpar_texto(descricao_a)
    normalizada_b = limpar_texto(descricao_b)
    if normalizada_a and normalizada_a == normalizada_b:
        return True, 1.0
    tokens_a = tokens_nome_produto(descricao_a)
    if tokens_a and tokens_a == tokens_nome_produto(descricao_b):
        return True, 1.0
    similaridade = similaridade_textos(normalizada_a, normalizada_b)
    return similaridade >= LIMIAR_DUPLICIDADE, similaridade


def _preparar_descricoes_grupo(grupo):
    """(descrição original, normalizada, palavras do nome) de cada descrição do grupo."""
    preparadas = []
    for descricao in grupo["descricoes_unicas"]:
        preparadas.append((descricao, limpar_texto(descricao), tokens_nome_produto(descricao)))
    return preparadas


def _comparar_descricoes_grupos(preparadas_a, preparadas_b):
    """Melhor par entre as descrições de dois grupos: (idêntica?, similaridade, par).

    Só calcula a similaridade de caracteres quando as duas descrições têm alguma palavra
    do nome em comum — sem isso é impossível chegar a LIMIAR_DUPLICIDADE e a comparação
    fica barata.
    """
    for descricao, normalizada, _ in preparadas_a:
        for descricao_outra, normalizada_outra, _ in preparadas_b:
            if normalizada and normalizada == normalizada_outra:
                return True, 1.0, (descricao, descricao_outra)

    melhor, par = 0.0, None
    for descricao, normalizada, palavras in preparadas_a:
        for descricao_outra, normalizada_outra, palavras_outra in preparadas_b:
            if not (palavras & palavras_outra):
                continue
            if not travas_mesmo_produto(descricao, descricao_outra):
                continue
            if palavras == palavras_outra:
                return False, 1.0, (descricao, descricao_outra)
            valor = similaridade_textos(normalizada, normalizada_outra)
            if valor > melhor:
                melhor, par = valor, (descricao, descricao_outra)
    return False, melhor, par


def escolher_direcao_duplicidade(prod, outro):
    """(chave_origem, grupo_destino) do 1 clique que junta dois grupos duplicados.

    `prod` é o grupo do card (dict de montar_grupos) e `outro` o grupo do alerta.
    Fica como grupo principal quem tem EAN (âncora confiável de produto) e, entre iguais,
    o grupo com mais registros; o outro grupo é fundido nele.
    """
    outro_e_melhor = (bool(outro["tem_ean"]), int(outro["registros"])) > (
        bool(prod["tem_ean"]),
        int(prod["registros"]),
    )
    if outro_e_melhor:
        return prod["chave"], outro
    return outro["chave"], prod


def detectar_grupos_duplicados(grupos):
    """Alerta de duplicidade: grupos distintos que representam o MESMO produto.

    Compara as descrições de todos os grupos entre si e sinaliza os pares com a mesma
    descrição (ex.: "ARROZ TIPO 1 5KG" nos dois grupos, porque um tem EAN e o outro não),
    com o mesmo nome de produto (números/medidas/ordem ignorados) ou com similaridade de
    caracteres >= LIMIAR_DUPLICIDADE (95%) — ver `duplicidade_entre_descricoes`. Descrições
    com medidas/qualificadores conflitantes (5KG x 1KG, COM x SEM) NÃO são duplicidade.

    Usa um índice invertido (descrição normalizada + palavras do nome) para comparar
    apenas candidatos, mantendo a detecção rápida mesmo com centenas de grupos.
    Retorna {chave_do_grupo: [sugestões ordenadas]} só para os grupos com duplicidade.
    """
    preparadas = [_preparar_descricoes_grupo(grupo) for grupo in grupos]
    indice_descricao, indice_palavra = {}, {}
    for pos, itens in enumerate(preparadas):
        for _, normalizada, palavras in itens:
            if normalizada:
                indice_descricao.setdefault(normalizada, set()).add(pos)
            for palavra in palavras:
                indice_palavra.setdefault(palavra, set()).add(pos)

    duplicados = {}
    for pos, grupo in enumerate(grupos):
        candidatos = set()
        for _, normalizada, palavras in preparadas[pos]:
            if normalizada:
                candidatos |= indice_descricao.get(normalizada, set())
            for palavra in palavras:
                candidatos |= indice_palavra.get(palavra, set())
        candidatos.discard(pos)

        sugestoes = []
        for pos_outro in candidatos:
            identica, similaridade, par = _comparar_descricoes_grupos(preparadas[pos], preparadas[pos_outro])
            if not identica and similaridade < LIMIAR_DUPLICIDADE:
                continue
            sugestoes.append(
                {
                    "grupo": grupos[pos_outro],
                    "identica": identica,
                    "similaridade": similaridade,
                    "descricao_origem": par[0],
                    "descricao_destino": par[1],
                }
            )
        if sugestoes:
            sugestoes.sort(
                key=lambda s: (not s["identica"], -s["similaridade"], s["grupo"]["nome_padrao"].upper())
            )
            duplicados[grupo["chave"]] = sugestoes[:MAXIMO_SUGESTOES_DUPLICIDADE]
    return duplicados


def descrever_descricao_da_regra(chave_regra, descricoes_conhecidas):
    """Tenta recuperar a descrição legível de uma chave DESC_... gravada como regra de mover."""
    if not chave_regra.startswith("DESC_"):
        return None
    for descricao in sorted(descricoes_conhecidas, key=len, reverse=True):
        if chave_regra.endswith("_" + limpar_texto(descricao)):
            return descricao
    return None


def salvar_agrupamento(chave_origem, destino, tipo, descricoes_movidas=()):
    """Persiste a regra de-para.

    - 'juntar': chave_origem é o grupo inteiro que será fundido no destino.
    - 'mover': chave_origem é a chave DESC_... de UMA descrição, que já é aprovada no destino.
    """
    try:
        resp = requests.post(
            REGRAS_URL,
            json={"chave_origem": chave_origem, "chave_destino": destino["chave"], "tipo": tipo},
            headers=cabecalhos_api(),
            timeout=30,
        )
    except Exception as e:
        return False, f"Erro de conexão com o backend: {e}"

    if resp.status_code != 200:
        try:
            detalhe = resp.json().get("detail", resp.text)
        except Exception:
            detalhe = resp.text
        return False, f"Erro ao salvar agrupamento ({resp.status_code}): {detalhe}"

    if tipo == "mover":
        for descricao in descricoes_movidas:
            try:
                requests.post(
                    AUDIT_URL,
                    json={"chave_identificacao": chave_descricao(destino["chave"], descricao)},
                    headers=cabecalhos_api(),
                    timeout=30,
                )
            except Exception:
                pass

    return True, "Agrupamento salvo com sucesso."


# ──────────────────────────────────────────────────────────────────────────────
# Componentes da auditoria
# ──────────────────────────────────────────────────────────────────────────────
def renderizar_seletor_agrupamento(acao, prod, p_idx, todos_grupos, chave_estado):
    """Mostra os seletores e grava a regra de agrupamento.

    - 'juntar': funde o grupo inteiro no destino escolhido.
    - 'mover': move UMA descrição; o destino pode ser outro grupo ou a opção
      OPCAO_CRIAR_GRUPO, que cria na hora uma chave NOVO_... só para essa descrição.
    """
    chave_produto = prod["chave"]
    descricoes = prod["descricoes_unicas"]

    if acao == "juntar":
        st.info(
            "🔗 Escolha o grupo que será **unido** a este produto. As descrições dos dois passam a ser "
            "variações do mesmo produto e as novas variações continuam pendentes de auditoria."
        )
        descricao_mover = None
    else:
        st.info(
            "➡️ Selecione **uma descrição** deste grupo e o grupo de destino. Apenas a descrição escolhida "
            "será movida (e já aprovada no destino); as outras permanecem neste grupo. Se o grupo correto "
            f"ainda não existe, escolha **{OPCAO_CRIAR_GRUPO}** para isolar a descrição em um card novo."
        )
        descricao_mover = st.selectbox(
            "Descrição que será movida:",
            sorted(descricoes),
            index=None,
            placeholder="Selecione a descrição exata...",
            key=f"sel_desc_mover_{p_idx}_{chave_produto}",
        )

    opcoes = {}
    for outro in todos_grupos:
        if outro["chave"] == chave_produto:
            continue
        rotulo = f"{outro['nome_padrao']} | EAN {outro['ean']} | Cap. {outro['capitulo']}"
        opcoes.setdefault(rotulo, outro)

    # Criar grupo novo só faz sentido ao mover UMA descrição (é ela que vira o grupo).
    pode_criar_grupo = acao == "mover"
    if not opcoes and not pode_criar_grupo:
        st.warning("Não há outro grupo disponível para agrupar.")
        if st.button("Fechar", key=f"btn_fechar_{p_idx}_{chave_produto}"):
            st.session_state.pop(chave_estado, None)
            st.rerun()
        return

    rotulos_destino = ([OPCAO_CRIAR_GRUPO] if pode_criar_grupo else []) + list(opcoes.keys())
    escolha = st.selectbox(
        "Grupo de destino:",
        rotulos_destino,
        index=None,
        placeholder="Digite a descrição ou selecione outro produto...",
        key=f"sel_{acao}_{p_idx}_{chave_produto}",
    )
    criar_grupo_novo = pode_criar_grupo and escolha == OPCAO_CRIAR_GRUPO
    if criar_grupo_novo and descricao_mover:
        st.caption(
            "🆕 Será criado o grupo "
            f"`{_aplicar_volume_na_chave(chave_grupo_novo(descricao_mover), descricao_mover)}` "
            f"só com a descrição `{descricao_mover}`. Ela sai deste grupo e o novo card "
            "aparece na auditoria para revisão/aprovação."
        )
    elif criar_grupo_novo:
        st.caption(
            "🆕 A descrição escolhida acima será isolada em um grupo próprio, criado na hora "
            "(use quando o grupo correto ainda não existe)."
        )

    falta_descricao = acao == "mover" and descricao_mover is None
    col_confirmar, col_cancelar = st.columns(2)
    with col_confirmar:
        confirmar = st.button(
            "✅ Confirmar agrupamento",
            key=f"btn_conf_{acao}_{p_idx}_{chave_produto}",
            type="primary",
            width="stretch",
            disabled=escolha is None or falta_descricao,
        )
    with col_cancelar:
        cancelar = st.button("Cancelar", key=f"btn_cancel_{acao}_{p_idx}_{chave_produto}", width="stretch")

    if falta_descricao:
        st.caption("👆 Escolha a descrição que será movida para habilitar a confirmação.")

    if cancelar:
        st.session_state.pop(chave_estado, None)
        st.rerun()

    if confirmar and escolha and not falta_descricao:
        if acao == "juntar":
            destino, chave_origem, movidas = opcoes[escolha], chave_produto, []
        elif criar_grupo_novo:
            # Grupo novo: a descrição sai do grupo atual e passa a ser um grupo próprio.
            # Não aprova automaticamente — o card novo nasce com a descrição pendente para
            # o auditor revisar (é assim que ele aparece na lista padrão da auditoria).
            destino = {"chave": chave_grupo_novo(descricao_mover)}
            chave_origem, movidas = chave_descricao(chave_produto, descricao_mover), []
        else:
            destino = opcoes[escolha]
            chave_origem, movidas = chave_descricao(chave_produto, descricao_mover), [descricao_mover]
        sucesso, mensagem = salvar_agrupamento(chave_origem, destino, acao, movidas)
        if sucesso:
            st.session_state.pop(chave_estado, None)
            st.rerun()
            return
        st.error(mensagem)
        return


@st.dialog("📄 DANFE — Nota Fiscal Original", width="large")
def mostrar_nota_fiscal(item_id):
    """Modal com a visualização do DANFE: emitente, destinatário, nota, item e totais."""
    dados, erro = carregar_nota(item_id)
    if erro:
        st.error(erro)
        if st.button("Fechar", width="stretch"):
            st.rerun()
        return

    nota = dados.get("nota") or {}
    item = dados.get("item") or {}
    totais = dados.get("totais") or {}
    emitente = dados.get("emitente") or {}
    destinatario = dados.get("destinatario") or {}

    st.caption(
        f"Compra registrada em {formatar_data(nota.get('data_emissao'))} | "
        f"Loja: {item.get('loja') or '—'} | Item: {item.get('descricao') or '—'}"
    )
    if not dados.get("xml_disponivel"):
        st.warning(dados.get("aviso") or "XML original indisponível: exibindo os dados do banco.")

    col_emitente, col_destinatario = st.columns(2)
    with col_emitente:
        st.markdown("##### 🏭 Emitente (Fornecedor)")
        st.markdown(f"**{emitente.get('razao_social') or '—'}**")
        st.markdown(f"CNPJ: `{formatar_cnpj(emitente.get('cnpj'))}`")
        if emitente.get("nome_fantasia"):
            st.markdown(f"Fantasia: {emitente['nome_fantasia']}")
        if emitente.get("inscricao_estadual"):
            st.markdown(f"Inscrição Estadual: {emitente['inscricao_estadual']}")
        st.caption(emitente.get("endereco") or "Endereço não informado no XML")
    with col_destinatario:
        st.markdown("##### 🏪 Destinatário (Loja)")
        st.markdown(f"**{destinatario.get('razao_social') or '—'}**")
        st.markdown(f"CNPJ: `{formatar_cnpj(destinatario.get('cnpj'))}`")
        if destinatario.get("nome_fantasia"):
            st.markdown(f"Fantasia: {destinatario['nome_fantasia']}")
        if destinatario.get("inscricao_estadual"):
            st.markdown(f"Inscrição Estadual: {destinatario['inscricao_estadual']}")
        st.caption(destinatario.get("endereco") or "Endereço não informado no XML")

    st.divider()
    emissao = nota.get("data_hora_emissao") or nota.get("data_emissao")
    formato_emissao = "%d/%m/%Y %H:%M" if nota.get("data_hora_emissao") else "%d/%m/%Y"
    coluna_nota, coluna_emissao, coluna_valor = st.columns(3)
    coluna_nota.metric("Nota / Série", f"{nota.get('numero') or '—'} / {nota.get('serie') or '—'}")
    coluna_emissao.metric("Data de Emissão", formatar_data(emissao, formato_emissao))
    coluna_valor.metric("Valor Total", formatar_moeda(totais.get("total") or nota.get("valor_total")))

    st.markdown(f"**Chave de acesso:** `{formatar_chave_acesso(nota.get('chave_acesso'))}`")
    if nota.get("natureza_operacao"):
        st.markdown(f"**Natureza da operação:** {nota['natureza_operacao']}")
    if nota.get("protocolo"):
        autorizacao = formatar_data(nota.get("autorizada_em"), "%d/%m/%Y %H:%M") if nota.get("autorizada_em") else ""
        st.markdown(
            f"**Protocolo de autorização:** `{nota['protocolo']}`" + (f" — {autorizacao}" if autorizacao else "")
        )

    st.divider()
    st.markdown("##### 🧾 Item desta compra")
    st.markdown(f"**{item.get('descricao') or '—'}**")
    col_qtd, col_unit, col_total, col_ncm = st.columns(4)
    col_qtd.metric("Quantidade", formatar_quantidade(item.get("quantidade")))
    col_unit.metric("Valor Unitário", formatar_moeda(item.get("valor_unitario")))
    col_total.metric("Valor do Item", formatar_moeda(item.get("valor_total")))
    col_ncm.metric("NCM", item.get("ncm") or "—")
    st.caption(f"EAN: `{item.get('ean') or '—'}` | Fornecedor: {item.get('fornecedor') or '—'}")

    st.markdown("##### 💰 Totais da nota")
    col_prod, col_desc, col_frete, col_nota_total = st.columns(4)
    col_prod.metric("Produtos", formatar_moeda(totais.get("produtos")))
    col_desc.metric("Desconto", formatar_moeda(totais.get("desconto")))
    col_frete.metric("Frete", formatar_moeda(totais.get("frete")))
    col_nota_total.metric("Total da Nota", formatar_moeda(totais.get("total") or nota.get("valor_total")))

    st.divider()
    if st.button("Fechar", width="stretch"):
        st.rerun()


def _variacao_entre_precos(preco_anterior, preco_atual):
    """Variação percentual entre duas compras (None quando não dá para comparar)."""
    try:
        anterior = float(preco_anterior)
        atual = float(preco_atual)
    except (TypeError, ValueError):
        return None
    if pd.isna(anterior) or pd.isna(atual) or anterior <= 0 or atual <= 0:
        return None
    return (atual / anterior - 1) * 100


def _variacao_e_brusca(preco_anterior, preco_atual):
    """True quando a variação atinge o mesmo limiar de divergência do backend."""
    variacao = _variacao_entre_precos(preco_anterior, preco_atual)
    return variacao is not None and abs(variacao) >= LIMIAR_VARIACAO_BRUSCA


def _ultimas_compras_por_descricao(grupo):
    """Últimas DUAS compras de cada descrição do grupo.

    A Auditoria exibe, ao lado da descrição original, o preço da penúltima e o da
    última compra; cada preço tem seu próprio "Ver DANFE" (endpoint
    ``/api/nota/{item_id}``). Quando há variação de 50% ou mais entre eles, os dois
    preços são marcados em vermelho.
    """
    if grupo is None or getattr(grupo, "empty", True):
        return {}
    if not {"descricao_produto", "valor_unitario"}.issubset(grupo.columns):
        return {}
    dados = grupo.copy()
    if "data_emissao" in dados.columns:
        dados["_data_ordem"] = pd.to_datetime(
            dados["data_emissao"], errors="coerce"
        )
    else:
        dados["_data_ordem"] = pd.NaT
    ordem = ["_data_ordem", "id"] if "id" in dados.columns else ["_data_ordem"]
    dados = dados.sort_values(ordem, na_position="first", kind="stable")
    ultimas = {}
    for descricao, compras in dados.groupby("descricao_produto", sort=False):
        ultima = compras.iloc[-1]
        anterior = compras.iloc[-2] if len(compras) >= 2 else None
        preco_ultima = ultima.get("valor_unitario")
        preco_anterior = anterior.get("valor_unitario") if anterior is not None else None
        info = {
            "preco": preco_ultima,
            "preco_ultima": preco_ultima,
            "data": ultima.get("data_emissao"),
            "data_ultima": ultima.get("data_emissao"),
            "fornecedor": ultima.get("fornecedor") or "Fornecedor não informado",
            "item_id": ultima.get("id"),
            "item_id_ultima": ultima.get("id"),
            "preco_anterior": preco_anterior,
            "variacao_pct": _variacao_entre_precos(preco_anterior, preco_ultima),
        }
        if anterior is not None:
            info.update(
                {
                    "data_anterior": anterior.get("data_emissao"),
                    "fornecedor_anterior": anterior.get("fornecedor")
                    or "Fornecedor não informado",
                    "item_id_anterior": anterior.get("id"),
                }
            )
        ultimas[str(descricao)] = info
    return ultimas


def renderizar_produto(
    prod,
    p_idx,
    todos_grupos,
    itens_auditados,
    mostrar_todos,
    regras_agrupamento,
    descricoes_conhecidas,
    duplicidades,
    aberracoes_pendentes=None,
    descricoes_aberrantes=None,
):
    grupo = prod["grupo"]
    chave_produto = prod["chave"]
    descricoes = prod["descricoes_unicas"]
    aberracoes_pendentes = aberracoes_pendentes or set()
    descricoes_aberrantes = descricoes_aberrantes or set()
    ultimas_compras = _ultimas_compras_por_descricao(grupo)

    with st.expander(
        f"📦 {prod['nome_padrao']} | Preço Médio: {formatar_moeda(prod['preco_medio'])} | "
        f"{len(descricoes)} descrição(ões)"
    ):
        # Alerta de duplicidade: outro grupo com a mesma descrição (ou similaridade >= 95%).
        for sugestao in duplicidades.get(chave_produto, []):
            outro = sugestao["grupo"]
            if sugestao["identica"]:
                detalhe = (
                    f"a descrição `{sugestao['descricao_origem']}` aparece nos **dois** grupos"
                )
            elif sugestao["similaridade"] >= 1.0:
                detalhe = (
                    "as descrições têm o **mesmo nome de produto**, mudando só a escrita/ordem: "
                    f"`{sugestao['descricao_origem']}` x `{sugestao['descricao_destino']}`"
                )
            else:
                detalhe = (
                    f"similaridade de **{sugestao['similaridade'] * 100:.0f}%** entre "
                    f"`{sugestao['descricao_origem']}` e `{sugestao['descricao_destino']}`"
                )
            st.warning(
                f"⚠️ **Possível duplicidade:** este grupo repete o produto do grupo "
                f"**{outro['nome_padrao']}** (EAN `{outro['ean']}`, "
                f"{formatar_numero(outro['registros'])} registros) — {detalhe}. "
                "Junte os dois grupos para consolidar o histórico de compras."
            )
            # Direção do 1 clique: fica como principal quem tem EAN (âncora confiável) e,
            # entre iguais, o grupo de maior histórico.
            origem_duplicado, destino_duplicado = escolher_direcao_duplicidade(prod, outro)
            if destino_duplicado is outro:
                rotulo_botao = f"🔗 Juntar com o grupo {outro['nome_padrao'][:45]}"
                ajuda_botao = "Este grupo é unido ao grupo do alerta, que tem EAN/histórico mais forte."
            else:
                rotulo_botao = f"🔗 Trazer o grupo {outro['nome_padrao'][:45]} para este"
                ajuda_botao = "O grupo do alerta é unido a este, que tem EAN/histórico mais forte."
            if st.button(
                rotulo_botao,
                key=f"btn_duplicado_{p_idx}_{chave_produto}_{outro['chave']}",
                type="primary",
                width="stretch",
                help=ajuda_botao,
            ):
                sucesso, mensagem = salvar_agrupamento(origem_duplicado, destino_duplicado, "juntar")
                if sucesso:
                    st.rerun()
                st.error(mensagem)

        st.markdown(
            f"**EAN:** `{prod['ean']}` | **NCM:** `{prod['ncm']}` | "
            f"**Registros:** {formatar_numero(prod['registros'])}"
        )

        if len(descricoes) > 1:
            if prod["tem_ean"]:
                st.caption(
                    f"🧭 Padrão (descrição mais antiga): `{prod['descricao_padrao']}` — similaridade "
                    f"mínima das demais: **{prod['similaridade_padrao'] * 100:.0f}%** "
                    f"(exibe abaixo de {LIMIAR_SIMILARIDADE_EAN * 100:.0f}%, pois o EAN válido dá "
                    "confiança de que é o mesmo produto)."
                )
            else:
                st.caption(
                    "🧭 Sem EAN: similaridade mínima **entre as descrições** do grupo: "
                    f"**{prod['similaridade_minima'] * 100:.0f}%** "
                    f"(exibe abaixo de {LIMIAR_SIMILARIDADE_SEM_EAN * 100:.0f}% — rigor no texto)."
                )

        if grupo_na_zona_cinzenta(prod):
            similaridade_zona = similaridade_auditoria_grupo(prod)
            texto_ia_a, texto_ia_b = par_ambiguidade_grupo(prod)
            chave_estado_ia = f"{PREFIXO_IA_AMBIGUIDADE}{chave_produto}"
            with st.container(border=True):
                st.markdown("#### 🤖 Zona cinzenta — segunda opinião da IA")
                st.caption(
                    f"Similaridade de **{similaridade_zona * 100:.0f}%**: não é óbvio se as "
                    "descrições são o mesmo produto. O DeepSeek compara o par mais divergente "
                    "e devolve um veredito para o auditor conferir."
                )
                st.markdown(f"- `{texto_ia_a}`\n- `{texto_ia_b}`")
                if st.button(
                    "🤖 Consultar IA",
                    key=f"btn_ia_ambiguidade_{p_idx}_{chave_produto}",
                    width="stretch",
                    help="Segunda opinião do DeepSeek: produtos iguais ou diferentes?",
                ):
                    with st.spinner("Consultando o DeepSeek..."):
                        resultado_ia, erro_ia = consultar_ia_ambiguidade(texto_ia_a, texto_ia_b)
                    st.session_state[chave_estado_ia] = {
                        "resultado": resultado_ia,
                        "erro": erro_ia,
                        "par": (texto_ia_a, texto_ia_b),
                    }

                estado_ia = st.session_state.get(chave_estado_ia)
                if estado_ia:
                    resultado_ia = estado_ia.get("resultado")
                    erro_ia = estado_ia.get("erro")
                    if erro_ia:
                        st.error(erro_ia)
                    elif resultado_ia:
                        par_analisado = estado_ia.get("par") or (texto_ia_a, texto_ia_b)
                        try:
                            confianca_ia = float(resultado_ia.get("confianca") or 0)
                        except (TypeError, ValueError):
                            confianca_ia = 0.0
                        if resultado_ia.get("mesmo_produto"):
                            st.success(
                                "🤖 **IA: mesmo produto** "
                                f"({confianca_ia * 100:.0f}% de confiança). "
                                "Se concordar, junte/mantenha as descrições no grupo."
                            )
                        else:
                            st.warning(
                                "🤖 **IA: produtos diferentes** "
                                f"({confianca_ia * 100:.0f}% de confiança). "
                                "Se concordar, mova a descrição para o grupo correto."
                            )
                        st.caption(
                            f"Par analisado: `{par_analisado[0]}` x `{par_analisado[1]}`"
                        )
                        motivo_ia = str(resultado_ia.get("motivo") or "").strip()
                        if motivo_ia:
                            st.markdown(f"**Motivo:** {motivo_ia}")
                        diferencas_ia = [
                            str(item).strip()
                            for item in (resultado_ia.get("diferencas") or [])
                            if str(item).strip()
                        ]
                        if diferencas_ia:
                            st.markdown(
                                "**Diferenças apontadas:** " + "; ".join(diferencas_ia)
                            )
                        st.caption(
                            f"Modelo: `{resultado_ia.get('modelo') or 'deepseek'}`. "
                            "A IA é só uma segunda opinião: a decisão final continua sua."
                        )


        regras_do_produto = [r for r in regras_agrupamento if r.get("chave_destino") == chave_produto]
        grupos_origem = [r["chave_origem"] for r in regras_do_produto if not r["chave_origem"].startswith("DESC_")]
        descricoes_origem = [
            descrever_descricao_da_regra(r["chave_origem"], descricoes_conhecidas) or r["chave_origem"]
            for r in regras_do_produto
            if r["chave_origem"].startswith("DESC_")
        ]
        if grupos_origem:
            st.caption("🔗 Grupos agrupados neste produto: " + ", ".join(f"`{o}`" for o in grupos_origem))
        if descricoes_origem:
            st.caption("📝 Descrições movidas para este grupo: " + ", ".join(f"`{d}`" for d in descricoes_origem))

        col_juntar, col_mover = st.columns(2)
        with col_juntar:
            clicou_juntar = st.button(
                "🔗 Juntar com outro grupo", key=f"btn_juntar_{p_idx}_{chave_produto}", width="stretch"
            )
        with col_mover:
            clicou_mover = st.button(
                "➡️ Mover para outro grupo", key=f"btn_mover_{p_idx}_{chave_produto}", width="stretch"
            )

        chave_estado = f"agrupamento_{chave_produto}"
        if clicou_juntar:
            st.session_state[chave_estado] = "juntar"
        elif clicou_mover:
            st.session_state[chave_estado] = "mover"

        acao = st.session_state.get(chave_estado)
        if acao:
            renderizar_seletor_agrupamento(acao, prod, p_idx, todos_grupos, chave_estado)

        st.markdown("---")
        aprovadas = sum(
            1
            for d in descricoes
            if chave_descricao_aprovada(chave_produto, d, itens_auditados)
        )
        st.caption(
            f"Descrições do grupo: {len(descricoes)} | Aprovadas: {aprovadas} | Pendentes: {len(descricoes) - aprovadas}"
        )

        for idx_d, d in enumerate(descricoes):
            chave_desc_unica = chave_descricao(chave_produto, d)
            ja_aprovado = chave_descricao_aprovada(
                chave_produto, d, itens_auditados
            )
            pendente_aberr = (
                chave_desc_unica in aberracoes_pendentes
                or limpar_texto(str(d)) in descricoes_aberrantes
            )
            if ja_aprovado and not mostrar_todos and not pendente_aberr:
                continue

            ultima = ultimas_compras.get(str(d)) or {}
            preco_ultima = ultima.get("preco_ultima", ultima.get("preco"))
            preco_anterior = ultima.get("preco_anterior")
            item_danfe_ultima = ultima.get("item_id_ultima", ultima.get("item_id"))
            item_danfe_anterior = ultima.get("item_id_anterior")
            variacao_brusca = pendente_aberr or _variacao_e_brusca(
                preco_anterior, preco_ultima
            )

            def renderizar_preco_compra(coluna, titulo, preco, item_id, sufixo):
                """Preço + "Ver DANFE" da compra (última ou penúltima)."""
                with coluna:
                    st.caption(titulo)
                    col_valor, col_danfe = st.columns([1.15, 1.0])
                    with col_valor:
                        if preco is None or pd.isna(preco):
                            st.write("—")
                        elif variacao_brusca:
                            st.markdown(f":red[**{formatar_moeda(preco)}**]")
                        else:
                            st.write(formatar_moeda(preco))
                    with col_danfe:
                        if item_id is not None and not pd.isna(item_id):
                            if st.button(
                                "Ver DANFE",
                                key=(
                                    f"btn_danfe_{sufixo}_{p_idx}_{idx_d}_"
                                    f"{chave_produto}"
                                ),
                                width="stretch",
                                help=f"Abre o DANFE/XML original da {titulo.lower()}.",
                            ):
                                mostrar_nota_fiscal(int(item_id))

            col_anterior, col_ultima, col_desc_txt, col_desc_btn = st.columns(
                [1.55, 1.55, 3.0, 1.7]
            )
            renderizar_preco_compra(
                col_anterior,
                "Penúltima compra",
                preco_anterior,
                item_danfe_anterior,
                "anterior",
            )
            renderizar_preco_compra(
                col_ultima,
                "Última compra",
                preco_ultima,
                item_danfe_ultima,
                "ultima",
            )
            with col_desc_txt:
                if ja_aprovado:
                    st.success(f"✅ **Aprovado:** {d}")
                else:
                    st.write(f"{'🔥' if pendente_aberr else '📝'} {d}")
                if variacao_brusca and preco_anterior is not None:
                    variacao = ultima.get("variacao_pct")
                    if variacao is not None:
                        st.caption(
                            f":red[⚠ Variação de {variacao:+.1f}% entre as compras]"
                        )
            with col_desc_btn:
                if ja_aprovado:
                    if st.button(
                        "Desfazer Aprovação",
                        key=f"btn_desfazer_{p_idx}_{idx_d}_{chave_produto}",
                        type="primary",
                        width="stretch",
                    ):
                        try:
                            resp = requests.delete(
                                f"{AUDIT_URL}/{quote(chave_desc_unica, safe='')}",
                                headers=cabecalhos_api(),
                                timeout=30,
                            )
                            if resp.status_code == 200:
                                st.rerun()
                            else:
                                st.error(f"Erro ao desfazer aprovação ({resp.status_code}).")
                        except Exception as e:
                            st.error(f"Erro: {e}")
                else:
                    if st.button("Aprovar", key=f"btn_aprovar_{p_idx}_{idx_d}_{chave_produto}", width="stretch"):
                        try:
                            resp = requests.post(
                                AUDIT_URL,
                                json={"chave_identificacao": chave_desc_unica},
                                headers=cabecalhos_api(),
                                timeout=30,
                            )
                            if resp.status_code == 200:
                                st.rerun()
                            else:
                                st.error("Erro ao salvar no backend.")
                        except Exception as e:
                            st.error(f"Erro: {e}")

        st.markdown("#### Histórico das Compras Registradas:")
        colunas_hist = ["id", "data_emissao", "fornecedor", "descricao_produto", "quantidade", "valor_unitario"]
        df_hist = grupo[[c for c in colunas_hist if c in grupo.columns]].copy()
        df_hist = df_hist.sort_values("data_emissao", ascending=False)
        total_compras = len(df_hist)
        tem_botao_nota = "id" in df_hist.columns

        mostrar_historico_completo = total_compras <= LIMITE_HISTORICO_COMPRAS
        if total_compras > LIMITE_HISTORICO_COMPRAS:
            mostrar_historico_completo = st.checkbox(
                f"Mostrar todas as {formatar_numero(total_compras)} compras "
                f"(padrão: as {formatar_numero(LIMITE_HISTORICO_COMPRAS)} mais recentes)",
                key=f"chk_historico_{p_idx}_{chave_produto}",
            )
        if not mostrar_historico_completo:
            df_hist = df_hist.head(LIMITE_HISTORICO_COMPRAS)

        larguras_hist = [1.1, 2.0, 3.4, 0.8, 1.2, 1.3]
        titulos_hist = ["Data", "Fornecedor", "Descrição Original", "Qtd", "Valor Unit.", "Nota Fiscal"]
        for coluna, titulo in zip(st.columns(larguras_hist), titulos_hist):
            coluna.markdown(f"**{titulo}**")
        st.divider()

        for _, linha in df_hist.iterrows():
            col_data, col_forn, col_desc, col_qtd, col_unit, col_nota = st.columns(larguras_hist)
            col_data.write(formatar_data(linha.get("data_emissao")))
            col_forn.write(str(linha.get("fornecedor") or "—"))
            col_desc.write(str(linha.get("descricao_produto") or "—"))
            col_qtd.write(formatar_quantidade(linha.get("quantidade")))
            col_unit.write(formatar_moeda(linha.get("valor_unitario")))
            item_id = linha.get("id") if tem_botao_nota else None
            if item_id is not None and not pd.isna(item_id):
                if col_nota.button(
                    "🔍 Ver Nota", key=f"btn_nota_{int(item_id)}", width="stretch"
                ):
                    mostrar_nota_fiscal(int(item_id))
        st.caption(
            f"Exibindo {formatar_numero(len(df_hist))} de {formatar_numero(total_compras)} "
            "compras registradas."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Relatórios (visão do cliente)
# ──────────────────────────────────────────────────────────────────────────────
def usar_todas_as_lojas(loja):
    """True quando o filtro está em "Minhas Lojas (Todas)" (ou vazio)."""
    return not loja or str(loja).strip().lower() in LOJAS_TODAS_ALIASES


def filtrar_por_loja(df, loja):
    """Recorta o DataFrame da loja escolhida; "Minhas Lojas (Todas)" mantém o grupo inteiro."""
    if usar_todas_as_lojas(loja):
        return df
    return df[df["nome_loja"] == loja]


def filtro_loja(df, chave, lojas_cadastradas=None):
    """Filtro de loja: o grupo inteiro ("Minhas Lojas (Todas)") ou uma loja/CNPJ.

    As opções juntam as lojas com compra na base e as lojas cadastradas (os 3 CNPJs do
    grupo), então dá para escolher uma loja mesmo quando ela ainda não tem nota
    importada — nesse caso a tabela avisa que não há compra no período. O rótulo mostra
    o CNPJ de cada loja para não confundir nomes parecidos.
    """
    lojas_dados = sorted(df["nome_loja"].dropna().unique().tolist()) if "nome_loja" in df else []
    cadastradas = [
        loja for loja in (lojas_cadastradas or []) if loja and loja.get("nome_loja")
    ]
    nomes = sorted({*lojas_dados, *(loja["nome_loja"] for loja in cadastradas)})
    cnpj_por_loja = {
        loja["nome_loja"]: formatar_cnpj(loja.get("cnpj")) for loja in cadastradas
    }
    opcoes = [ROTULO_TODAS_LOJAS] + nomes

    def rotulo(opcao):
        if opcao == ROTULO_TODAS_LOJAS:
            quantidade = len(nomes)
            plural = "CNPJ" if quantidade <= 1 else "CNPJs"
            return f"🏬 {opcao} — {quantidade} {plural}"
        cnpj = cnpj_por_loja.get(opcao)
        sufixo = f" ({cnpj})" if cnpj and cnpj != "—" else ""
        sem_dados = "" if opcao in lojas_dados else " — sem compras importadas"
        return f"🏪 {opcao}{sufixo}{sem_dados}"

    return st.selectbox(
        "🏪 Loja",
        opcoes,
        key=chave,
        format_func=rotulo,
        help=(
            "**Minhas Lojas (Todas)** agrupa as compras de todos os CNPJs do grupo. "
            "Escolha uma loja para ver só as compras dela (o comparativo entre lojas usa "
            "as lojas selecionadas na própria aba)."
        ),
    )


def filtro_dias_periodicidade(chave, dias_padrao=30, periodicidade_padrao="Mensal"):
    """Filtros padronizados dos relatórios: janela em dias e periodicidade do agrupamento.

    A janela termina na data da nota mais recente da base (mesma referência usada pelo
    backend), para continuar útil quando a importação está alguns dias atrasada.
    """
    col_dias, col_periodicidade = st.columns(2)
    with col_dias:
        dias = st.selectbox(
            "📅 Janela de análise (dias)",
            DIAS_RELATORIO,
            index=DIAS_RELATORIO.index(dias_padrao) if dias_padrao in DIAS_RELATORIO else 2,
            key=f"{chave}_dias",
            help="Contada a partir da data da nota mais recente importada.",
        )
    with col_periodicidade:
        opcoes = list(PERIODICIDADES_RELATORIO.keys())
        rotulo = st.selectbox(
            "🗓️ Periodicidade",
            opcoes,
            index=opcoes.index(periodicidade_padrao) if periodicidade_padrao in opcoes else 3,
            key=f"{chave}_periodicidade",
        )
    return dias, PERIODICIDADES_RELATORIO[rotulo]


def filtro_produto_relatorio(df, chave, limite_opcoes=400):
    """Busca por descrição/EAN e devolve a descrição escolhida (None = todos os produtos).

    O produto vai para o backend como texto: lá ele é resolvido para a chave do grupo,
    então escolher qualquer descrição traz o histórico do produto agrupado.
    """
    termo = st.text_input("🔎 Filtrar produto (descrição ou EAN)", "", key=f"{chave}_busca").strip()
    base = df
    if termo:
        termo = termo.upper()
        base = df[
            df["descricao_produto"].astype(str).str.upper().str.contains(termo, na=False)
            | df["ean"].astype(str).str.upper().str.contains(termo, na=False)
        ]
    opcoes = sorted(base["descricao_produto"].dropna().unique().tolist())
    if len(opcoes) > limite_opcoes:
        st.caption(
            f"Mostrando {limite_opcoes} de {len(opcoes)} produtos. Use a busca acima para localizar os demais."
        )
        opcoes = opcoes[:limite_opcoes]

    rotulo_todos = "Todos os produtos (visão geral)"
    escolha = st.selectbox("📦 Produto", [rotulo_todos] + opcoes, key=f"{chave}_produto")
    return None if escolha == rotulo_todos else escolha


# ──────────────────────────────────────────────────────────────────────────────
# Pivot tables (formato Excel) usadas nas abas Evolução e Comparativo
# ──────────────────────────────────────────────────────────────────────────────
# O `st.dataframe` não aceita cabeçalho de 2 níveis (Período > Loja/Mercado) nem tooltip
# por célula, então a tabela é desenhada como HTML: o cabeçalho usa colspan/rowspan como no
# Excel e cada célula da loja recebe o atributo `title` com data, fornecedor e valor das
# compras que formaram aquela média. A coluna Mercado fica sempre sem tooltip (LGPD).
CSS_TABELA_PIVOT = """
<style>
/* Caixa da tabela: rola nos dois eixos com o cabeçalho fixo no topo e a coluna do
   produto fixa à esquerda. A altura máxima mantém a barra de rolagem HORIZONTAL sempre
   visível na parte de baixo da caixa (não é preciso descer a página para achá-la). */
div.pvt-wrap { max-height: 74vh; overflow: auto; border: 1px solid #e3e6ef; border-radius: 10px;
    background: #ffffff; scrollbar-width: thin; scrollbar-color: #9aa6bd #eef1f8; }
div.pvt-wrap::-webkit-scrollbar { height: 13px; width: 13px; }
div.pvt-wrap::-webkit-scrollbar-track { background: #eef1f8; border-radius: 10px; }
div.pvt-wrap::-webkit-scrollbar-thumb { background: #9aa6bd; border-radius: 7px;
    border: 2px solid #eef1f8; }
div.pvt-wrap::-webkit-scrollbar-thumb:hover { background: #7b8aa8; }
table.pvt { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 0.82rem;
    font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
table.pvt th, table.pvt td { padding: 6px 10px; border-bottom: 1px solid #eef0f6;
    border-right: 1px solid #f2f4f9; white-space: nowrap; }
table.pvt thead th { background: #eef1f8; color: #1f2430; font-weight: 600; text-align: center;
    position: sticky; top: 0; z-index: 2; }
table.pvt thead tr:first-child th { height: 34px; box-sizing: border-box; }
table.pvt thead tr:nth-child(2) th { background: #f6f8fc; color: #3c4257; font-size: 0.76rem;
    position: sticky; top: 34px; z-index: 2; }
table.pvt th.prod, table.pvt td.prod { position: sticky; left: 0; z-index: 1; background: #ffffff;
    text-align: left; font-weight: 600; color: #1f2430; min-width: 220px; max-width: 320px;
    white-space: normal; border-right: 1px solid #e3e6ef; }
table.pvt thead th.prod { z-index: 3; background: #eef1f8; }
table.pvt td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.pvt td.vazia { color: #b6bccb; }
table.pvt tbody tr:nth-child(even) td { background: #fafbff; }
table.pvt tbody tr:hover td { background: #eef4ff; }
table.pvt td.tt { cursor: help; text-decoration: underline dotted #c3c9d6; text-underline-offset: 3px; }
table.pvt td.div { background: #fff3e0 !important; color: #b25000; font-weight: 600; }
table.pvt td.det { color: #39415a; font-size: 0.74rem; max-width: 260px; white-space: normal; }
table.pvt td.det small { color: #6b7385; }
table.pvt tbody tr.aberr td { background: #fdecea !important; color: #b3261e; }
table.pvt td.status { font-size: 0.76rem; font-weight: 600; color: #b3261e; white-space: normal;
    max-width: 190px; }
table.pvt td.status.ok { color: #1b5e20; }
table.pvt td.aum { color: #b71c1c; font-weight: 600; }
table.pvt td.eco { color: #1b5e20; font-weight: 600; }
/* Variação (%): vermelho quando o preço CAIU, verde quando SUBIU. */
table.pvt td.var-neg { color: #b71c1c; font-weight: 600; }
table.pvt td.var-pos { color: #1b5e20; font-weight: 600; }
table.pvt td.var-zero { color: #6b7385; }
</style>
"""


def _escapar_html(texto):
    """Escapa texto livre (produto, fornecedor) para entrar no HTML da tabela."""
    return html.escape(str(texto if texto is not None else ""), quote=True)


def _atributo_html(texto):
    """Texto do tooltip (atributo title) escapado, preservando as quebras de linha."""
    return _escapar_html(texto).replace("\n", "&#10;")


def _detalhes_compras(compras):
    """Uma linha de texto por compra: '27/10/2026 • Fornecedor • R$ 12,50'."""
    return [
        f"{formatar_data(compra.get('data'))} • {compra.get('fornecedor') or 'Fornecedor não informado'}"
        f" • {formatar_moeda(compra.get('valor'))}"
        for compra in compras or []
    ]


def _tooltip_compras(compras):
    """Tooltip de uma célula de loja: uma compra por linha (vazio = célula sem tooltip)."""
    return "\n".join(_detalhes_compras(compras))


def _tooltip_compras_por_loja(compras_por_loja):
    """Tooltip com as compras separadas por loja (usado quando o filtro é 'Todas as Lojas')."""
    blocos = []
    for nome in sorted(compras_por_loja or {}):
        linhas = _detalhes_compras(compras_por_loja[nome])
        if linhas:
            blocos.append("\n".join([f"{nome}:"] + [f"    {linha}" for linha in linhas]))
    return "\n".join(blocos)


def _tooltip_marcas(linha):
    """Tooltip da coluna Mercado: outras marcas do mesmo NCM (só informativo).

    O valor da célula continua sendo a média exata do EAN/descrição do produto
    (`mercado_preco_medio`); esta lista vem da consulta paralela do backend
    (`mercado_marcas`) e mostra as marcas substitutas com o preço médio de cada uma —
    "MANDIOCA MATHEUS" x "MANDIOCA YOKI". Nada aqui é somado a nenhum total.
    """
    marcas = [marca for marca in (linha.get("mercado_marcas") or []) if marca.get("marca")]
    if not marcas:
        return ""
    linhas = ["🏷️ Outras marcas do mesmo NCM (descrição parecida):"]
    for marca in marcas:
        linhas.append(
            f"    {marca['marca']} • {formatar_moeda(marca.get('preco_medio'))}"
            f" ({formatar_numero(marca.get('registros') or 0)} compra(s))"
        )
    return "\n".join(linhas)


def _renomear_produtos(linhas):
    """Rótulo único por grupo (chave) nas linhas da pivot: repetições viram "Nome (2)".

    O backend agrupa por chave (EAN/descrição), então duas chaves diferentes podem ter a
    mesma descrição mais frequente. Sem o sufixo a pivot juntaria dois produtos na mesma
    linha (média de preços de itens diferentes); com ele cada grupo tem a sua linha.
    Devolve cópias das linhas para não alterar a resposta guardada no cache do Streamlit.
    """
    copias = [dict(linha) for linha in linhas or []]
    rotulos = {}
    contagem = {}
    for indice, linha in enumerate(copias):
        chave = linha.get("chave")
        if chave is None:
            # Sem chave no payload: cada linha é um grupo próprio (evita fundir produtos).
            chave = f"__linha_{indice}"
        if chave in rotulos:
            linha["produto"] = rotulos[chave]
            continue
        nome = linha.get("produto") or chave
        contagem[nome] = contagem.get(nome, 0) + 1
        sufixo = contagem[nome]
        rotulos[chave] = nome if sufixo == 1 else f"{nome} ({sufixo})"
        linha["produto"] = rotulos[chave]
    return copias


def _montar_pivot(registros, ordem_periodos, colunas_valor):
    """Monta a tabela dinâmica: linhas = Produto, colunas = (Período, coluna de valor).

    `pivot_table` faz a agregação (cada produto x período aparece uma única vez, então a
    média é o próprio valor) e o `reindex` final fixa a ordem do Excel: períodos em ordem
    cronológica e, dentro de cada período, as colunas na ordem recebida (lojas..., Mercado
    por último).
    """
    base = pd.DataFrame(registros)
    if base.empty:
        return pd.DataFrame()
    pivot = base.pivot_table(
        index="Produto",
        columns="Período",
        values=list(colunas_valor),
        aggfunc="mean",
        dropna=False,
    )
    # pivot_table devolve (valor, período); o Excel mostra (período, valor).
    pivot = pivot.swaplevel(0, 1, axis=1)
    # O reindex fixa a ordem do Excel e recria como vazia a combinação que não teve compra
    # (sem isso a loja que não comprou no período perderia a coluna naquele período).
    colunas = [(periodo, coluna) for periodo in ordem_periodos for coluna in colunas_valor]
    if not colunas:
        return pd.DataFrame()
    pivot = pivot.reindex(columns=pd.MultiIndex.from_tuples(colunas))
    return pivot.sort_index()


def _janela_pivot(produtos, periodos, colunas_por_periodo):
    """Escolhe produtos/períodos exibidos respeitando o orçamento de células da tabela HTML.

    Tabelas muito largas pesam no navegador: com poucos períodos cabem todos os produtos;
    com muitos períodos a lista de produtos é reduzida (sempre avisando na tela).
    """
    periodos_exibidos = periodos[-LIMITE_PERIODOS_PIVOT:]
    if not periodos_exibidos:
        return [], []
    largura = max(1, len(periodos_exibidos) * max(1, colunas_por_periodo))
    limite = max(1, min(LIMITE_PRODUTOS_PIVOT, LIMITE_CELULAS_PIVOT // largura))
    return produtos[:limite], periodos_exibidos


def _recortar_pivot(pivot, produtos, periodos, colunas_valor):
    """Recorta a pivot para os produtos/períodos exibidos, mantendo a ordem das colunas."""
    if pivot is None or pivot.empty:
        return pd.DataFrame()
    linhas = [produto for produto in produtos if produto in pivot.index]
    colunas = [(periodo, coluna) for periodo in periodos for coluna in colunas_valor]
    colunas = [coluna for coluna in colunas if coluna in pivot.columns]
    if not linhas or not colunas:
        return pd.DataFrame()
    return pivot.loc[linhas].reindex(columns=pd.MultiIndex.from_tuples(colunas))


def _renderizar_pivot(
    pivot,
    colunas_valor,
    rotulos=None,
    tooltips=None,
    destaques=None,
    detalhes=None,
    rotulo_detalhes="🛒 Detalhes",
):
    """Desenha a pivot no formato do Excel: cabeçalho de 2 linhas (Período > sub-colunas).

    `rotulos` renomeia as sub-colunas, `tooltips` guarda o texto de hover por célula,
    `destaques` aplica classes CSS extras (ex.: maior preço do período) e `detalhes`
    acrescenta uma última coluna por produto (texto, tooltip).
    """
    if pivot is None or pivot.empty:
        st.info("Sem dados para montar a tabela.")
        return

    rotulos = rotulos or {}
    tooltips = tooltips or {}
    destaques = destaques or {}
    grupos = list(dict.fromkeys(pivot.columns.get_level_values(0)))

    partes = [
        CSS_TABELA_PIVOT,
        '<div class="pvt-wrap">',
        '<table class="pvt">',
        "<thead>",
        "<tr>",
        '<th class="prod" rowspan="2">Produto</th>',
    ]
    for grupo in grupos:
        partes.append(f'<th colspan="{len(colunas_valor)}">{_escapar_html(grupo)}</th>')
    if detalhes is not None:
        partes.append(f'<th class="det" rowspan="2">{_escapar_html(rotulo_detalhes)}</th>')
    partes.extend(["</tr>", "<tr>"])
    for _ in grupos:
        for coluna in colunas_valor:
            partes.append(f"<th>{_escapar_html(rotulos.get(coluna, coluna))}</th>")
    partes.extend(["</tr>", "</thead>", "<tbody>"])

    for produto in pivot.index:
        valores_produto = pivot.loc[produto]
        partes.append(f'<tr><td class="prod">{_escapar_html(produto)}</td>')
        for grupo in grupos:
            for coluna in colunas_valor:
                valor = valores_produto.get((grupo, coluna))
                vazio = valor is None or pd.isna(valor)
                classes = "num vazia" if vazio else "num"
                destaque = destaques.get((produto, grupo, coluna))
                if destaque:
                    classes = f"{classes} {destaque}"
                texto = "—" if vazio else formatar_moeda(valor)
                dica = tooltips.get((produto, grupo, coluna))
                if dica:
                    partes.append(
                        f'<td class="{classes} tt" title="{_atributo_html(dica)}">{texto}</td>'
                    )
                else:
                    partes.append(f'<td class="{classes}">{texto}</td>')
        if detalhes is not None:
            texto_detalhe, dica_detalhe = detalhes.get(produto, ("—", ""))
            if dica_detalhe:
                partes.append(
                    f'<td class="det tt" title="{_atributo_html(dica_detalhe)}">{texto_detalhe}</td>'
                )
            else:
                partes.append(f'<td class="det">{texto_detalhe}</td>')
        partes.append("</tr>")

    partes.extend(["</tbody>", "</table>", "</div>"])
    st.markdown("\n".join(partes), unsafe_allow_html=True)


def _tabela_compras_detalhadas(linhas, lojas=None):
    """Tabela 'tidy' das compras que formaram os preços médios (usada no expander)."""
    registros = []
    for linha in linhas or []:
        for nome, compras in (linha.get("compras") or {}).items():
            if lojas is not None and nome not in lojas:
                continue
            for compra in compras or []:
                registros.append(
                    {
                        "Produto": linha.get("produto"),
                        "Período": linha.get("label"),
                        "Loja": nome,
                        "Data": pd.to_datetime(compra.get("data"), errors="coerce"),
                        "Fornecedor": compra.get("fornecedor") or "—",
                        "Valor Unitário (R$)": compra.get("valor"),
                    }
                )
    if not registros:
        return pd.DataFrame()
    tabela = pd.DataFrame(registros)
    return tabela.sort_values(
        ["Produto", "Data"], ascending=[True, False], na_position="last"
    ).reset_index(drop=True)


def _exibir_compras_detalhadas(linhas, lojas=None, chave="detalhes"):
    """Expander com data, fornecedor e valor de cada compra — somente das lojas do usuário."""
    tabela = _tabela_compras_detalhadas(linhas, lojas)
    if tabela.empty:
        return
    with st.expander(
        f"🛒 Compras que formaram os preços médios ({formatar_numero(len(tabela))} registros)"
    ):
        st.caption(
            "Somente compras das suas lojas. O mercado aparece apenas como preço médio, "
            "sem identificação de fornecedor (proteção LGPD)."
        )
        st.dataframe(
            tabela,
            width="stretch",
            hide_index=True,
            key=f"{chave}_compras",
            column_config={
                "Produto": st.column_config.TextColumn("Produto", width="large"),
                "Período": st.column_config.TextColumn("Período"),
                "Loja": st.column_config.TextColumn("Loja"),
                "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "Fornecedor": st.column_config.TextColumn("Fornecedor", width="medium"),
                "Valor Unitário (R$)": st.column_config.NumberColumn(
                    "Valor Unitário (R$)", format="R$ %.2f"
                ),
            },
        )


def _resumo_detalhes(linhas, lojas=None):
    """Coluna 'Detalhes' da pivot: quantas compras por loja + tooltip com todas elas."""
    por_produto = {}
    for linha in linhas or []:
        for nome, compras in (linha.get("compras") or {}).items():
            if lojas is not None and nome not in lojas:
                continue
            if compras:
                por_produto.setdefault(linha.get("produto"), []).append(
                    (linha.get("label"), nome, compras)
                )
    resumo = {}
    for produto, registros in por_produto.items():
        contagem = {}
        blocos = []
        for rotulo, nome, compras in registros:
            contagem[nome] = contagem.get(nome, 0) + len(compras)
            blocos.append(
                "\n".join(
                    [f"{rotulo} — {nome}:"]
                    + [f"    {linha}" for linha in _detalhes_compras(compras)]
                )
            )
        texto = " · ".join(
            f"{nome}: {quantidade}×" for nome, quantidade in sorted(contagem.items())
        )
        resumo[produto] = (f"<small>{_escapar_html(texto)}</small>", "\n".join(blocos))
    return resumo


def _avisar_corte_pivot(produtos, produtos_exibidos, periodos, periodos_exibidos):
    """Informa na tela quando a pivot exibe apenas parte dos produtos/períodos."""
    if len(produtos) > len(produtos_exibidos):
        st.caption(
            f"Exibindo os {formatar_numero(len(produtos_exibidos))} primeiros de "
            f"{formatar_numero(len(produtos))} produtos. Use o filtro de produto ou a busca "
            "para localizar os demais."
        )
    if len(periodos) > len(periodos_exibidos):
        st.caption(
            f"Exibindo os {formatar_numero(len(periodos_exibidos))} períodos mais recentes de "
            f"{formatar_numero(len(periodos))}. Reduza a janela de análise para ver todos."
        )


def _periodos_em_ordem(linhas):
    """Rótulos dos períodos em ordem cronológica (o backend envia a data ISO em `periodo`)."""
    mapa = {linha["periodo"]: linha["label"] for linha in linhas}
    return mapa, [mapa[chave] for chave in sorted(mapa)]


def _aviso_integridade(dados):
    """Legenda do que as regras de integridade deixaram FORA do relatório.

    O backend devolve em `integridade` quantos itens foram ignorados por estarem
    pendentes na auditoria e quantos eram bonificados (CFOP 5910/6910 ou R$ 0,00).
    """
    integridade = (dados or {}).get("integridade") or {}
    pendentes = int(integridade.get("pendentes_auditoria") or 0)
    bonificados = int(integridade.get("bonificados") or 0)
    bonificados_cfop = int(integridade.get("bonificados_cfop") or 0)
    bonificados_zero = max(0, bonificados - bonificados_cfop)
    if not pendentes and not bonificados:
        return
    partes = []
    if pendentes:
        partes.append(
            f"{formatar_numero(pendentes)} itens com descrição pendente de aprovação na Auditoria"
        )
    if bonificados_cfop:
        partes.append(
            f"{formatar_numero(bonificados_cfop)} itens com CFOP de bonificação (5910/6910)"
        )
    if bonificados_zero:
        partes.append(f"{formatar_numero(bonificados_zero)} itens bonificados/zerados (R$ 0,00)")
    st.caption("🚫 Fora do cálculo: " + " e ".join(partes) + ".")


def relatorio_evolucao_precos(df, lojas_cadastradas=None):
    """Aba 1: pivot Produto x Período, com as sub-colunas Loja e Mercado em cada período."""
    st.subheader("📈 Evolução de Preços: Produto x Período")
    st.caption(
        "Tabela dinâmica no formato do Excel: cada linha é um produto e cada período tem duas "
        "colunas — o preço médio da loja e o preço médio do mercado. Passe o mouse sobre o preço "
        "da loja para ver data, fornecedor e valor de cada compra que formou a média."
    )

    df_datas = df[df["data_emissao"].notna()]
    if df_datas.empty:
        st.warning("Sem datas de emissão válidas para montar a evolução de preços.")
        return

    loja = filtro_loja(df_datas, "r2_loja", lojas_cadastradas)
    dias, periodicidade = filtro_dias_periodicidade("r2")
    produto = filtro_produto_relatorio(filtrar_por_loja(df_datas, loja), "r2")

    todas_as_lojas = usar_todas_as_lojas(loja)
    parametros = {"dias": dias, "periodicidade": periodicidade, "limite": LIMITE_LINHAS_PIVOT}
    if not todas_as_lojas:
        parametros["lojas"] = [loja]
    if produto:
        parametros["produto"] = produto
    # A pivot precisa do preço da loja E do mercado no mesmo grão (produto x período), então
    # usamos o endpoint do comparativo; sem `lojas` ele devolve todas as lojas da base.
    dados, erro = carregar_relatorio(
        RELATORIO_COMPARATIVO_URL, json.dumps(parametros, sort_keys=True)
    )
    if erro:
        st.error(erro)
        return
    _aviso_integridade(dados)
    linhas = dados.get("linhas") or []
    if not linhas:
        aviso = dados.get("aviso") or "Nenhuma compra encontrada para os filtros selecionados."
        if produto:
            aviso += " O produto filtrado não tem compra nesta janela: aumente a janela de análise."
        st.info(aviso)
        return
    linhas = _renomear_produtos(linhas)

    registros = []
    tooltips = {}
    for linha in linhas:
        precos = {
            nome: valor
            for nome, valor in (linha.get("precos") or {}).items()
            if valor is not None
        }
        compras = linha.get("compras") or {}
        if todas_as_lojas:
            # "Todas as Lojas": média das lojas que compraram o item no período.
            preco_loja = round(sum(precos.values()) / len(precos), 4) if precos else None
            dica = _tooltip_compras_por_loja(compras)
        else:
            preco_loja = precos.get(loja)
            dica = _tooltip_compras(compras.get(loja) or [])
        registros.append(
            {
                "Produto": linha["produto"],
                "Período": linha["label"],
                "Loja": preco_loja,
                "Mercado": linha.get("mercado_preco_medio"),
            }
        )
        if dica:
            tooltips[(linha["produto"], linha["label"], "Loja")] = dica
        # Tooltip da coluna Mercado: outras marcas do mesmo NCM (o número não muda).
        dica_mercado = _tooltip_marcas(linha)
        if dica_mercado:
            tooltips[(linha["produto"], linha["label"], "Mercado")] = dica_mercado

    valores_loja = [registro["Loja"] for registro in registros if registro["Loja"] is not None]
    valores_mercado = [
        registro["Mercado"] for registro in registros if registro["Mercado"] is not None
    ]
    media_loja = sum(valores_loja) / len(valores_loja) if valores_loja else None
    media_mercado = sum(valores_mercado) / len(valores_mercado) if valores_mercado else None
    diferenca = (
        round((media_loja / media_mercado - 1) * 100, 2)
        if (media_loja and media_mercado)
        else None
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Preço Médio Loja",
        formatar_moeda(media_loja),
        help="Média das células da coluna 🏪 Loja (uma por produto x período).",
    )
    m2.metric(
        "Preço Médio Mercado",
        formatar_moeda(media_mercado),
        help="Média das células da coluna 🏷️ Mercado, calculada com todas as empresas da base.",
    )
    m3.metric(
        "Diferença vs. Mercado",
        formatar_percentual(diferenca),
        help="Positivo = a sua loja pagou mais caro que o mercado na média do período.",
    )
    m4.metric("Produtos x Períodos", formatar_numero(len(registros)))
    st.caption(
        f"Janela: {formatar_data(dados.get('data_inicio'))} a {formatar_data(dados.get('data_fim'))} | "
        f"Periodicidade: {dados.get('periodicidade_label')} | Produto: {dados.get('produto')} | "
        f"Loja: {loja}"
    )
    if todas_as_lojas:
        st.caption(
            "Com o filtro em **Minhas Lojas (Todas)**, a coluna 🏪 Loja traz a média das lojas "
            "que compraram o produto no período. Escolha uma loja para ver o preço e as compras dela."
        )

    _, ordem_periodos = _periodos_em_ordem(linhas)
    colunas_valor = ["Loja", "Mercado"]
    pivot = _montar_pivot(registros, ordem_periodos, colunas_valor)
    produtos = list(pivot.index)
    periodos = list(dict.fromkeys(pivot.columns.get_level_values(0)))
    produtos_exibidos, periodos_exibidos = _janela_pivot(
        produtos, periodos, len(colunas_valor)
    )
    exibicao = _recortar_pivot(pivot, produtos_exibidos, periodos_exibidos, colunas_valor)
    if exibicao.empty:
        st.info("Nenhum preço para exibir com os filtros atuais.")
        return

    _renderizar_pivot(
        exibicao,
        colunas_valor,
        tooltips=tooltips,
        detalhes=_resumo_detalhes(linhas, None if todas_as_lojas else {loja}),
    )
    _avisar_corte_pivot(produtos, produtos_exibidos, periodos, periodos_exibidos)
    st.caption(
        "Passe o mouse sobre um preço da coluna 🏪 Loja para ver data • fornecedor • valor de cada "
        "compra. A coluna 🏷️ Mercado é somente o preço médio: nenhum fornecedor é revelado (LGPD) — "
        "o hover dela mostra outras marcas do mesmo NCM, quando existirem."
    )

    if produto:
        indice_periodo = {rotulo: posicao for posicao, rotulo in enumerate(ordem_periodos)}
        with st.expander("📈 Ver o gráfico da evolução do produto"):
            serie = pd.DataFrame(
                [
                    {
                        "Período": registro["Período"],
                        "Loja": registro["Loja"],
                        "Mercado": registro["Mercado"],
                    }
                    for registro in sorted(
                        registros, key=lambda item: indice_periodo.get(item["Período"], 0)
                    )
                ]
            )
            st.line_chart(
                serie, x="Período", y=["Loja", "Mercado"], x_label="Período", y_label="R$"
            )

    _exibir_compras_detalhadas(linhas, None if todas_as_lojas else {loja}, chave="r2")


def relatorio_comparativo_lojas(df, lojas_cadastradas):
    """Aba 2: pivot Produto x Período, com uma coluna por loja e Mercado fechando o período."""
    st.subheader("🏪 Comparativo entre Lojas & Mercado")
    st.caption(
        "Tabela dinâmica no formato do Excel: cada linha é um produto e cada período tem uma coluna "
        "por loja, com a coluna Mercado no fim. Passe o mouse sobre o preço de uma loja para ver as "
        "compras (data • fornecedor • valor) que formaram aquela média — e sobre a coluna Mercado "
        "para ver outras marcas do mesmo NCM."
    )

    df_datas = df[df["data_emissao"].notna()]
    if df_datas.empty:
        st.warning("Sem datas de emissão válidas para montar o comparativo.")
        return

    lojas_com_dados = sorted(df_datas["nome_loja"].dropna().unique().tolist())
    cadastradas = [
        loja for loja in (lojas_cadastradas or []) if loja and loja.get("nome_loja")
    ]
    nomes_cadastrados = [loja["nome_loja"] for loja in cadastradas]
    cnpj_por_loja = {
        loja["nome_loja"]: formatar_cnpj(loja.get("cnpj")) for loja in cadastradas
    }
    opcoes_lojas = sorted({*lojas_com_dados, *nomes_cadastrados})

    def rotulo_loja(opcao):
        if opcao == ROTULO_TODAS_LOJAS:
            return f"🏬 {opcao} — {len(opcoes_lojas)} CNPJ(s)"
        cnpj = cnpj_por_loja.get(opcao)
        sufixo = f" ({cnpj})" if cnpj and cnpj != "—" else ""
        sem_dados = "" if opcao in lojas_com_dados else " — sem compras importadas"
        return f"🏪 {opcao}{sufixo}{sem_dados}"

    selecionadas = st.multiselect(
        "🏪 Lojas comparadas",
        [ROTULO_TODAS_LOJAS, *opcoes_lojas],
        default=[ROTULO_TODAS_LOJAS],
        format_func=rotulo_loja,
        key="r1_lojas",
        help=(
            "**Minhas Lojas (Todas)** compara todos os CNPJs da conta — inclusive os que "
            "ainda não têm nota importada (coluna vazia). Escolha CNPJs específicos para "
            "comparar só entre eles."
        ),
    )
    lojas_sel = (
        opcoes_lojas
        if ROTULO_TODAS_LOJAS in selecionadas
        else [nome for nome in selecionadas if nome != ROTULO_TODAS_LOJAS]
    )
    dias, periodicidade = filtro_dias_periodicidade("r1")
    apenas_divergentes = st.checkbox(
        "⚠️ Mostrar apenas produtos com preços divergentes entre as lojas",
        value=False,
        key="r1_divergentes",
        help=(
            "Ex.: a Loja 1 comprou o item a R$ 10,00 e a Loja 2 comprou o mesmo item a R$ 15,00 "
            "no mesmo período."
        ),
    )
    base_produtos = df_datas[df_datas["nome_loja"].isin(lojas_sel)] if lojas_sel else df_datas
    produto = filtro_produto_relatorio(base_produtos, "r1")

    if not lojas_sel:
        st.info("Selecione pelo menos uma loja para comparar.")
        return

    parametros = {"dias": dias, "periodicidade": periodicidade, "limite": LIMITE_LINHAS_PIVOT}
    parametros["lojas"] = list(lojas_sel)
    if produto:
        parametros["produto"] = produto
    # Sem `apenas_divergentes`: a pivot precisa de todos os períodos do produto. O checkbox é
    # aplicado logo abaixo por PRODUTO (o backend filtra por linha, o que quebraria a tabela).
    dados, erro = carregar_relatorio(
        RELATORIO_COMPARATIVO_URL, json.dumps(parametros, sort_keys=True)
    )
    if erro:
        st.error(erro)
        return

    _aviso_integridade(dados)
    lojas = dados.get("lojas") or []
    linhas = dados.get("linhas") or []
    if not linhas:
        aviso = dados.get("aviso") or "Nenhuma compra encontrada para os filtros selecionados."
        if produto:
            aviso += " O produto filtrado não tem compra nesta janela: aumente a janela de análise."
        st.info(aviso)
        return
    linhas = _renomear_produtos(linhas)
    if apenas_divergentes:
        produtos_divergentes = {linha["produto"] for linha in linhas if linha["divergente"]}
        linhas = [linha for linha in linhas if linha["produto"] in produtos_divergentes]
        if not linhas:
            st.info(
                "Nenhum produto com preço divergente entre as lojas selecionadas. Selecione mais "
                "de uma loja ou desmarque a opção."
            )
            return

    colunas_valor = [*lojas, "Mercado"]
    registros, tooltips, destaques = [], {}, {}
    for linha in linhas:
        precos = linha.get("precos") or {}
        compras = linha.get("compras") or {}
        registro = {"Produto": linha["produto"], "Período": linha["label"]}
        for nome in lojas:
            registro[nome] = precos.get(nome)
        registro["Mercado"] = linha.get("mercado_preco_medio")
        registros.append(registro)

        # Tooltip da coluna Mercado: outras marcas do mesmo NCM (o número não muda).
        dica_mercado = _tooltip_marcas(linha)
        if dica_mercado:
            tooltips[(linha["produto"], linha["label"], "Mercado")] = dica_mercado

        valores = [precos.get(nome) for nome in lojas if precos.get(nome) is not None]
        maior = max(valores) if valores else None
        for nome in lojas:
            dica = _tooltip_compras(compras.get(nome) or [])
            if dica:
                tooltips[(linha["produto"], linha["label"], nome)] = dica
            # Divergência: destaca em laranja a loja que pagou o maior preço no período.
            if linha.get("divergente") and maior is not None and precos.get(nome) == maior:
                destaques[(linha["produto"], linha["label"], nome)] = "div"

    produtos = sorted({linha["produto"] for linha in linhas})
    produtos_divergentes = {linha["produto"] for linha in linhas if linha["divergente"]}
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Produtos Comparados", formatar_numero(len(produtos)))
    m2.metric(
        "Produtos Divergentes",
        formatar_numero(len(produtos_divergentes)),
        help="Produtos com preço médio diferente entre as lojas no mesmo período.",
    )
    m3.metric("Lojas Comparadas", formatar_numero(len(lojas)))
    m4.metric("Produtos x Períodos", formatar_numero(len(linhas)))
    st.caption(
        f"Janela: {formatar_data(dados.get('data_inicio'))} a {formatar_data(dados.get('data_fim'))} | "
        f"Periodicidade: {dados.get('periodicidade_label')} | Produto: {dados.get('produto')}"
    )
    lojas_sem_dados = [
        nome
        for nome in lojas
        if nome not in ((dados.get("resumo") or {}).get("lojas_com_dados") or [])
    ]
    if lojas_sem_dados:
        st.caption(
            "Sem compras no período (coluna vazia): "
            + ", ".join(lojas_sem_dados)
            + ". Aumente a janela ou confira se as notas dessa loja foram importadas."
        )
    if dados.get("truncado"):
        st.warning(
            f"A consulta retornou {formatar_numero(dados.get('total_linhas'))} linhas; exibindo as "
            f"primeiras {formatar_numero(len(linhas))}. Reduza a janela ou filtre o produto."
        )

    _, ordem_periodos = _periodos_em_ordem(linhas)
    pivot = _montar_pivot(registros, ordem_periodos, colunas_valor)
    lista_produtos = list(pivot.index)
    periodos = list(dict.fromkeys(pivot.columns.get_level_values(0)))
    produtos_exibidos, periodos_exibidos = _janela_pivot(
        lista_produtos, periodos, len(colunas_valor)
    )
    exibicao = _recortar_pivot(pivot, produtos_exibidos, periodos_exibidos, colunas_valor)
    if exibicao.empty:
        st.info("Nenhum preço para exibir com os filtros atuais.")
        return

    _renderizar_pivot(
        exibicao,
        colunas_valor,
        tooltips=tooltips,
        destaques=destaques,
        detalhes=_resumo_detalhes(linhas, set(lojas)),
    )
    _avisar_corte_pivot(lista_produtos, produtos_exibidos, periodos, periodos_exibidos)
    st.caption(
        "Células em laranja = maior preço do período (divergência entre lojas). A coluna "
        "🛒 Detalhes resume quantas compras formaram os preços — passe o mouse para ver data, "
        "fornecedor e valor. Mercado é apenas o preço médio: nenhum fornecedor é revelado (LGPD)."
    )

    _exibir_compras_detalhadas(linhas, set(lojas), chave="r1")


ORDENACOES_IMPACTO = {
    "💸 Impacto mensal (maior → menor)": "impacto",
    "📈 Variação % (maior → menor)": "variacao_desc",
    "📉 Variação % (menor → maior)": "variacao_asc",
    "🔤 Produto (A → Z)": "produto",
    "🗓️ Data da última compra (mais recente)": "data",
}


def _chave_ordenacao_impacto(ordenacao):
    """Função de ordenação (crescente) equivalente a cada opção de `ORDENACOES_IMPACTO`."""
    if ordenacao == "variacao_desc":
        return lambda linha: -float(linha.get("variacao_pct") or 0.0)
    if ordenacao == "variacao_asc":
        return lambda linha: float(linha.get("variacao_pct") or 0.0)
    if ordenacao == "produto":
        return lambda linha: str(linha.get("produto") or "").upper()
    if ordenacao == "data":

        def por_data(linha):
            try:
                return -pd.Timestamp(linha.get("data_atual")).value
            except (TypeError, ValueError):
                return 0

        return por_data
    return lambda linha: -abs(float(linha.get("impacto_mensal") or 0.0))


def _ordenar_linhas_impacto(linhas, ordenacao="impacto"):
    """Ordena as linhas do Impacto mantendo as aberrações SEMPRE no fim da tabela."""
    chave = _chave_ordenacao_impacto(ordenacao)
    normais = sorted((linha for linha in linhas or [] if not linha.get("aberrante")), key=chave)
    aberracoes = sorted((linha for linha in linhas or [] if linha.get("aberrante")), key=chave)
    return normais + aberracoes


def _renderizar_tabela_impacto(linhas, meses, ordenacao="impacto"):
    """Tabela HTML do Impacto com tooltip de DATA nas colunas de preço.

    O `st.dataframe` não permite tooltip por célula, então usamos o mesmo padrão das abas
    Evolução/Comparativo (HTML + atributo `title`): em "Preço Atual" aparece a data da
    última compra e em "Preço Anterior" a data da compra imediatamente anterior (com o
    fornecedor de cada uma). A coluna "Variação (%)" sai em vermelho quando o preço caiu e
    em verde quando subiu; aumento de custo em vermelho, economia em verde e as aberrações
    (variação de 50% ou mais) ficam no fim, em vermelho, com o alerta "Verificar Variação
    Brusca" no lugar dos valores de aumento/economia.
    """
    ordenadas = _ordenar_linhas_impacto(linhas, ordenacao)
    partes = [
        CSS_TABELA_PIVOT,
        '<div class="pvt-wrap">',
        '<table class="pvt">',
        "<thead><tr>",
        '<th class="prod">Produto</th>',
        "<th>Preço Atual (R$)</th>",
        "<th>Preço Anterior (R$)</th>",
        "<th>Variação (%)</th>",
        f"<th>Consumo Médio ({int(meses)}m)</th>",
        "<th>🔴 Impacto Negativo (Aumento) (R$)</th>",
        "<th>🟢 Impacto Positivo (Economia) (R$)</th>",
        "<th>Status</th>",
        "</tr></thead>",
        "<tbody>",
    ]
    for linha in ordenadas:
        aberrante = bool(linha.get("aberrante"))
        produto = _escapar_html(linha.get("produto"))
        variacao = linha.get("variacao_pct")
        if variacao is None or pd.isna(variacao):
            classe_variacao, dica_variacao = "num var-zero", "Sem comparação de preço"
        elif float(variacao) < 0:
            classe_variacao = "num var-neg"
            dica_variacao = f"Queda de preço: {formatar_percentual(variacao)}"
        elif float(variacao) > 0:
            classe_variacao = "num var-pos"
            dica_variacao = f"Aumento de preço: {formatar_percentual(variacao)}"
        else:
            classe_variacao, dica_variacao = "num var-zero", "Preço estável (0,0%)"
        dica_atual = (
            f"Última compra em {formatar_data(linha.get('data_atual'))}"
            f"\nFornecedor: {linha.get('fornecedor_atual') or 'Fornecedor não informado'}"
            f"\nPreço: {formatar_moeda(linha.get('preco_atual'))}"
        )
        dica_anterior = (
            f"Compra anterior em {formatar_data(linha.get('data_anterior'))}"
            f"\nFornecedor: {linha.get('fornecedor_anterior') or 'Fornecedor não informado'}"
            f"\nPreço: {formatar_moeda(linha.get('preco_anterior'))}"
        )
        if aberrante:
            classe_aumento, texto_aumento = "num vazia", "—"
            classe_economia, texto_economia = "num vazia", "—"
            status = f"⚠️ {linha.get('alerta') or 'Verificar Variação Brusca'}"
            classe_status = "status"
        else:
            aumento = float(linha.get("aumento") or 0.0)
            economia = float(linha.get("economia") or 0.0)
            classe_aumento = "num aum" if aumento > 0 else "num vazia"
            texto_aumento = formatar_moeda(aumento) if aumento > 0 else "—"
            classe_economia = "num eco" if economia > 0 else "num vazia"
            texto_economia = formatar_moeda(economia) if economia > 0 else "—"
            if linha.get("validada"):
                status, classe_status = "✅ Variação validada", "status ok"
            else:
                status, classe_status = "", "status"
        partes.append(
            f'<tr class="aberr">' if aberrante else "<tr>"
        )
        partes.append(
            f'<td class="prod">{produto}</td>'
            f'<td class="num tt" title="{_atributo_html(dica_atual)}">'
            f'{formatar_moeda(linha.get("preco_atual"))}</td>'
            f'<td class="num tt" title="{_atributo_html(dica_anterior)}">'
            f'{formatar_moeda(linha.get("preco_anterior"))}</td>'
            f'<td class="{classe_variacao} tt" title="{_atributo_html(dica_variacao)}">'
            f'{formatar_percentual(linha.get("variacao_pct"))}</td>'
            f'<td class="num">{formatar_quantidade(linha.get("consumo_medio"))}</td>'
            f'<td class="{classe_aumento}">{texto_aumento}</td>'
            f'<td class="{classe_economia}">{texto_economia}</td>'
            f'<td class="{classe_status}">{_escapar_html(status)}</td>'
            "</tr>"
        )
    partes.extend(["</tbody>", "</table>", "</div>"])
    st.markdown("\n".join(partes), unsafe_allow_html=True)


def _planilha_impacto(linhas, meses):
    """Versão em `st.dataframe` da tabela de Impacto (ordenação por CLIQUE no cabeçalho).

    A tabela HTML é a visão principal (tooltip por célula com data/fornecedor), mas ela não
    ordena por clique. Esta planilha usa o `st.dataframe`, cuja ordenação por clique no
    cabeçalho já vem habilitada por padrão (é nativa do widget — não existe parâmetro para
    desligar sem `disabled`), e traz as datas das duas compras como colunas de verdade.
    As aberrações continuam por último e o Status mostra o alerta.
    """
    registros = []
    for linha in _ordenar_linhas_impacto(linhas):
        registros.append(
            {
                "Produto": linha.get("produto"),
                "Preço Atual (R$)": linha.get("preco_atual"),
                "Preço Anterior (R$)": linha.get("preco_anterior"),
                "Variação (%)": linha.get("variacao_pct"),
                f"Consumo Médio ({int(meses)}m)": linha.get("consumo_medio"),
                "Aumento (R$)": linha.get("aumento"),
                "Economia (R$)": linha.get("economia"),
                "Status": linha.get("alerta")
                or ("✅ Variação validada" if linha.get("validada") else "OK"),
                "Data da Última Compra": pd.to_datetime(linha.get("data_atual"), errors="coerce"),
                "Data da Compra Anterior": pd.to_datetime(linha.get("data_anterior"), errors="coerce"),
            }
        )
    if not registros:
        return
    tabela = pd.DataFrame(registros)
    with st.expander("📋 Ver como planilha ordenável (clique no cabeçalho para ordenar)"):
        st.caption(
            "Clique em qualquer cabeçalho para ordenar (clique de novo para inverter). "
            "Aumento e Economia são zero nas linhas com alerta: elas ficam fora dos totais."
        )
        st.dataframe(
            tabela,
            width="stretch",
            hide_index=True,
            key="r3_planilha",
            column_config={
                "Produto": st.column_config.TextColumn("Produto", width="large"),
                "Preço Atual (R$)": st.column_config.NumberColumn("Preço Atual (R$)", format="R$ %.2f"),
                "Preço Anterior (R$)": st.column_config.NumberColumn(
                    "Preço Anterior (R$)", format="R$ %.2f"
                ),
                "Variação (%)": st.column_config.NumberColumn("Variação (%)", format="%.2f%%"),
                f"Consumo Médio ({int(meses)}m)": st.column_config.NumberColumn(
                    f"Consumo Médio ({int(meses)}m)", format="%.3f"
                ),
                "Aumento (R$)": st.column_config.NumberColumn("Aumento (R$)", format="R$ %.2f"),
                "Economia (R$)": st.column_config.NumberColumn("Economia (R$)", format="R$ %.2f"),
                "Status": st.column_config.TextColumn("Status", width="medium"),
                "Data da Última Compra": st.column_config.DateColumn(
                    "Data da Última Compra", format="DD/MM/YYYY"
                ),
                "Data da Compra Anterior": st.column_config.DateColumn(
                    "Data da Compra Anterior", format="DD/MM/YYYY"
                ),
            },
        )


def _painel_reenvio_auditoria(aberracoes):
    """Painel (admin) para reenviar as variações bruscas para a Auditoria.

    A rota `POST /api/auditar/pendente` é exclusiva de administradores (403 para conta
    comum), então o painel só é renderizado para quem pode usá-la. O envio AUTOMÁTICO
    continua acontecendo no backend para todas as contas — o cliente não perde o alerta
    nem os itens marcados, apenas o botão manual.
    """
    if not usuario_e_admin():
        return
    with st.expander("📤 Reenviar estas variações para a Auditoria"):
        st.caption(
            "O envio é automático ao abrir a aba (nada é apagado dos relatórios). Use o "
            "botão só para reforçar o envio, por exemplo depois de marcar itens como "
            "validados na Auditoria."
        )
        if st.button(
            "📤 Enviar novamente para a auditoria",
            key="r3_enviar_auditoria",
            help="Marca os itens como 'Pendente de Aprovação' (motivo 'aberrante') na aba "
            "Auditoria.",
        ):
            marcadas, erro_envio = enviar_pendentes_auditoria(
                [linha.get("chave_descricao") for linha in aberracoes],
                motivo=MOTIVO_PENDENCIA_ABERRACAO,
            )
            if erro_envio:
                st.error(erro_envio)
            else:
                st.session_state["r3_auditoria_enviada"] = marcadas
                st.rerun()


CSS_METRICAS_IMPACTO = """
<style>
/* Cartões do Impacto Financeiro: custo em VERMELHO com sinal negativo, economia em
   VERDE com sinal positivo. O valor grande e tabular facilita a leitura rápida. */
div.metrica-impacto { border-radius: 12px; padding: .85rem 1rem; border: 1px solid #e3e6ef;
    border-left: 6px solid #9aa6bd; background: #ffffff; }
div.metrica-impacto.neg { border-left-color: #b3261e; background: #fdf3f2; }
div.metrica-impacto.pos { border-left-color: #1b5e20; background: #f1f8f2; }
div.metrica-impacto .rotulo { font-size: .78rem; font-weight: 700; letter-spacing: .04em;
    text-transform: uppercase; color: #4a6076; }
div.metrica-impacto .valor { font-size: 1.55rem; font-weight: 700; margin-top: .2rem;
    font-variant-numeric: tabular-nums; color: #1f2430; }
div.metrica-impacto.neg .valor { color: #b3261e; }
div.metrica-impacto.pos .valor { color: #1b5e20; }
div.metrica-impacto .dica { font-size: .74rem; color: #6b7385; margin-top: .25rem; }
</style>
"""


def _metrica_impacto(rotulo, valor, sinal, dica):
    """Cartão do Impacto: `sinal` -1 = custo (vermelho, "-R$"), +1 = economia (verde, "+R$")."""
    numero = float(valor or 0.0)
    if numero == 0:
        classe, texto = "neutro", formatar_moeda(0.0)
    else:
        classe = "neg" if sinal < 0 else "pos"
        texto = f"{'-' if sinal < 0 else '+'}{formatar_moeda(abs(numero))}"
    st.markdown(
        f'<div class="metrica-impacto {classe}" title="{_atributo_html(dica)}">'
        f'<div class="rotulo">{_escapar_html(rotulo)}</div>'
        f'<div class="valor">{_escapar_html(texto)}</div>'
        f'<div class="dica">{_escapar_html(dica)}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


def relatorio_impacto_financeiro(df, lojas_cadastradas=None):
    """Aba 3: impacto mensal da variação entre a última e a penúltima compra."""
    st.subheader("💰 Impacto Financeiro da Variação de Preço")
    st.caption(
        "Compara a última compra com a penúltima e projeta o impacto mensal usando o consumo médio "
        "dos últimos 3 meses: (preço atual − preço anterior) × consumo médio. Ficam de fora os "
        "itens pendentes de aprovação na Auditoria e os bonificados (CFOP 5910/6910 ou R$ 0,00); "
        "variações de 50% ou mais aparecem como 'Verificar Variação Brusca', vão para o fim da "
        "tabela e são enviadas automaticamente para a Auditoria — não entram como aumento nem economia."
    )

    df_datas = df[df["data_emissao"].notna()]
    if df_datas.empty:
        st.warning("Sem datas de emissão válidas para calcular o impacto financeiro.")
        return

    loja = filtro_loja(df_datas, "r3_loja", lojas_cadastradas)
    produto = filtro_produto_relatorio(filtrar_por_loja(df_datas, loja), "r3")
    col_meses, _ = st.columns(2)
    with col_meses:
        meses_consumo = st.selectbox(
            "📦 Consumo médio calculado em",
            [1, 3, 6],
            index=1,
            key="r3_meses",
            help="Quantidade comprada na janela dividida pelo número de meses.",
        )

    parametros = {"loja": loja, "meses_consumo": meses_consumo}
    if produto:
        parametros["produto"] = produto
    dados, erro = carregar_relatorio(
        RELATORIO_IMPACTO_URL, json.dumps(parametros, sort_keys=True)
    )
    if erro:
        st.error(erro)
        return

    linhas = dados.get("linhas") or []
    totais = dados.get("totais") or {}
    st.markdown(CSS_METRICAS_IMPACTO, unsafe_allow_html=True)
    aumento = float(totais.get("aumento") or 0.0)
    economia = float(totais.get("economia") or 0.0)
    # impacto_liquido positivo = aumento de custo; negativo = economia.
    liquido = float(totais.get("impacto_liquido") or 0.0)
    m1, m2, m3 = st.columns(3)
    with m1:
        _metrica_impacto(
            "Aumento de Custo (mês)",
            aumento,
            -1,
            "Soma dos impactos mensais dos produtos que subiram de preço "
            "(variações de 50% ou mais ficam de fora).",
        )
    with m2:
        _metrica_impacto(
            "Economia (mês)",
            economia,
            1,
            "Soma da economia mensal dos produtos que caíram de preço "
            "(variações de 50% ou mais ficam de fora).",
        )
    with m3:
        _metrica_impacto(
            "Impacto Líquido (mês)",
            liquido,
            -1 if liquido > 0 else 1,
            "Economia menos o aumento de custo no período comparado, sem as aberrações. "
            "Verde = sobrou dinheiro; vermelho = custo maior.",
        )
    st.caption(
        f"Referência: {formatar_data(dados.get('referencia'))} | Consumo dos últimos "
        f"{dados.get('meses_consumo')} meses a partir de {formatar_data(dados.get('inicio_consumo'))} | "
        f"Produto: {dados.get('produto')}"
    )
    _aviso_integridade(dados)

    enviados = st.session_state.pop("r3_auditoria_enviada", 0)
    if enviados:
        st.success(
            f"{formatar_numero(enviados)} variação(ões) registrada(s) na Auditoria. Elas "
            "continuam visíveis aqui com o alerta, mas fora dos totais até a validação."
        )

    if not linhas:
        st.info(dados.get("aviso") or "Nenhum produto com pelo menos duas compras registradas.")
        return

    aberracoes = [linha for linha in linhas if linha.get("aberrante")]
    if aberracoes:
        info_aberracoes = dados.get("aberracoes") or {}
        limite_pct = info_aberracoes.get("limite_pct") or 50
        pendentes_auditoria = int(info_aberracoes.get("pendentes_auditoria") or 0)
        enviadas_agora = int(info_aberracoes.get("enviadas_auditoria") or 0)
        st.warning(
            f"⚠️ {formatar_numero(len(aberracoes))} produto(s) com variação de "
            f"{formatar_numero(limite_pct)}% ou mais entre a compra anterior e a atual. Isso "
            "costuma ser erro de unidade (caixa x unidade) ou de cadastro: NÃO entram como "
            "aumento nem economia e ficaram no fim da tabela com o alerta "
            f"'Verificar Variação Brusca'. Já foram enviados para a Auditoria "
            f"({formatar_numero(pendentes_auditoria)} aguardando validação"
            + (f", {formatar_numero(enviadas_agora)} agora" if enviadas_agora else "")
            + ")."
        )
        _painel_reenvio_auditoria(aberracoes)

    col_ordem, col_filtro = st.columns([3, 2])
    with col_ordem:
        rotulo_ordem = st.selectbox(
            "↕️ Ordenar por",
            list(ORDENACOES_IMPACTO),
            key="r3_ordem",
            help=(
                "As variações bruscas (Status 'Verificar Variação Brusca') ficam sempre no fim "
                "da tabela, independente da ordenação escolhida."
            ),
        )
    with col_filtro:
        apenas_aumentos = st.checkbox(
            "Mostrar apenas aumentos de preço", value=False, key="r3_aumentos"
        )
    if apenas_aumentos:
        linhas = [linha for linha in linhas if float(linha.get("impacto_mensal") or 0) > 0]
        if not linhas:
            st.info("Nenhum produto com aumento de preço na comparação atual.")
            return

    meses = dados.get("meses_consumo") or 3
    ordenacao = ORDENACOES_IMPACTO[rotulo_ordem]
    _renderizar_tabela_impacto(linhas, meses, ordenacao)
    _planilha_impacto(linhas, meses)
    st.caption(
        f"Ordenado por **{rotulo_ordem}** e exibindo {formatar_numero(len(linhas))} de "
        f"{formatar_numero(dados.get('total_linhas'))} produtos com duas ou mais compras. "
        "Na coluna Variação (%): 🔴 vermelho = preço caiu, 🟢 verde = preço subiu. Passe o "
        "mouse em Preço Atual/Preço Anterior para ver a data e o fornecedor de cada compra."
    )

    # A tabela HTML (com tooltip de data em cada preço) é desenhada por
    # _renderizar_tabela_impacto — o st.dataframe não permite tooltip por célula.


def renderizar_relatorios(df, lojas_cadastradas):
    """Página do cliente: indicadores gerais e os três relatórios em abas.

    `lojas_cadastradas` são TODOS os CNPJs da conta: os filtros de loja das abas listam
    o grupo inteiro ("Minhas Lojas (Todas)" + cada CNPJ, mesmo o que ainda não tem nota
    importada). O recorte por loja é escolhido dentro de cada aba.
    """
    st.title("📊 Relatórios de Preços")
    lojas = [
        loja for loja in (lojas_cadastradas or []) if loja and loja.get("nome_loja")
    ]
    st.caption(
        f"🏢 **{ROTULO_EMPRESA_TODAS}** ({len(lojas)} CNPJ(s)) — evolução de preços, "
        "comparativo entre lojas e impacto financeiro. Use o filtro de loja de cada aba "
        "para analisar um CNPJ específico."
    )

    datas = df["data_emissao"].dropna()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Registros Analisados", formatar_numero(len(df)))
    m2.metric("Valor Total Movimentado", formatar_moeda(float(df["valor_total"].sum())))
    m3.metric("Lojas com Dados", formatar_numero(df["nome_loja"].nunique()))
    m4.metric("Período Analisado", f"{datas.min():%m/%Y} a {datas.max():%m/%Y}" if not datas.empty else "—")

    aba_evolucao, aba_comparativo, aba_impacto = st.tabs(
        [
            "📈 Evolução de Preços",
            "🏪 Comparativo de Lojas",
            "💰 Impacto Financeiro",
        ]
    )
    with aba_evolucao:
        relatorio_evolucao_precos(df, lojas_cadastradas)
    with aba_comparativo:
        relatorio_comparativo_lojas(df, lojas_cadastradas)
    with aba_impacto:
        relatorio_impacto_financeiro(df, lojas_cadastradas)


# ──────────────────────────────────────────────────────────────────────────────
# Página de auditoria (admin)
# ──────────────────────────────────────────────────────────────────────────────
def _botao_aprovar_pendente(chave, prefixo):
    """Botão de aprovação de uma pendência (libera a chave na auditoria)."""
    if st.button("✅ Validar", key=f"{prefixo}_{abs(hash(chave))}", width="stretch"):
        try:
            resposta = requests.post(
                AUDIT_URL,
                json={"chave_identificacao": chave},
                headers=cabecalhos_api(),
                timeout=30,
            )
        except Exception as erro:
            st.error(f"Erro de conexão com o backend: {erro}")
        else:
            if resposta.status_code == 200:
                st.cache_data.clear()
                st.success("Validado! O item foi liberado na Auditoria.")
                st.rerun()
            else:
                st.error(f"Erro ao validar ({resposta.status_code}).")


def _remover_pendencia(chave):
    """Apaga a chave pendente (usado depois de mover a descrição para outro grupo)."""
    try:
        requests.delete(
            f"{AUDIT_URL}/{quote(str(chave), safe='')}",
            headers=cabecalhos_api(),
            timeout=30,
        )
    except Exception:
        pass


def _mapas_de_regras(regras_agrupamento):
    """(mapa de grupos, mapa de descrições) das regras de-para salvas na Auditoria."""
    mapa_regras, mapa_descricoes = {}, {}
    for regra in regras_agrupamento or []:
        origem = regra.get("chave_origem")
        destino = regra.get("chave_destino")
        if not origem or not destino:
            continue
        if origem.startswith("DESC_"):
            mapa_descricoes[origem] = destino
        else:
            mapa_regras[origem] = destino
    return mapa_regras, mapa_descricoes


def _painel_mover_pendente(df, chave, descricoes_conhecidas):
    """Seletor de destino para mover UMA descrição pendente (variação brusca).

    Grava uma regra de-para do tipo "mover": a descrição sai do grupo em que apareceu e
    passa a ser uma variação do produto escolhido (já aprovada lá). A pendência antiga é
    apagada, então o alerta de variação brusca sai da lista e o item aparece no produto
    correto nos relatórios.
    """
    descricao = (
        descrever_descricao_da_regra(chave, descricoes_conhecidas)
        or descrever_chave_pendente(chave)
    )
    mapa_regras, mapa_descricoes = _mapas_de_regras(carregar_regras_agrupamento())
    grupos = montar_grupos(df, mapa_regras, mapa_descricoes, set(carregar_auditados()))
    opcoes = {}
    for grupo in grupos:
        if chave_descricao(grupo["chave"], descricao) == chave:
            continue  # não faz sentido mover para o próprio grupo
        rotulo = f"{grupo['nome_padrao']} | EAN {grupo['ean']} | Cap. {grupo['capitulo']}"
        opcoes.setdefault(rotulo, grupo)

    st.info(
        f"➡️ Escolha o produto correto de **{descricao}**. A descrição passa a ser uma "
        "variação do grupo de destino (já aprovada lá) e o alerta de variação brusca sai "
        "desta lista."
    )
    if not opcoes:
        st.warning("Não há outro grupo de produto disponível para receber esta descrição.")
        if st.button("Fechar", key=f"btn_fechar_mover_{abs(hash(chave))}"):
            st.session_state.pop(CHAVE_MOVER_PENDENTE, None)
            st.rerun()
        return

    escolha = st.selectbox(
        "Produto de destino:",
        list(opcoes),
        index=None,
        placeholder="Digite parte da descrição para localizar o produto...",
        key=f"sel_mover_pendente_{abs(hash(chave))}",
    )
    col_mover, col_cancelar = st.columns(2)
    with col_mover:
        confirmar = st.button(
            "✅ Mover descrição",
            key=f"btn_conf_mover_pendente_{abs(hash(chave))}",
            type="primary",
            disabled=escolha is None,
            width="stretch",
        )
    with col_cancelar:
        cancelar = st.button(
            "Cancelar", key=f"btn_cancel_mover_pendente_{abs(hash(chave))}", width="stretch"
        )

    if cancelar:
        st.session_state.pop(CHAVE_MOVER_PENDENTE, None)
        st.rerun()
    if confirmar and escolha:
        sucesso, mensagem = salvar_agrupamento(chave, opcoes[escolha], "mover", [descricao])
        if sucesso:
            _remover_pendencia(chave)
            st.session_state.pop(CHAVE_MOVER_PENDENTE, None)
            st.success(mensagem)
            st.rerun()
        else:
            st.error(mensagem)


def _carregar_sugestoes_ia(status_aprovacao="pendente"):
    """GET das sugestões da IA. Devolve (lista, erro)."""
    try:
        resposta = requests.get(
            IA_SUGESTOES_URL,
            params={"status_aprovacao": status_aprovacao} if status_aprovacao else None,
            headers=cabecalhos_api(),
            timeout=60,
        )
    except Exception as erro:
        return [], f"Erro de conexão com o backend: {erro}"
    if resposta.status_code != 200:
        return [], mensagem_de_erro(resposta)
    try:
        corpo = resposta.json()
    except Exception:
        return [], "O backend devolveu uma resposta inválida ao listar as sugestões."
    sugestoes = corpo.get("sugestoes") if isinstance(corpo, dict) else None
    return (sugestoes or []), None


def _painel_aplicar_sugestao_ia(df, sugestao):
    """Seletor de destino da sugestão da IA.

    Usa as MESMAS opções do "Mover" da Auditoria: qualquer grupo já existente
    (produto | EAN | capítulo) ou ``OPCAO_CRIAR_GRUPO`` para isolar a descrição.
    A escolha é enviada ao backend no corpo de `/api/ia/aplicar-sugestao/{id}`.
    """
    sugestao_id = sugestao.get("id")
    item = str(sugestao.get("item_sugerido_sair") or "").strip()
    grupo_origem = str(sugestao.get("grupo_origem") or "").strip()

    st.info(
        f"➡️ Escolha para onde mover **{item}**. Selecione um grupo que já existe ou "
        f"**{OPCAO_CRIAR_GRUPO}** para isolar a descrição em um card novo. A descrição "
        "passa a ser uma variação do destino e nenhuma compra é apagada."
    )
    st.caption(f"Grupo de origem: `{grupo_origem}`")

    grupos = []
    if df is not None and not getattr(df, "empty", True):
        mapa_regras, mapa_descricoes = _mapas_de_regras(carregar_regras_agrupamento())
        grupos = montar_grupos(
            df, mapa_regras, mapa_descricoes, set(carregar_auditados())
        )
    else:
        st.warning(
            "Não foi possível carregar os grupos do catálogo; só é possível criar "
            "um grupo novo para esta descrição."
        )

    opcoes = {}
    for grupo in grupos:
        if grupo["chave"] == grupo_origem:
            continue  # não faz sentido mover para o próprio grupo
        rotulo = f"{grupo['nome_padrao']} | EAN {grupo['ean']} | Cap. {grupo['capitulo']}"
        opcoes.setdefault(rotulo, grupo)

    escolha = st.selectbox(
        "Grupo de destino:",
        [OPCAO_CRIAR_GRUPO] + list(opcoes),
        index=0,
        key=f"sel_ia_destino_{sugestao_id}",
        help="A lista é a mesma da Auditoria: produto, EAN e capítulo do grupo.",
    )
    if escolha == OPCAO_CRIAR_GRUPO:
        st.caption("Será criado um grupo novo apenas com esta descrição.")
    else:
        st.caption(f"Destino: **{opcoes[escolha]['nome_padrao']}**")

    col_aplicar, col_cancelar = st.columns(2)
    with col_aplicar:
        confirmar = st.button(
            "✅ Aplicar sugestão",
            key=f"btn_ia_confirmar_destino_{sugestao_id}",
            type="primary",
            width="stretch",
        )
    with col_cancelar:
        cancelar = st.button(
            "Cancelar",
            key=f"btn_ia_cancelar_destino_{sugestao_id}",
            width="stretch",
        )

    if cancelar:
        st.session_state.pop(CHAVE_IA_SUGESTAO_DESTINO, None)
        st.rerun()
    if not confirmar:
        return

    payload = (
        {}
        if escolha == OPCAO_CRIAR_GRUPO
        else {"chave_destino": opcoes[escolha]["chave"]}
    )
    try:
        resposta = requests.post(
            f"{IA_APLICAR_SUGESTAO_URL}/{sugestao_id}",
            json=payload,
            headers=cabecalhos_api(),
            timeout=180,
        )
    except Exception as erro_http:
        st.error(f"Erro de conexão com o backend: {erro_http}")
        return
    if resposta.status_code != 200:
        st.error(mensagem_de_erro(resposta))
        return

    st.cache_data.clear()
    try:
        mensagem = resposta.json().get("mensagem") or "Sugestão aplicada."
    except Exception:
        mensagem = "Sugestão aplicada."
    st.session_state[CHAVE_IA_SUGESTAO_FLASH] = mensagem
    st.session_state.pop(CHAVE_IA_SUGESTAO_DESTINO, None)
    st.rerun()


def renderizar_sugestoes_ia(df):
    """Sub-aba 🤖 Sugestões da IA da Auditoria.

    A IA só grava sugestões. Nenhum item é movido até o administrador clicar
    em "Aplicar"; "Ignorar" mantém a base intacta e impede a sugestão de voltar
    na próxima rodada.
    """
    st.header("🤖 Sugestões da IA")
    st.caption(
        "A IA analisa os grupos com 2+ descrições e sugere qual item deve sair. "
        "**Nada é alterado na base sem o seu clique**: Aplicar abre o seletor de "
        "destino (grupo existente ou grupo novo); Ignorar descarta a sugestão."
    )
    flash = st.session_state.pop(CHAVE_IA_SUGESTAO_FLASH, None)
    if flash:
        st.success(flash)

    resultado = st.session_state.get("_ia_auditoria_resultado")
    if resultado:
        if resultado.get("erro"):
            st.error(resultado["erro"])
        else:
            st.success(
                f"Auditoria concluída: {formatar_numero(resultado.get('grupos_analisados', 0))} "
                f"grupo(s) analisado(s), {formatar_numero(resultado.get('sugestoes_criadas', 0))} "
                f"sugestão(ões) nova(s) e {formatar_numero(resultado.get('sugestoes_atualizadas', 0))} "
                "atualizada(s)."
            )
            if resultado.get("restantes"):
                st.info(
                    f"Ainda restam {formatar_numero(resultado['restantes'])} grupo(s). "
                    "Clique em Auditar novamente para continuar."
                )
            erros = resultado.get("erros") or []
            if erros:
                with st.expander(f"⚠️ {len(erros)} grupo(s) não puderam ser analisados"):
                    for erro in erros:
                        st.write(f"`{erro.get('grupo_origem', '')}` — {erro.get('erro', '')}")

    col_rodada, col_botao = st.columns([1.2, 2])
    with col_rodada:
        opcao_limite = st.selectbox(
            "Grupos por rodada:",
            ["Todos", "10", "25", "50"],
            index=0,
            key="ia_auditoria_limite",
            help="Limitar a rodada reduz o tempo e o custo da IA. Você pode repetir até acabar.",
        )
    with col_botao:
        st.write("")
        auditar = st.button(
            "🤖 Auditar base completa com IA",
            key="btn_ia_auditar_base",
            type="primary",
            width="stretch",
        )
    if auditar:
        payload = {} if opcao_limite == "Todos" else {"limite": int(opcao_limite)}
        with st.spinner("Consultando o DeepSeek grupo a grupo... isso pode levar alguns minutos."):
            try:
                resposta = requests.post(
                    IA_AUDITAR_BASE_URL,
                    json=payload,
                    headers=cabecalhos_api(),
                    timeout=1800,
                )
            except Exception as erro:
                st.session_state["_ia_auditoria_resultado"] = {
                    "erro": f"Erro de conexão com o backend: {erro}"
                }
            else:
                if resposta.status_code == 200:
                    st.session_state["_ia_auditoria_resultado"] = resposta.json()
                else:
                    st.session_state["_ia_auditoria_resultado"] = {
                        "erro": mensagem_de_erro(resposta)
                    }
        st.rerun()

    sugestoes, erro = _carregar_sugestoes_ia("pendente")
    if erro:
        st.warning(erro)
        return
    if not sugestoes:
        st.info(
            "Nenhuma sugestão pendente. Rode a auditoria com IA para procurar grupos "
            "com descrições possivelmente misturadas."
        )
        return

    sugestao_aberta = st.session_state.get(CHAVE_IA_SUGESTAO_DESTINO)
    if sugestao_aberta is not None:
        selecionada = next(
            (s for s in sugestoes if s.get("id") == sugestao_aberta), None
        )
        if selecionada is None:
            st.session_state.pop(CHAVE_IA_SUGESTAO_DESTINO, None)
        else:
            _painel_aplicar_sugestao_ia(df, selecionada)
            return

    st.subheader(f"📝 {formatar_numero(len(sugestoes))} sugestão(ões) aguardando aprovação")
    for sugestao in sugestoes:
        sugestao_id = sugestao.get("id")
        with st.container(border=True):
            st.markdown(
                "**Item sugerido para sair:** "
                + html.escape(str(sugestao.get("item_sugerido_sair") or ""))
            )
            st.caption(f"Grupo de origem: `{sugestao.get('grupo_origem') or ''}`")
            motivo = str(sugestao.get("motivo_ia") or "").strip()
            if motivo:
                st.markdown(f"**Motivo da IA:** {motivo}")
            st.caption(
                "Aplicar = escolher o grupo de destino (existente ou novo), sem apagar "
                "compras. Ignorar = manter tudo como está."
            )
            col_aplicar, col_ignorar = st.columns(2)
            with col_aplicar:
                if st.button(
                    "✅ Aplicar",
                    key=f"btn_ia_aplicar_{sugestao_id}",
                    type="primary",
                    width="stretch",
                ):
                    st.session_state[CHAVE_IA_SUGESTAO_DESTINO] = sugestao_id
                    st.rerun()
            with col_ignorar:
                if st.button(
                    "❌ Ignorar",
                    key=f"btn_ia_ignorar_{sugestao_id}",
                    width="stretch",
                ):
                    try:
                        resposta = requests.post(
                            f"{IA_IGNORAR_SUGESTAO_URL}/{sugestao_id}",
                            headers=cabecalhos_api(),
                            timeout=60,
                        )
                    except Exception as erro_http:
                        st.error(f"Erro de conexão com o backend: {erro_http}")
                    else:
                        if resposta.status_code == 200:
                            st.rerun()
                        else:
                            st.error(mensagem_de_erro(resposta))


def _contexto_local_aberrante(df, chave):
    """Fallback do contexto: última/penúltima compra da descrição pendente.

    Usado quando a variação já não aparece no relatório (ex.: a base ganhou uma compra
    nova e o produto deixou de estar em 50%). Mantém os mesmos campos exibidos pelo
    contexto vindo de `/api/relatorio/impacto`.
    """
    if df is None or df.empty or "descricao_produto" not in df.columns:
        return {}
    descricao = descrever_chave_pendente(chave)
    alvo = limpar_texto(descricao)
    if not alvo:
        return {}
    iguais = df[
        df["descricao_produto"].fillna("").map(limpar_texto).to_numpy() == alvo
    ]
    if iguais.empty:
        return {}
    iguais = iguais.assign(
        _data=pd.to_datetime(iguais["data_emissao"], errors="coerce")
    ).dropna(subset=["_data"])
    if iguais.empty:
        return {}
    iguais = iguais.sort_values(["_data", "id"] if "id" in iguais.columns else ["_data"])
    ultima = iguais.iloc[-1]
    contexto = {
        "produto": str(ultima.get("descricao_produto") or descricao),
        "data_atual": ultima.get("_data") or ultima.get("data_emissao"),
        "preco_atual": ultima.get("valor_unitario"),
        "fornecedor_atual": ultima.get("fornecedor") or "Fornecedor não informado",
        # Item da última compra: alimenta o botão "Ver DANFE".
        "item_id": ultima.get("id"),
    }
    if len(iguais) >= 2:
        anterior = iguais.iloc[-2]
        contexto.update(
            {
                "data_anterior": anterior.get("_data")
                or anterior.get("data_emissao"),
                "preco_anterior": anterior.get("valor_unitario"),
                "fornecedor_anterior": anterior.get("fornecedor")
                or "Fornecedor não informado",
            }
        )
    return contexto


def _renderizar_pendencias_manuais():
    """Pendências MANUAIS (motivo ``manual``) que saem dos relatórios.

    As variações bruscas (motivo ``aberrante``) não têm seção própria: elas aparecem
    na própria lista de grupos da Auditoria de Divergências, com os dois preços em
    vermelho e os botões "Ver DANFE" ao lado de cada compra.
    """
    manuais = carregar_pendentes_auditoria("manual")
    if not manuais:
        return
    with st.expander(
        f"⏳ {formatar_numero(len(manuais))} item(ns) pendentes marcados manualmente",
        expanded=True,
    ):
        st.caption(
            "Itens enviados à mão pela aba Impacto Financeiro. Enquanto estiverem "
            "pendentes eles NÃO entram nos relatórios de preços. Validar devolve o item "
            "aos cálculos."
        )
        for chave in manuais:
            col_desc, col_acao = st.columns([4, 1])
            col_desc.write(f"**{descrever_chave_pendente(chave)}**")
            with col_acao:
                _botao_aprovar_pendente(chave, "validar_manual")


def _portao_2fa_auditoria():
    """Portão de 2FA da Auditoria. True = dados liberados nesta sessão.

    - Código já confirmado (token 2FA válido) → libera;
    - 2FA ativo e sessão sem o token → pede o código de 6 dígitos;
    - 2FA ainda não configurado → mostra o QR Code e ativa no primeiro código válido.
    """
    if _2fa_desbloqueado():
        st.success("🔐 2FA verificado nesta sessão — Auditoria liberada.")
        if (usuario_logado() or {}).get("totp_ativo"):
            st.caption(
                "A verificação vale por 2 horas. Use **Bloquear** para pedir o código "
                "novamente sem sair da conta."
            )
            if st.button("🔒 Bloquear auditoria", key="btn_bloquear_2fa"):
                _limpar_sessao_2fa()
                st.rerun()
        return True

    usuario = usuario_logado() or {}
    st.subheader("🔐 Autenticação em dois fatores (2FA)")
    if usuario.get("totp_ativo"):
        st.caption(
            "A Auditoria é protegida por 2FA. Informe o código de 6 dígitos do aplicativo "
            "autenticador para liberar esta sessão."
        )
        dados = formulario_codigo_2fa("auditoria", rotulo="Desbloquear Auditoria")
        if dados:
            _guardar_sessao_2fa(dados)
            st.rerun()
        return False

    st.info(
        "A Auditoria exige autenticação em dois fatores. Escaneie o QR Code no aplicativo "
        "autenticador (Google Authenticator, Authy, Microsoft Authenticator...) e confirme "
        "o código de 6 dígitos para ativar."
    )
    setup, erro = carregar_setup_2fa()
    if erro:
        st.error(erro)
        return False
    if setup.get("ja_configurado"):
        # O backend já tem 2FA ativo (configurado em outra sessão): só pede o código.
        st.session_state[SESSAO_USUARIO] = {
            **(usuario_logado() or {}),
            "totp_ativo": True,
        }
        st.rerun()

    segredo = setup.get("segredo") or ""
    qr_code = setup.get("qr_code") or ""
    col_qr, col_passos = st.columns([1, 2])
    with col_qr:
        if qr_code.startswith("data:image"):
            st.image(base64.b64decode(qr_code.split(",", 1)[-1]), width=210)
        else:
            st.warning("Não foi possível desenhar o QR Code. Use o segredo manual abaixo.")
    with col_passos:
        st.markdown(
            "1. Abra o aplicativo autenticador e escolha **Adicionar conta**;\n"
            "2. Escaneie o QR Code ao lado (ou digite o segredo abaixo);\n"
            "3. Informe o código de 6 dígitos para ativar o 2FA e liberar a Auditoria."
        )
        st.caption(f"Segredo para digitação manual: `{segredo}`")
        st.caption(f"Emissor: {setup.get('emissor')} | Conta: {setup.get('conta')}")

    dados = formulario_codigo_2fa(
        "setup_auditoria", rotulo="Ativar 2FA e liberar a Auditoria"
    )
    if dados:
        _guardar_sessao_2fa(dados)
        st.rerun()

    with st.expander("🔄 Trocar de aparelho (gerar um QR Code novo)"):
        st.caption(
            "O segredo atual deixa de valer e o 2FA fica inativo até você confirmar o código "
            "do novo aparelho."
        )
        if st.button("Gerar novo QR Code", key="btn_reiniciar_2fa"):
            carregar_setup_2fa(reiniciar=True)
            st.rerun()
    return False


def renderizar_exportacao_base_completa():
    """Aba de exportação: raio-x dos grupos + catálogo MASTER (admin + 2FA).

    A varredura acontece no backend; o front só guarda os bytes em memória para o
    `st.download_button`. Os botões de preparar são separados dos downloads porque
    gerar cada arquivo percorre a base inteira — não deve rodar a cada rerun da tela.
    """
    st.header("📥 Exportar Base Completa para Excel")
    st.caption(
        "Raio-x de auditoria: todos os grupos de produto da base, inclusive os que não "
        "aparecem na tela por já estarem aprovados ou por não caberem nos filtros."
    )
    st.markdown(
        "**O que o arquivo contém**\n"
        "1. Varre **todas** as notas importadas (não só a janela dos relatórios).\n"
        "2. Agrupa pela mesma **chave inteligente** da Auditoria (EAN válido ou nome + "
        "regras de juntar/mover).\n"
        "3. Mantém apenas grupos com **2 ou mais descrições únicas** — ou seja, grupos "
        "com variações de nome.\n"
        "4. Colunas: **Produto Principal**, **EAN**, **NCM** e **Lista de Descrições** "
        "(todas as variações do grupo, separadas por `;`)."
    )

    if st.button(
        "🔄 Preparar arquivo da base completa",
        key="btn_preparar_exportacao_grupos",
        type="primary",
        width="stretch",
        help="Varre a base inteira no backend e monta o Excel em memória.",
    ):
        with st.spinner("Varrendo toda a base e montando o Excel..."):
            conteudo, nome_arquivo, total_grupos, erro = carregar_exportacao_grupos_completos()
        if erro:
            st.session_state.pop(CHAVE_EXPORTACAO_GRUPOS, None)
            st.error(erro)
        else:
            st.session_state[CHAVE_EXPORTACAO_GRUPOS] = {
                "conteudo": conteudo,
                "nome": nome_arquivo,
                "total": total_grupos,
                "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            }
            st.success(
                f"Arquivo pronto: {formatar_numero(total_grupos)} grupo(s) com 2+ "
                "descrições. Use o botão abaixo para baixar."
            )

    exportacao = st.session_state.get(CHAVE_EXPORTACAO_GRUPOS)
    if exportacao:
        st.download_button(
            "📥 Exportar Base Completa para Excel",
            data=exportacao.get("conteudo") or b"",
            file_name=exportacao.get("nome") or "grupos_com_variacoes.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            key="btn_exportar_grupos_completos",
            type="primary",
            width="stretch",
        )
        total = int(exportacao.get("total") or 0)
        st.caption(
            f"{formatar_numero(total)} grupo(s) com variações | gerado em "
            f"{exportacao.get('gerado_em') or '—'}. O download não consulta o backend "
            "de novo: o arquivo já está pronto na memória."
        )
    else:
        st.info(
            "Clique em **Preparar arquivo da base completa** para gerar o Excel. "
            "A varredura é feita no backend e pode levar alguns segundos."
        )

    st.divider()
    st.header("📥 Exportar Catálogo Completo (Excel)")
    st.caption(
        "Listagem MASTER de **todas** as descrições da base, sem exceção: inclusive "
        "produtos com uma única descrição. O agrupamento é por **Produto + Marca** "
        "(ex.: Polenta Palito (Arrico) x Polenta Palito (El Chef))."
    )
    st.markdown(
        "**Colunas do arquivo**\n"
        "1. `Produto (Marca)` — nome do produto sem a marca e sem números de controle, "
        "com a marca entre parênteses.\n"
        "2. `Marca` — a marca extraída (ou `SEM MARCA`), pronta para filtrar no Excel.\n"
        "3. `EAN` — código de barras (texto, preserva zeros à esquerda).\n"
        "4. `NCM` — NCM do item.\n"
        "5. `Descrição Original` — o texto exato da NFe, sem alterações."
    )

    if st.button(
        "🔄 Preparar catálogo completo",
        key="btn_preparar_exportacao_catalogo",
        type="primary",
        width="stretch",
        help="Varre a base inteira e monta o catálogo MASTER em Excel.",
    ):
        with st.spinner("Varrendo a base e montando o catálogo completo..."):
            (
                conteudo,
                nome_arquivo,
                total_produtos,
                total_descricoes,
                erro,
            ) = carregar_exportacao_catalogo_completo()
        if erro:
            st.session_state.pop(CHAVE_EXPORTACAO_CATALOGO, None)
            st.error(erro)
        else:
            st.session_state[CHAVE_EXPORTACAO_CATALOGO] = {
                "conteudo": conteudo,
                "nome": nome_arquivo,
                "total_produtos": total_produtos,
                "total_descricoes": total_descricoes,
                "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            }
            st.success(
                f"Catálogo pronto: {formatar_numero(total_produtos)} produto(s) e "
                f"{formatar_numero(total_descricoes)} descrição(ões). Use o botão "
                "abaixo para baixar."
            )

    catalogo = st.session_state.get(CHAVE_EXPORTACAO_CATALOGO)
    if catalogo:
        st.download_button(
            "📥 Exportar Catálogo Completo (Excel)",
            data=catalogo.get("conteudo") or b"",
            file_name=catalogo.get("nome") or "catalogo_completo.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            key="btn_exportar_catalogo_completo",
            type="primary",
            width="stretch",
        )
        st.caption(
            f"{formatar_numero(catalogo.get('total_produtos') or 0)} produto(s) | "
            f"{formatar_numero(catalogo.get('total_descricoes') or 0)} descrição(ões) | "
            f"gerado em {catalogo.get('gerado_em') or '—'}."
        )
    else:
        st.info(
            "Clique em **Preparar catálogo completo** para gerar o Excel com TODOS os "
            "itens da base."
        )


def renderizar_auditoria(df):
    st.title("🔍 Auditoria de Produtos (Admin)")
    st.caption(
        "Área interna de administração do catálogo: aprove as variações de descrição e agrupe "
        "produtos iguais. Acesso restrito às contas administradoras (`is_admin`) **com 2FA**: "
        "o backend responde 403 para contas comuns e 401 sem o código do autenticador."
    )

    # Portão do segundo fator: sem o token 2FA (ou com o 2FA ainda não configurado) nada
    # de dados de auditoria é carregado — e as rotas responderiam 401/403 de qualquer forma.
    if not _portao_2fa_auditoria():
        st.stop()

    itens_auditados_lista = carregar_auditados()
    itens_auditados = set(itens_auditados_lista)
    regras_agrupamento = carregar_regras_agrupamento()

    aba1, aba2, aba3, aba4, aba5, aba6 = st.tabs(
        [
            "🔍 Auditoria de Divergências",
            "📈 Resumo Geral",
            "🧠 Aprendizado da IA (Marcas)",
            "⚙️ Configurações",
            "📥 Exportar Base",
            "🤖 Sugestões da IA",
        ]
    )

    with aba1:
        st.header("Auditoria de Divergências NFe")
        st.caption("Valide as variações de nomes. As descrições aprovadas exibirão o selo verde.")

        # Variações bruscas (>= 50%) entram NESTA mesma lista de grupos, junto com as
        # divergências de descrição. Não existe seção/aba separada de "Variação Brusca".
        aberracoes_pendentes = {
            str(chave) for chave in carregar_pendentes_auditoria("aberrante")
        }

        modo_exibicao = st.radio(
            "Modo de exibição:",
            MODOS_EXIBICAO,
            horizontal=True,
            help=(
                "'Mostrar Apenas Pendentes' esconde as descrições já aprovadas. 'Mostrar Todos' permite "
                "desfazer aprovações e revisar grupos já finalizados."
            ),
        )
        mostrar_todos = modo_exibicao == "Mostrar Todos"

        col_filtro1, col_filtro2 = st.columns([3, 1])
        with col_filtro1:
            termo_busca = st.text_input("🔎 Pesquisar produto (Nome ou EAN):", "").strip().upper()
        with col_filtro2:
            st.metric("Nomes Aprovados", len(itens_auditados_lista))

        st.caption(
            "ℹ️ O grupo do produto só desaparece da lista quando **todas** as suas descrições estiverem aprovadas."
        )

        df_trabalho = df.copy()
        if termo_busca:
            df_trabalho = df_trabalho[
                df_trabalho["descricao_produto"].str.upper().str.contains(termo_busca, na=False)
                | df_trabalho["ean"].astype(str).str.upper().str.contains(termo_busca, na=False)
            ]

        # O aviso é calculado sobre o MESMO recorte que alimenta a lista (inclusive a
        # busca): assim ele nunca anuncia uma quantidade que a lista não mostra.
        descricoes_aberrantes = _descricoes_de_chaves_pendentes(
            aberracoes_pendentes,
            df_trabalho["descricao_produto"].dropna().unique().tolist()
            if "descricao_produto" in df_trabalho.columns
            else [],
        )
        if descricoes_aberrantes:
            st.warning(
                f"🔥 **{formatar_numero(len(descricoes_aberrantes))} descrição(ões)** com "
                "variação de 50% ou mais aparecem destacadas na lista abaixo, com os "
                "preços da penúltima e da última compra em vermelho."
            )

        grupos_exibidos = 0

        if df_trabalho.empty:
            st.info("Nenhum registro encontrado para a busca.")
        else:
            mapa_regras, mapa_descricoes = _mapas_de_regras(regras_agrupamento)
            todos_grupos = montar_grupos(df_trabalho, mapa_regras, mapa_descricoes, itens_auditados)
            # Grupo com variação brusca pendente entra na lista MESMO que a regra de
            # ouro o escondesse (ex.: uma única descrição com duas compras).
            chaves_com_aberr = {
                grupo["chave"]
                for grupo in todos_grupos
                if any(
                    limpar_texto(str(descricao)) in descricoes_aberrantes
                    for descricao in grupo["descricoes_unicas"]
                )
            }
            grupos_auditaveis = [g for g in todos_grupos if grupo_e_excecao(g)]
            chaves_auditaveis = {grupo["chave"] for grupo in grupos_auditaveis}
            grupos_visiveis = [
                g
                for g in todos_grupos
                if g["chave"] in chaves_auditaveis or g["chave"] in chaves_com_aberr
            ]
            grupos_ocultos = len(todos_grupos) - len(grupos_visiveis)
            descricoes_conhecidas = df["descricao_produto"].dropna().unique().tolist()

            incluir_ocultos = st.checkbox(
                f"🔓 Incluir os {formatar_numero(grupos_ocultos)} grupos ocultos pela regra de ouro"
                if grupos_ocultos
                else "🔓 Incluir grupos ocultos pela regra de ouro",
                value=False,
                help=(
                    "Por padrão a auditoria mostra só exceções: grupos SEM EAN com 2+ descrições "
                    f"que tenham menos de {LIMIAR_SIMILARIDADE_SEM_EAN * 100:.0f}% de similaridade "
                    "entre si e grupos COM EAN válido cuja descrição difira do padrão "
                    f"(similaridade < {LIMIAR_SIMILARIDADE_EAN * 100:.0f}% da descrição mais antiga). "
                    "A similaridade é híbrida: caracteres (difflib) + palavras principais em comum, "
                    "ignorando a ordem das palavras. Mesmo com o nome igual, o grupo aparece "
                    "quando uma descrição diz COM e a outra SEM ou quando as medidas divergem "
                    f"(peso/volume/comprimento, com tolerância de {TOLERANCIA_MEDIDA:g} na unidade base). "
                    "Marque para revisar também os grupos já padronizados."
                ),
            )
            grupos_base = todos_grupos if incluir_ocultos else grupos_visiveis

            # Alerta de duplicidade: grupos distintos com a mesma descrição / >= 95% de similaridade.
            duplicidades = detectar_grupos_duplicados(todos_grupos)
            chaves_exibidas = {g["chave"] for g in grupos_base}
            duplicidades_exibidas = {c: s for c, s in duplicidades.items() if c in chaves_exibidas}
            if duplicidades_exibidas:
                st.warning(
                    f"⚠️ **{formatar_numero(len(duplicidades_exibidas))} grupo(s)** têm descrição repetida "
                    f"em outro grupo (ou similaridade ≥ {LIMIAR_DUPLICIDADE * 100:.0f}%). O aviso aparece no "
                    "topo do card do produto, com o botão para juntar os dois grupos em 1 clique."
                )

            st.caption(
                f"🛡️ **Regra de ouro:** {formatar_numero(len(grupos_visiveis))} de "
                f"{formatar_numero(len(todos_grupos))} grupos precisam de auditoria "
                f"({formatar_numero(grupos_ocultos)} ocultos). "
                f"**Com EAN** (confiança no código de barras): só exibe abaixo de "
                f"{LIMIAR_SIMILARIDADE_EAN * 100:.0f}% de similaridade com a descrição padrão; "
                f"**sem EAN** (rigor no texto): exige 2+ descrições com menos de "
                f"{LIMIAR_SIMILARIDADE_SEM_EAN * 100:.0f}% entre si. "
                f"Descrições com {LIMIAR_TOKENS_MESMO_PRODUTO * 100:.0f}%+ das palavras principais "
                "em comum (ordem ignorada) são tratadas como o mesmo produto — exceto "
                "quando uma diz COM e a outra SEM (ex.: com osso x sem osso) ou quando as "
                "medidas divergem (ex.: 5KG x 1KG, 1L x 900ML)."
            )

            # Lista final desta visão: pendentes (padrão) ou todos os grupos. A paginação
            # entra DEPOIS do filtro para não renderizar milhares de cards de uma vez —
            # cada card monta histórico, botões e consultas, o que deixava Aprovar/Mover lento.
            produtos_base = [
                g
                for g in grupos_base
                if mostrar_todos
                or g["chave"] in chaves_com_aberr
                or len(g["descricoes_pendentes"]) >= 1
            ]
            grupos_exibidos = len(produtos_base)

            col_pagina_tamanho, col_pagina_info, col_pagina_nav = st.columns(
                [1.05, 2.35, 1.6]
            )
            with col_pagina_tamanho:
                tamanho_pagina = st.selectbox(
                    "Grupos por página:",
                    [25, 50, 100],
                    index=1,
                    key="auditoria_grupos_por_pagina",
                    help="A paginação vale para a lista já filtrada (pendentes ou todos).",
                )
            total_paginas = max(
                1, (len(produtos_base) + tamanho_pagina - 1) // tamanho_pagina
            )
            pagina_salva = st.session_state.get("auditoria_pagina", 1)
            try:
                pagina_salva = int(pagina_salva)
            except (TypeError, ValueError):
                pagina_salva = 1
            pagina_atual = min(max(pagina_salva, 1), total_paginas)
            st.session_state["auditoria_pagina"] = pagina_atual
            inicio_pagina = (pagina_atual - 1) * tamanho_pagina
            produtos_pagina = produtos_base[
                inicio_pagina : inicio_pagina + tamanho_pagina
            ]
            with col_pagina_info:
                if produtos_pagina:
                    st.caption(
                        f"Exibindo {formatar_numero(inicio_pagina + 1)}–"
                        f"{formatar_numero(inicio_pagina + len(produtos_pagina))} de "
                        f"{formatar_numero(len(produtos_base))} grupo(s) · "
                        f"página **{pagina_atual}** de **{total_paginas}**"
                    )
                else:
                    st.caption("Nenhum grupo para exibir neste filtro.")
            with col_pagina_nav:
                col_anterior, col_proxima = st.columns(2)
                with col_anterior:
                    if st.button(
                        "⬅️ Anterior",
                        key="auditoria_pagina_anterior",
                        disabled=pagina_atual <= 1,
                        width="stretch",
                    ):
                        st.session_state["auditoria_pagina"] = pagina_atual - 1
                        st.rerun()
                with col_proxima:
                    if st.button(
                        "Próxima ➡️",
                        key="auditoria_pagina_proxima",
                        disabled=pagina_atual >= total_paginas,
                        width="stretch",
                    ):
                        st.session_state["auditoria_pagina"] = pagina_atual + 1
                        st.rerun()

            capitulos = sorted({g["capitulo"] for g in produtos_pagina})
            indice_render = inicio_pagina

            for capitulo in capitulos:
                nome_capitulo = CAPITULOS_NCM_NOME.get(capitulo, "Outros Produtos")
                titulo_capitulo = f"📁 Capítulo {capitulo} - {nome_capitulo}"
                produtos_para_exibir = [
                    g for g in produtos_pagina if g["capitulo"] == capitulo
                ]
                if not produtos_para_exibir:
                    continue

                with st.expander(
                    f"{titulo_capitulo} ({len(produtos_para_exibir)} grupos)"
                ):
                    for prod in produtos_para_exibir:
                        renderizar_produto(
                            prod,
                            indice_render,
                            todos_grupos,
                            itens_auditados,
                            mostrar_todos,
                            regras_agrupamento,
                            descricoes_conhecidas,
                            duplicidades,
                            aberracoes_pendentes,
                            descricoes_aberrantes,
                        )
                        indice_render += 1

            if grupos_exibidos == 0:
                st.success(
                    "🎉 Nenhuma divergência pendente neste filtro — todos os grupos de exceção já estão "
                    "com as descrições aprovadas!"
                )
            else:
                st.caption(f"Grupos exibidos: {formatar_numero(grupos_exibidos)}")

        # Pendências manuais continuam em um bloco próprio (elas saem dos relatórios);
        # as variações bruscas já estão integradas aos cards acima.
        _renderizar_pendencias_manuais()

        if regras_agrupamento:
            with st.expander(f"⚙️ Regras de agrupamento ativas ({len(regras_agrupamento)})"):
                st.caption(
                    "Regras de-para aplicadas na formação da chave inteligente dos produtos. "
                    "As regras de **juntar** fundem grupos inteiros; as de **mover** trazem uma única "
                    "descrição para outro grupo. Elas valem também para as próximas cargas."
                )
                descricoes_conhecidas_regras = df["descricao_produto"].dropna().unique().tolist()
                for regra in regras_agrupamento:
                    col_regra, col_remover = st.columns([5, 1])
                    with col_regra:
                        if regra["chave_origem"].startswith("DESC_"):
                            descricao_movida = (
                                descrever_descricao_da_regra(regra["chave_origem"], descricoes_conhecidas_regras)
                                or regra["chave_origem"]
                            )
                            rotulo_regra = (
                                f"📝 **{descricao_movida}** ➡️ `{regra['chave_destino']}` (mover)"
                            )
                        else:
                            rotulo_regra = (
                                f"🔗 `{regra['chave_origem']}` ➡️ `{regra['chave_destino']}` ({regra['tipo']})"
                            )
                        st.write(rotulo_regra)
                    with col_remover:
                        if st.button("Remover", key=f"btn_remover_regra_{regra['id']}", width="stretch"):
                            try:
                                resp = requests.delete(
                                    f"{REGRAS_URL}/{regra['id']}",
                                    headers=cabecalhos_api(),
                                    timeout=30,
                                )
                                if resp.status_code == 200:
                                    st.rerun()
                                else:
                                    st.error(f"Erro ao remover regra ({resp.status_code}).")
                            except Exception as e:
                                st.error(f"Erro: {e}")

    with aba2:
        st.header("📈 Resumo Geral e Estatísticas")
        st.metric("Total de Registros Analisados", formatar_numero(len(df)))
        st.metric("Valor Total Movimentado", formatar_moeda(float(df["valor_total"].sum())))

    with aba3:
        renderizar_aprendizado()

    with aba4:
        renderizar_configuracoes_admin()

    with aba5:
        renderizar_exportacao_base_completa()

    with aba6:
        renderizar_sugestoes_ia(df)


def renderizar_memoria_marcas_ia():
    """Painel 🤖 Memória de marcas: analisa em lotes e aplica o cache sem repetir tokens.

    Cada descrição única passa pela tabela `marcas_ia`: se já foi respondida pela DeepSeek,
    volta do banco (custo zero); só as inéditas geram chamada. O processamento é feito em
    lotes de `LOTE_MARCAS_IA` para a barra de progresso andar e para uma falha não perder
    o que já foi memorizado. No fim, `itens_nota.marca` é recalculado só com cache +
    heurística local, sem nenhuma chamada nova à IA.
    """
    st.subheader("🤖 Memória de marcas da IA (economia de tokens)")
    st.caption(
        "A marca de cada descrição fica gravada em `marcas_ia`. Ao rodar de novo, o "
        "sistema responde pela memória e chama a DeepSeek **somente** para descrições "
        "inéditas — o contador abaixo mostra quantas consultas foram economizadas."
    )

    resumo = st.session_state.get(CHAVE_RESUMO_MARCAS_IA)
    if resumo:
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("Descrições analisadas", formatar_numero(resumo.get("total", 0)))
        col_b.metric(
            "Consultas à IA",
            formatar_numero(resumo.get("consultas_ia", 0)),
            help="Cada consulta é uma descrição inédita enviada à DeepSeek.",
        )
        col_c.metric(
            "Economizadas pelo cache",
            formatar_numero(resumo.get("cache_hits", 0)),
            help="Descrições que já estavam em `marcas_ia` e não gastaram tokens.",
        )
        col_d.metric("Sem marca", formatar_numero(resumo.get("sem_marca", 0)))
        st.success(
            f"Memória e catálogo atualizados: {formatar_numero(resumo.get('total', 0))} "
            f"descrições | {formatar_numero(resumo.get('itens_com_marca', 0))} itens com "
            f"marca | {formatar_numero(resumo.get('atualizados', 0))} registros gravados."
        )
        if resumo.get("erro"):
            st.warning(
                "A IA parou no meio da rodada e os lotes seguintes usaram só o cache: "
                f"{resumo['erro']}"
            )

    col_botao, col_info = st.columns([1, 2])
    with col_botao:
        iniciar = st.button(
            "🤖 Recalcular Marcas com IA",
            key="btn_recalcular_marcas_ia",
            help=(
                "Analisa as descrições únicas da base. Já analisadas vêm do cache; só as "
                "inéditas gastam tokens da DeepSeek."
            ),
        )
    with col_info:
        st.caption(
            "O processamento vai em lotes de "
            f"{LOTE_MARCAS_IA} descrições para a barra de progresso. Ao final, as marcas "
            "são aplicadas em toda a base (inclusive nas compras já importadas)."
        )

    if not iniciar:
        return

    st.session_state.pop(CHAVE_RESUMO_MARCAS_IA, None)
    dados, erro = carregar_descricoes_marcas_ia()
    if erro:
        st.error(erro)
        return

    descricoes = dados.get("descricoes") or []
    total = len(descricoes)
    if not total:
        st.info("Nenhuma descrição de produto encontrada na base para analisar.")
        return

    barra = st.progress(0.0, text=f"Consultando a memória de marcas... 0/{total}")
    consultas_ia = 0
    cache_hits = 0
    sem_marca = 0
    marcas_encontradas = 0
    processadas = 0
    erro_ia = None
    usar_ia = True

    for inicio in range(0, total, LOTE_MARCAS_IA):
        lote = descricoes[inicio : inicio + LOTE_MARCAS_IA]
        resposta, falha = processar_marcas_ia_lote(lote, usar_ia=usar_ia)
        if falha:
            barra.empty()
            st.error(f"Lote {inicio // LOTE_MARCAS_IA + 1}: {falha}")
            return
        consultas_ia += int(resposta.get("consultas_ia", 0) or 0)
        cache_hits += int(resposta.get("cache_hits", 0) or 0)
        sem_marca += int(resposta.get("sem_marca", 0) or 0)
        marcas_encontradas += int(resposta.get("marcas_encontradas", 0) or 0)
        processadas += int(resposta.get("processadas", len(lote)) or 0)
        if resposta.get("erro"):
            erro_ia = resposta["erro"]
            # Sem chave/erro na IA: os lotes seguintes não tentam de novo, só cache.
            usar_ia = False
        barra.progress(
            min(processadas / total, 1.0),
            text=(
                f"Memória: {formatar_numero(processadas)}/{formatar_numero(total)} | "
                f"IA: {formatar_numero(consultas_ia)} | cache: "
                f"{formatar_numero(cache_hits)}"
            ),
        )

    barra.progress(1.0, text="Aplicando as marcas no catálogo...")
    aplicado, falha_aplicar = aplicar_memoria_marcas_ia()
    barra.empty()
    if falha_aplicar:
        st.error(falha_aplicar)
        return

    st.session_state[CHAVE_RESUMO_MARCAS_IA] = {
        "total": total,
        "processadas": processadas,
        "consultas_ia": consultas_ia,
        "cache_hits": cache_hits,
        "sem_marca": sem_marca,
        "marcas_encontradas": marcas_encontradas,
        "itens_com_marca": (aplicado or {}).get("com_marca", 0),
        "atualizados": (aplicado or {}).get("atualizados", 0),
        "erro": erro_ia,
    }
    st.rerun()


def renderizar_aprendizado():
    """Aba 🧠 Aprendizado da IA (Marcas) (admin).

    Mostra a memória de-para (CNPJ do fornecedor + cProd) que o importador aprendeu, a
    marca que a IA extraiu para cada descrição e a embalagem normalizada. A correção
    manual de de-para fica na aba 🔍 Auditoria de Divergências (a origem manual continua
    sendo respeitada pelo importador).
    """
    st.header("🧠 Aprendizado da IA (Marcas) e Embalagens")
    st.caption(
        "O importador grava, por **fornecedor + cProd**, o EAN, a descrição, o NCM e a "
        "embalagem canônicos. A coluna **Marca** mostra a resposta da IA para a descrição "
        "(ou **SEM MARCA**) e a coluna **Embalagem** mostra a conversão CX/FD → UN/KG "
        "aplicada nos preços (ex.: `CAIXA 24 UN`, `CAIXA 15 KG`)."
    )

    renderizar_memoria_marcas_ia()
    st.divider()

    col_busca, col_limite = st.columns([3, 1])
    with col_busca:
        busca = st.text_input(
            "🔎 Pesquisar por cProd, descrição, EAN, NCM ou CNPJ do fornecedor:",
            "",
            key="aprendizado_busca",
        ).strip()
    with col_limite:
        limite = st.selectbox(
            "Registros", [100, 300, 1000, 2000], index=1, key="aprendizado_limite"
        )

    col_reprocessar, col_ajuda = st.columns([1, 2])
    with col_reprocessar:
        if st.button(
            "🔄 Reprocessar base",
            key="btn_reprocessar_aprendizado",
            help=(
                "Recalcula as embalagens dos itens já importados e completa a memória "
                "com as notas antigas. Não altera correções manuais."
            ),
        ):
            with st.spinner("Reprocessando as notas já importadas..."):
                resumo, erro = reprocessar_aprendizado()
            if erro:
                st.error(erro)
            else:
                st.session_state["_resumo_aprendizado"] = resumo
                st.rerun()
    with col_ajuda:
        st.caption(
            "Use o botão quando importar XMLs por fora do sistema ou depois de corrigir "
            "vários de-para: é o mesmo motor do `python backfill_aprendizado.py`."
        )

    resumo = st.session_state.get("_resumo_aprendizado")
    if resumo:
        st.success(
            f"Base reprocessada: {formatar_numero(resumo.get('itens', 0))} itens | "
            f"{formatar_numero(resumo.get('itens_com_embalagem', 0))} com embalagem | "
            f"{formatar_numero(resumo.get('mapeamentos_criados', 0))} de-para criados | "
            f"{formatar_numero(resumo.get('mapeamentos_atualizados', 0))} atualizados."
        )
        if resumo.get("itens_codigo_recuperado"):
            st.caption(
                f"Notas antigas: {formatar_numero(resumo.get('xmls_lidos', 0))} XMLs relidos "
                f"e {formatar_numero(resumo.get('itens_codigo_recuperado', 0))} itens com "
                "cProd/uCom recuperados do XML."
            )
        if resumo.get("xmls_nao_encontrados"):
            st.warning(
                f"{formatar_numero(resumo.get('xmls_nao_encontrados', 0))} XML(s) não "
                "encontrados no caminho gravado: reimporte essas notas para recuperar o "
                "cProd dos itens."
            )

    dados = carregar_aprendizado(busca, limite)
    if dados.get("erro"):
        st.error(dados["erro"])
        return

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("De-Para aprendidos", formatar_numero(dados.get("total", 0)))
    col2.metric("Corrigidos à mão", formatar_numero(dados.get("manuais", 0)))
    col3.metric(
        "Itens com embalagem", formatar_numero(dados.get("itens_com_embalagem", 0))
    )
    col4.metric(
        "Itens sem cProd",
        formatar_numero(dados.get("itens_sem_codigo_fornecedor", 0)),
        help="Notas antigas importadas antes do de-para: não alimentam a memória.",
    )

    itens = dados.get("itens") or []
    if not itens:
        st.info(
            "Nenhum de-para encontrado. Rode `python import_xmls.py` ou use o botão "
            "🔄 Reprocessar base para aprendê-los com as notas já importadas."
        )
        return

    tabela = pd.DataFrame(
        [
            {
                "Fornecedor": item.get("fornecedor") or item.get("cnpj_fornecedor", ""),
                "CNPJ": formatar_cnpj(item.get("cnpj_fornecedor", "")),
                "cProd": item.get("c_prod", ""),
                "Descrição canônica": item.get("descricao_padrao", ""),
                "Marca": item.get("marca_ia") or "SEM MARCA",
                "EAN": item.get("ean_padrao", "") or "SEM_EAN",
                "NCM": item.get("ncm_gravado", ""),
                "uCom": item.get("u_com_gravada", ""),
                "Embalagem": item.get("embalagem", ""),
                "Origem": "✋ manual" if item.get("origem") == "manual" else "🤖 automático",
                "Atualizado em": item.get("atualizado_em", ""),
            }
            for item in itens
        ]
    )
    st.dataframe(
        tabela,
        width="stretch",
        hide_index=True,
        key="tabela_aprendizado",
        column_config={
            "Descrição canônica": st.column_config.TextColumn(
                "Descrição canônica", width="large"
            ),
            "Fornecedor": st.column_config.TextColumn("Fornecedor", width="medium"),
            "Marca": st.column_config.TextColumn("Marca", width="small"),
            "Embalagem": st.column_config.TextColumn("Embalagem", width="small"),
        },
    )
    if dados.get("total", 0) > len(itens):
        st.caption(
            f"Exibindo {formatar_numero(len(itens))} de "
            f"{formatar_numero(dados.get('total', 0))} registros — refine a busca."
        )






def renderizar_configuracoes_admin():
    """Aba ⚙️ Configurações (admin): token da API do WhatsApp do motor de alertas.

    O token é GLOBAL (tabela `configuracoes_sistema`) e o valor nunca volta inteiro para a
    tela — o GET devolve só os últimos caracteres. Campo vazio + salvar remove o token e
    desliga o envio real (as simulações do cliente continuam funcionando).
    """
    st.header("⚙️ Configurações do sistema")
    st.caption(
        "Token da API do WhatsApp usado no envio dos alertas. Fica guardado no servidor e "
        "nunca é exibido inteiro. Deixar o campo vazio e salvar remove o token "
        "(envio real desligado)."
    )
    dados, erro = carregar_config_admin(st.session_state.get(SESSAO_TOKEN))
    if erro:
        st.error(erro)
        return
    dados = dados or {}
    if dados.get("whatsapp_token_configurado"):
        st.success(f"Token configurado: {dados.get('whatsapp_token_mascarado')}")
        st.caption(
            f"Atualizado em {dados.get('atualizado_em') or '—'} "
            f"por {dados.get('atualizado_por') or '—'}."
        )
    else:
        st.warning(
            "Nenhum token cadastrado: o disparo real dos alertas no WhatsApp está desligado."
        )

    with st.form("form_config_admin"):
        token = st.text_input(
            "Token da API do WhatsApp",
            key="admin_whatsapp_token",
            type="password",
            help=(
                "Token de acesso da API oficial do WhatsApp (WhatsApp Business). "
                "Ele não é gravado em texto na tela nem devolvido pela API."
            ),
        )
        salvar = st.form_submit_button("💾 Salvar token", type="primary")
    if not salvar:
        return
    novo, erro_salvar = salvar_config_admin(token)
    if erro_salvar:
        st.error(erro_salvar)
        return
    st.session_state.pop("admin_whatsapp_token", None)  # não deixa o token na tela
    st.cache_data.clear()
    st.success((novo or {}).get("mensagem") or "Token salvo.")
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# Login / cadastro (tela de entrada quando não há sessão)
# ──────────────────────────────────────────────────────────────────────────────
CSS_LOGIN = """
<style>
/* Tela de entrada: fundo em degradê, cartão branco centralizado, sidebar escondida. */
.stApp { background: linear-gradient(160deg, #eef4fb 0%, #dfe9f7 45%, #eaf0f8 100%); }
div[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none; }
.login-hero { text-align: center; margin: .2rem 0 1.1rem 0; }
.login-hero .icone { font-size: 2.6rem; line-height: 1; }
.login-hero h1 {
    font-size: 1.85rem; margin: .35rem 0 .25rem 0; color: #10365c;
    letter-spacing: -.02em; font-weight: 700;
}
.login-hero p { color: #4a6076; font-size: .93rem; margin: 0; }
div[class*="st-key-card_login"] {
    background: #ffffff; border: 1px solid #dbe4ef; border-radius: 18px;
    padding: 1.2rem 1.5rem .6rem 1.5rem;
    box-shadow: 0 18px 40px rgba(16, 54, 92, .12);
}
.login-rodape { text-align: center; color: #6b7f95; font-size: .8rem; margin-top: 1rem; }
</style>
"""


def formulario_codigo_2fa(chave, token=None, rotulo="Confirmar código"):
    """Formulário do código de 6 dígitos (login e Auditoria).

    Devolve a resposta de /api/auth/2fa/verify quando o código é aceito e None nos demais
    casos (o erro aparece na tela). Quem chama decide quando guardar o token 2FA — no
    login isso é feito DEPOIS de `entrar_no_sistema`, que limpa a sessão anterior.
    """
    with st.form(f"form_2fa_{chave}"):
        codigo = st.text_input(
            "🔐 Código de 6 dígitos do autenticador",
            key=f"2fa_codigo_{chave}",
            max_chars=6,
            placeholder="000000",
            help="Código gerado pelo aplicativo autenticador (Google Authenticator, Authy...).",
        )
        confirmar = st.form_submit_button(
            rotulo, key=f"btn_2fa_{chave}", type="primary", use_container_width=True
        )
    if not confirmar:
        return None
    digitos = re.sub(r"\D", "", codigo or "")
    if len(digitos) != 6:
        st.warning("O código tem 6 dígitos. Confira no aplicativo autenticador e tente de novo.")
        return None
    dados, erro = _post_2fa(AUTH_2FA_VERIFY_URL, {"codigo": digitos}, token=token)
    if erro:
        st.error(erro)
        return None
    return dados


def renderizar_tela_login():
    """Tela de entrada (Entrar / Criar conta) — ou a 2ª etapa do 2FA, quando pendente."""
    st.markdown(CSS_LOGIN, unsafe_allow_html=True)
    _, centro, _ = st.columns([1, 1.25, 1])
    with centro:
        st.markdown(
            """
            <div class="login-hero">
                <div class="icone">🧾</div>
                <h1>Inteligência de Custos</h1>
                <p>Compare preços entre as suas lojas e o mercado, com auditoria das variações.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        aviso = st.session_state.pop(MENSAGEM_SAIDA, None)
        if aviso:
            st.info(aviso)
        pendente = st.session_state.get(SESSAO_LOGIN_2FA)
        verificacao = st.session_state.get(SESSAO_VERIFICACAO_EMAIL)
        with st.container(border=True, key="card_login"):
            if verificacao:
                # Cadastro criado, e-mail ainda não confirmado: só o código abre a sessão.
                _formulario_verificacao_email(verificacao)
            elif pendente:
                _formulario_login_2fa(pendente)
            else:
                aba_entrar, aba_criar = st.tabs(["🔑 Entrar", "✨ Criar conta"])
                with aba_entrar:
                    _formulario_login()
                with aba_criar:
                    _formulario_cadastro()
        st.markdown(
            '<div class="login-rodape">Cada conta enxerga apenas os CNPJs vinculados a ela.</div>',
            unsafe_allow_html=True,
        )


def _formulario_login():
    """1ª etapa do login: e-mail e senha.

    Conta comum (restaurante) entra direto. Administrador **não** recebe sessão aqui: o
    backend responde 401/403 com um desafio de 2FA e a tela mostra o campo do código (ou
    o QR Code, no primeiro acesso). A sessão só nasce em /api/auth/2fa/verify.
    """
    with st.form("form_login"):
        email = st.text_input(
            "📧 E-mail", key="login_email", placeholder="voce@suaempresa.com.br"
        )
        senha = st.text_input("🔒 Senha", type="password", key="login_senha")
        entrar = st.form_submit_button(
            "Entrar", key="btn_entrar", type="primary", use_container_width=True
        )
    if not entrar:
        return
    if not email.strip() or not senha:
        st.warning("Preencha e-mail e senha para entrar.")
        return
    dados, erro, detalhe = post_auth(AUTH_LOGIN_URL, {"email": email, "senha": senha})
    if erro:
        if detalhe.get("desafio"):
            # Senha correta, segundo fator pendente: nada de sessão aberta ainda.
            st.session_state[SESSAO_LOGIN_2FA] = {
                "modo": "setup" if detalhe.get("configuracao_2fa_necessaria") else "codigo",
                "desafio": detalhe["desafio"],
                "expira_em": detalhe.get("expira_em") or "",
                "usuario": detalhe.get("usuario") or {},
                "aviso": erro if detalhe.get("motivo") == "codigo_invalido" else "",
            }
            st.rerun()
        if detalhe.get("email_nao_verificado"):
            # Senha correta, e-mail pendente: a tela do código abre com o aviso.
            st.session_state[SESSAO_VERIFICACAO_EMAIL] = {
                "email": detalhe.get("email") or email.strip().lower(),
                "mensagem": "Confirme o e-mail para entrar.",
                "aviso": "",
            }
            st.rerun()
        st.error(erro)
        return
    entrar_no_sistema(dados)
    if dados.get("token_2fa"):
        # O login já validou o código do autenticador: a Auditoria abre sem pedir de novo.
        _guardar_sessao_2fa(
            {
                "token_2fa": dados.get("token_2fa"),
                "expira_em": dados.get("expira_em_2fa"),
                "usuario": dados.get("usuario"),
            }
        )
    st.rerun()


def _formulario_login_2fa(pendente):
    """2ª etapa do login: QR Code (primeiro acesso) e código de 6 dígitos.

    O `desafio` guardado em `SESSAO_LOGIN_2FA` vai no `Authorization` das rotas de 2FA;
    quando o código confere, `/api/auth/2fa/verify` devolve também a sessão (`token`).
    """
    pendente = pendente or {}
    modo_setup = str(pendente.get("modo") or "codigo") == "setup"
    usuario = pendente.get("usuario") or {}
    desafio = pendente.get("desafio") or ""
    st.markdown("#### 🔐 Verificação em dois fatores")
    if str(pendente.get("aviso") or "").strip():
        st.error(pendente["aviso"])
    st.caption(
        f"Conta **{usuario.get('email') or ''}**. "
        + (
            "O login exige o autenticador: escaneie o QR Code abaixo e informe o código de "
            "6 dígitos para concluir."
            if modo_setup
            else "Abra o aplicativo autenticador e informe o código de 6 dígitos para concluir "
            "o login."
        )
    )
    if modo_setup:
        setup, erro_setup = carregar_setup_2fa(token=desafio)
        if erro_setup:
            st.error(erro_setup)
        elif setup and setup.get("qr_code"):
            col_qr, col_passos = st.columns([1, 2])
            with col_qr:
                st.image(base64.b64decode(setup["qr_code"].split(",", 1)[-1]), width=190)
            with col_passos:
                st.markdown(
                    "1. Abra o aplicativo autenticador e escolha **Adicionar conta**;\n"
                    "2. Escaneie o QR Code ao lado (ou digite o segredo);\n"
                    "3. Informe o código de 6 dígitos para entrar."
                )
                st.caption(f"Segredo para digitação manual: `{setup.get('segredo') or ''}`")
        elif setup and setup.get("ja_configurado"):
            # Já configurou em outro acesso: agora é só o código.
            st.info("O 2FA já está ativo nesta conta: informe o código do autenticador.")

    dados = formulario_codigo_2fa("login", token=desafio, rotulo="Entrar com 2FA")
    if dados and dados.get("token"):
        entrar_no_sistema({"usuario": dados.get("usuario"), "token": dados["token"]})
        _guardar_sessao_2fa(dados)
        st.rerun()
    if dados and not dados.get("token"):
        # Defesa: o verify do desafio sempre devolve a sessão. Sem ela, volta ao início.
        st.session_state.pop(SESSAO_LOGIN_2FA, None)
        st.session_state[MENSAGEM_SAIDA] = (
            "Não foi possível concluir o login. Informe e-mail e senha novamente."
        )
        st.rerun()
    if st.button("↩️ Voltar para o login", key="btn_login_2fa_voltar", use_container_width=True):
        st.session_state.pop(SESSAO_LOGIN_2FA, None)
        st.rerun()
    st.caption(
        "Perdeu o autenticador? No servidor, rode "
        "`python criar_admin.py SEU@EMAIL --resetar-2fa` e entre de novo."
    )


def _formulario_verificacao_email(pendente):
    """2ª etapa do cadastro: código de 6 dígitos enviado ao e-mail da conta.

    O token da sessão só nasce em /api/auth/validar-email. "Reenviar código" respeita o
    limite do backend (1/minuto, 10/hora) e "Voltar" devolve para as abas de entrada.
    """
    pendente = pendente if isinstance(pendente, dict) else {}
    email = str(pendente.get("email") or "")
    st.markdown("#### ✉️ Confirme o seu e-mail")
    st.caption(
        f"Enviamos um código de **6 dígitos** para **{email}**. "
        "Confira também a caixa de spam/lixo eletrônico."
    )
    if pendente.get("mensagem"):
        st.info(str(pendente["mensagem"]))
    if pendente.get("aviso"):
        st.warning(str(pendente["aviso"]))

    with st.form("form_verificar_email"):
        codigo = st.text_input(
            "Código de 6 dígitos",
            key="verificar_email_codigo",
            max_chars=6,
            placeholder="000000",
        )
        confirmar = st.form_submit_button(
            "✅ Confirmar e entrar", type="primary", use_container_width=True
        )
    if confirmar:
        digitos = re.sub(r"\D", "", codigo or "")
        if len(digitos) != 6:
            st.warning("O código tem 6 dígitos. Confira o e-mail e tente de novo.")
        else:
            dados, erro, _detalhe = post_auth(
                AUTH_VALIDAR_EMAIL_URL, {"email": email, "codigo": digitos}
            )
            if erro:
                st.error(erro)
            elif dados:
                st.session_state.pop(SESSAO_VERIFICACAO_EMAIL, None)
                entrar_no_sistema(dados)
                st.rerun()

    if st.button("📨 Reenviar código", key="btn_reenviar_codigo_email", use_container_width=True):
        dados, erro, _detalhe = post_auth(AUTH_REENVIAR_CODIGO_URL, {"email": email})
        if erro:
            st.error(erro)
        elif dados:
            pendente["mensagem"] = str(dados.get("mensagem") or "")
            pendente["aviso"] = (
                str(dados["aviso"]) if not dados.get("email_enviado") else ""
            )
            st.session_state[SESSAO_VERIFICACAO_EMAIL] = pendente
            st.rerun()
    if st.button(
        "↩️ Voltar para o login", key="btn_voltar_verificacao_email", use_container_width=True
    ):
        st.session_state.pop(SESSAO_VERIFICACAO_EMAIL, None)
        st.rerun()


def _formulario_cadastro():
    """Formulário de cadastro: POST /api/auth/registrar (a senha é hasheada no backend)."""
    with st.form("form_cadastro"):
        nome = st.text_input("👤 Nome", key="cad_nome", placeholder="Seu nome completo")
        email = st.text_input(
            "📧 E-mail", key="cad_email", placeholder="voce@suaempresa.com.br"
        )
        coluna_senha, coluna_confirmacao = st.columns(2)
        senha = coluna_senha.text_input("🔒 Senha", type="password", key="cad_senha")
        confirmacao = coluna_confirmacao.text_input(
            "🔒 Repita a senha", type="password", key="cad_senha_confirma"
        )
        criar = st.form_submit_button(
            "Criar conta", key="btn_criar_conta", type="primary", use_container_width=True
        )
    if not criar:
        return
    if len(nome.strip()) < 3:
        st.warning("Informe o seu nome completo.")
        return
    if "@" not in email or "." not in email.split("@")[-1]:
        st.warning("Informe um e-mail válido.")
        return
    if len(senha) < SENHA_MINIMA_UI:
        st.warning(f"A senha precisa ter pelo menos {SENHA_MINIMA_UI} caracteres.")
        return
    if senha != confirmacao:
        st.warning("As duas senhas não são iguais.")
        return
    dados, erro, _detalhe = post_auth(
        AUTH_REGISTRAR_URL, {"nome": nome, "email": email, "senha": senha}
    )
    if erro:
        st.error(erro)
        return
    if not dados:
        st.error("A API não devolveu os dados do cadastro. Tente novamente.")
        return
    # A conta existe, mas SEM sessão: o token só sai em /api/auth/validar-email.
    st.session_state[SESSAO_VERIFICACAO_EMAIL] = {
        "email": dados.get("email") or email.strip().lower(),
        "nome": dados.get("nome") or nome.strip(),
        "mensagem": str(dados.get("mensagem") or ""),
        "aviso": str(dados.get("aviso") or ""),
        "expira_em": dados.get("expira_em") or "",
    }
    st.rerun()


def exigir_login():
    """Mostra a tela de entrada e interrompe a página quando não há sessão."""
    if usuario_logado():
        return
    renderizar_tela_login()
    st.stop()


# ──────────────────────────────────────────────────────────────────────────────
# Tela "Minhas Lojas" (CNPJs da conta)
# ──────────────────────────────────────────────────────────────────────────────
def itens_da_empresa(df, empresas):
    """Recorta os itens das lojas da CONTA logada (todos os CNPJs vinculados).

    O recorte é feito pelo CNPJ (duas lojas podem ter o mesmo nome fantasia) e nunca sai
    das empresas do usuário logado. O nome fica como reserva para bases antigas, sem a
    coluna `cnpj` na resposta do /api/resumo-precos. O recorte por loja de cada relatório
    é feito nos filtros das abas (Evolução, Comparativo e Impacto).
    """
    alvo = list(empresas or [])
    cnpjs = {
        re.sub(r"\D", "", str(empresa.get("cnpj") or ""))
        for empresa in alvo
        if empresa.get("cnpj")
    }
    cnpjs.discard("")
    if cnpjs and "cnpj" in df.columns:
        cnpj_da_linha = df["cnpj"].astype(str).str.replace(r"\D", "", regex=True)
        return df[cnpj_da_linha.isin(cnpjs)]
    nomes = {empresa.get("nome_loja") for empresa in alvo if empresa.get("nome_loja")}
    if not nomes or "nome_loja" not in df.columns:
        return df.iloc[0:0]
    return df[df["nome_loja"].isin(nomes)]


def _chave_confirmacao_exclusao_certificado(empresa_id):
    """Chave versionada do checkbox "Confirmar exclusão" de uma loja.

    O Streamlit pode restaurar o valor de um checkbox cuja chave foi apenas
    removida do ``session_state``. Incluir a versão garante que, após a
    exclusão/limpeza, o próximo run crie outro widget — necessariamente
    desmarcado — em vez de reaproveitar o estado marcado anterior.
    """
    versao = st.session_state.get(CHAVE_CONFIRMACAO_EXCLUSAO_CERT, 0)
    if not isinstance(versao, int):
        versao = 0
    return f"{PREFIXO_CONFIRMACAO_EXCLUSAO_CERT}{empresa_id}_{versao}"


def _botao_excluir_certificado(empresa, habilitado=True):
    """Botão "Excluir Certificado" do cartão da loja, com confirmação em dois passos.

    Aparece em TODOS os cartões (padrão único de tela); fica desabilitado quando a loja
    está sem certificado. O checkbox é a confirmação: o botão só habilita depois que ele é
    marcado (nada de apagar o certificado com um clique acidental). O backend remove
    apenas os dados públicos do certificado — a loja e as notas importadas continuam.
    """
    empresa_id = empresa.get("id")
    confirmar = st.checkbox(
        "Confirmar exclusão",
        key=_chave_confirmacao_exclusao_certificado(empresa_id),
        disabled=not habilitado,
        help=(
            "O certificado é removido do cadastro desta loja. As notas fiscais já "
            "importadas e o histórico continuam intactos."
        ),
    )
    if st.button(
        "🗑️ Excluir Certificado",
        key=f"btn_excluir_cert_{empresa_id}",
        disabled=not (habilitado and confirmar),
        width="stretch",
    ):
        mensagem, erro = excluir_certificado_loja(empresa_id)
        if erro:
            st.error(erro)
        else:
            # O `st.rerun()` apaga a própria tela, então a confirmação vira
            # `st.success` no próximo run. O checkbox também é desmarcado antes
            # de ser recriado.
            st.session_state[MENSAGEM_MINHAS_LOJAS] = (
                mensagem or "Certificado excluído."
            )
            st.session_state[LIMPAR_CONFIRMACAO_EXCLUSAO_CERT] = True
            st.cache_data.clear()  # o cartão passa a mostrar "sem certificado"
            st.rerun()


def _acoes_acbr_empresa(empresa):
    """Botões de download manual e diagnóstico da ACBr no cartão da loja.

    Ações operacionais ficam restritas ao administrador: o cliente comum vê os
    dados da loja e pode excluir o certificado, mas não dispara downloads nem
    consultas técnicas na ACBr API.

    O erro devolvido pela API (incluindo o motivo real da ACBr) é exibido no
    próprio cartão, sem `st.rerun()`, para não apagar justamente a mensagem que
    o suporte precisa receber.
    """
    if not usuario_e_admin():
        return
    empresa_id = empresa.get("id")
    cnpj = empresa.get("cnpj") or ""

    if st.button(
        "🔄 Forçar Download de XMLs",
        key=f"btn_forcar_download_{empresa_id}",
        width="stretch",
        help=(
            "Consulta a Distribuição NF-e agora, usando o NSU salvo desta loja. "
            "No primeiro download vale a janela de 7 dias configurada no backend."
        ),
    ):
        with st.spinner("Baixando XMLs da ACBr..."):
            resultado, erro = forcar_download_acbr(cnpj)
        if erro:
            st.error(erro)
        else:
            st.success(
                resultado.get("mensagem") or "Download da ACBr concluído."
            )
            for aviso in resultado.get("avisos") or []:
                st.warning(str(aviso))
            st.caption(
                f"Encontrados: {resultado.get('documentos_encontrados', 0)} | "
                f"Baixados: {resultado.get('documentos_baixados', 0)} | "
                f"Ignorados: {resultado.get('documentos_ignorados', 0)}"
            )
            # A lista de lojas e os relatórios passam a refletir o novo NSU.
            st.cache_data.clear()

    if st.button(
        "🔍 Consultar Status ACBr",
        key=f"btn_status_acbr_{empresa_id}",
        width="stretch",
        help="Compara o NSU salvo na loja com o último NSU disponível na ACBr.",
    ):
        with st.spinner("Consultando a ACBr..."):
            status, erro = consultar_status_acbr(cnpj)
        if erro:
            st.error(erro)
        elif status:
            if status.get("sucesso"):
                st.success(
                    status.get("mensagem")
                    or (
                        f"Seu último NSU: {status.get('ultimo_nsu_local', '0')} | "
                        f"Último NSU na ACBr: {status.get('ultimo_nsu_acbr')}"
                    )
                )
                pendentes = status.get("pendentes")
                if pendentes:
                    st.caption(
                        f"{pendentes} NSU(s) novo(s) retornado(s) pela ACBr. "
                        "Use o botão de download para importar os XMLs."
                    )
                # O backend acabou de persistir o cursor: invalida a lista
                # cacheada para o próximo run mostrar o NSU atualizado no cartão.
                st.cache_data.clear()
            else:
                st.warning(
                    status.get("mensagem")
                    or "Não foi possível consultar o status da ACBr."
                )
                if status.get("erro"):
                    st.error(f"Motivo informado pela ACBr: {status['erro']}")

            if not status.get("certificado"):
                st.caption(
                    "🔐 Nenhum certificado digital cadastrado para esta loja."
                )

            ultima = status.get("ultima_automacao")
            if isinstance(ultima, dict):
                if ultima.get("sucesso") is False:
                    st.warning(
                        "Último download automático falhou: "
                        f"{ultima.get('motivo') or 'motivo não informado'}"
                    )
                else:
                    st.caption(
                        "Último download automático: "
                        f"{ultima.get('documentos_baixados', 0)} XML(s) baixado(s), "
                        f"NSU {ultima.get('ultimo_nsu_acbr') or 'não informado'}."
                    )


def _cartao_empresa(empresa, mostrar_acoes_acbr=False):
    """Cartão de uma loja vinculada: dados da empresa, notas e certificado A1.

    `mostrar_acoes_acbr` só é True para administradores; para as contas comuns o
    cartão mantém os dados e o botão de excluir certificado.
    """
    with st.container(border=True, key=f"card_loja_{empresa.get('id')}"):
        st.markdown(f"**🏪 {empresa.get('nome_loja') or 'Loja'}**")
        st.caption(f"CNPJ {formatar_cnpj(empresa.get('cnpj'))}")
        if empresa.get("dono_email"):
            # Só o administrador recebe `dono_email` (visão de suporte): mostra de
            # quem é a loja sem precisar entrar na conta do cliente.
            st.caption(f"👤 Conta: {empresa['dono_email']}")
        if empresa.get("razao_social"):
            st.caption(empresa["razao_social"])
        if empresa.get("municipio"):
            st.caption(f"📍 {empresa['municipio']}")
        st.markdown(
            f"📄 **{formatar_numero(empresa.get('total_notas') or 0)}** notas importadas"
        )
        certificado = empresa.get("certificado") or {}
        if certificado.get("validade"):
            st.caption(f"🔐 Certificado A1 válido até {certificado['validade']}")
        else:
            st.caption(
                "🔐 Sem certificado digital cadastrado — vincule o CNPJ novamente com o .pfx."
            )
        # Fonte das notas escolhida na tela (Omie x certificado A1). A concordância
        # da SEFAZ só vale para o certificado: sem ela o download fica pendente.
        if (empresa.get("fonte_notas") or "certificado") == "omie":
            st.caption("📥 Fonte das notas: **Omie**")
        else:
            st.caption("📥 Fonte das notas: **Certificado digital A1 (SEFAZ)**")
            concordancia = empresa.get("cert_concordancia") or {}
            if concordancia.get("em"):
                st.caption(
                    "✅ Concordância SEFAZ: "
                    f"{_formatar_data_hora_brasilia(concordancia['em'])}"
                )
            else:
                st.caption("⚠️ Concordância SEFAZ: pendente de confirmação")
        nsu = str(empresa.get("ultimo_nsu_acbr") or "").strip()
        if not nsu or nsu == "0":
            nsu = "Aguardando primeiro download"
        st.caption(f"🔄 Último NSU: {nsu}")
        if mostrar_acoes_acbr:
            _acoes_acbr_empresa(empresa)
        _botao_excluir_certificado(empresa, habilitado=bool(certificado))


def _chave_uploader_certificado():
    """Chave dinâmica do uploader do certificado no run atual.

    O Streamlit não permite limpar o valor de um `file_uploader` já instanciado.
    Por isso a chave carrega uma versão: depois do sucesso, a versão é
    incrementada e o próximo run cria outro widget, necessariamente vazio.
    """
    versao = st.session_state.get(CHAVE_UPLOADER_CERTIFICADO, 0)
    if not isinstance(versao, int):
        versao = 0
    return f"{PREFIXO_UPLOADER_CERTIFICADO}{versao}"


def _limpar_formulario_minhas_lojas():
    """Zera os campos de "Vincular novo CNPJ" ANTES de recriar os widgets.

    O Streamlit não permite alterar/remover uma chave de widget já instanciada no
    mesmo run; por isso a limpeza é sinalizada no envio e executada no topo do run
    seguinte, imediatamente antes do formulário. Para o `file_uploader`, além de
    remover o estado antigo, a versão é incrementada: a chave nova garante que o
    widget volte vazio mesmo com o arquivo ainda retido em memória pela sessão.
    """
    st.session_state["nova_loja_cnpj"] = ""
    st.session_state["nova_loja_senha_certificado"] = ""
    # Fonte Omie do cadastro: credenciais e o aceite da SEFAZ não pertencem à
    # próxima loja. O radio volta ao certificado (fluxo histórico como padrão).
    st.session_state[NOVA_LOJA_FONTE_NOTAS] = ROTULO_FONTE_CERTIFICADO
    st.session_state[NOVA_LOJA_CONCORDANCIA_SEFAZ] = False
    st.session_state[NOVA_LOJA_OMIE_APP_KEY] = ""
    st.session_state[NOVA_LOJA_OMIE_APP_SECRET] = ""
    # O endereço fiscal também nasce vazio (é o mesmo dado da loja recém-criada).
    for chave in CAMPOS_ENDERECO_MINHAS_LOJAS.values():
        st.session_state[chave] = ""
    # E o resumo da consulta do CNPJ (razão social/fonte/aviso) não pertence à próxima.
    st.session_state.pop(DADOS_CNPJ_MINHAS_LOJAS, None)
    st.session_state.pop(DADOS_CEP_MINHAS_LOJAS, None)
    st.session_state.pop(CAMPOS_PREENCHIDOS_PELO_CEP, None)
    st.session_state.pop(_chave_uploader_certificado(), None)
    versao = st.session_state.get(CHAVE_UPLOADER_CERTIFICADO, 0)
    if not isinstance(versao, int):
        versao = 0
    st.session_state[CHAVE_UPLOADER_CERTIFICADO] = versao + 1


def _limpar_confirmacoes_exclusao_certificado():
    """Desmarca todos os checkboxes "Confirmar exclusão" das lojas.

    A chave de cada checkbox inclui o id da empresa e uma versão. Além de remover
    as chaves antigas, a versão é incrementada para o Streamlit criar widgets
    novos (vazios) no run seguinte, sem restaurar o estado marcado anterior.
    """
    for chave in list(st.session_state.keys()):
        if str(chave).startswith(PREFIXO_CONFIRMACAO_EXCLUSAO_CERT):
            st.session_state.pop(chave, None)
    versao = st.session_state.get(CHAVE_CONFIRMACAO_EXCLUSAO_CERT, 0)
    if not isinstance(versao, int):
        versao = 0
    st.session_state[CHAVE_CONFIRMACAO_EXCLUSAO_CERT] = versao + 1


@st.cache_data(ttl=600, show_spinner=False)
def consultar_dados_cnpj_api(cnpj, token):
    """GET ``/api/empresas/consultar-cnpj``: razão social + endereço fiscal da Receita.

    Devolve ``(dados, erro)``. O cache de 10 minutos evita repetir a consulta a cada
    interação da tela (o Streamlit reexecuta o script inteiro a cada clique); o backend
    já repete a chamada em 429/5xx e guarda a resposta boa em memória.
    """
    digitos = re.sub(r"\D", "", str(cnpj or ""))
    if len(digitos) != 14:
        return None, "Informe os 14 dígitos do CNPJ para buscar os dados da Receita."
    try:
        resposta = requests.get(
            EMPRESA_CONSULTAR_CNPJ_URL,
            params={"cnpj": digitos},
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=45,
        )
    except Exception as erro:
        _log_chamada_api("GET", EMPRESA_CONSULTAR_CNPJ_URL, erro=erro)
        return None, f"Não foi possível consultar a Receita agora: {erro}"
    _log_chamada_api(
        "GET",
        EMPRESA_CONSULTAR_CNPJ_URL,
        status_code=resposta.status_code,
        corpo=_resumo_corpo_api(resposta),
    )
    if resposta.status_code != 200:
        return None, mensagem_de_erro(resposta)
    try:
        dados = resposta.json() or {}
    except ValueError:
        return None, "A API respondeu à consulta do CNPJ em formato inesperado."
    if not isinstance(dados, dict):
        return None, "A API respondeu à consulta do CNPJ em formato inesperado."
    return dados, None


@st.cache_data(ttl=600, show_spinner=False)
def consultar_cep_api(cep, token):
    """GET ``/api/empresas/consultar-cep``: rua/bairro/município/UF do CEP.

    Devolve ``(dados, erro)``. O cache de 10 minutos evita repetir a consulta a cada
    interação da tela; o backend também guarda a resposta boa em memória.
    """
    digitos = re.sub(r"\D", "", str(cep or ""))
    if len(digitos) != 8:
        return None, "Informe os 8 dígitos do CEP."
    try:
        resposta = requests.get(
            EMPRESA_CONSULTAR_CEP_URL,
            params={"cep": digitos},
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=45,
        )
    except Exception as erro:
        _log_chamada_api("GET", EMPRESA_CONSULTAR_CEP_URL, erro=erro)
        return None, f"Não foi possível consultar o CEP agora: {erro}"
    _log_chamada_api(
        "GET",
        EMPRESA_CONSULTAR_CEP_URL,
        status_code=resposta.status_code,
        corpo=_resumo_corpo_api(resposta),
    )
    if resposta.status_code != 200:
        return None, mensagem_de_erro(resposta)
    try:
        dados = resposta.json() or {}
    except ValueError:
        return None, "A API respondeu à consulta do CEP em formato inesperado."
    if not isinstance(dados, dict):
        return None, "A API respondeu à consulta do CEP em formato inesperado."
    return dados, None


def _aplicar_cep_minhas_lojas():
    """Preenche o endereço fiscal a partir do CEP (callback do campo, fora do form).

    Roda ANTES de o `st.form` ser desenhado no run, então os widgets de
    rua/bairro/município/UF ainda podem receber valor. O código IBGE não tem widget:
    vai para ``nova_loja_codigo_municipio`` na sessão e é lido no submit para o
    payload da ACBr (sem isso a ACBr recusa o cadastro por endereço incompleto).
    """
    digitos = re.sub(r"\D", "", str(st.session_state.get("nova_loja_cep") or ""))
    if len(digitos) != 8:
        st.session_state[DADOS_CEP_MINHAS_LOJAS] = {
            "erro": "Informe os 8 dígitos do CEP para buscar o endereço."
        }
        return
    dados, erro = consultar_cep_api(digitos, st.session_state.get(SESSAO_TOKEN))
    if erro:
        st.session_state[DADOS_CEP_MINHAS_LOJAS] = {"erro": erro, "cep": digitos}
        return
    if not dados.get("encontrado"):
        st.session_state[DADOS_CEP_MINHAS_LOJAS] = {
            "erro": dados.get("aviso") or "CEP não encontrado.",
            "cep": digitos,
        }
        return
    endereco = dados.get("endereco") or {}
    marcados = set(st.session_state.get(CAMPOS_PREENCHIDOS_PELO_CEP) or [])
    preenchidos = []
    for campo, chave in CAMPOS_ENDERECO_MINHAS_LOJAS.items():
        texto = str(endereco.get(campo) or "").strip()
        if not texto:
            continue
        atual = str(st.session_state.get(chave) or "").strip()
        # Nunca sobrescreve o que o cliente digitou à mão; valores que ESTE callback
        # preencheu antes (troca de CEP) podem ser atualizados.
        if atual and campo not in marcados and campo != "codigo_municipio":
            continue
        st.session_state[chave] = texto
        marcados.add(campo)
        preenchidos.append(campo)
    st.session_state[CAMPOS_PREENCHIDOS_PELO_CEP] = sorted(marcados)
    st.session_state[DADOS_CEP_MINHAS_LOJAS] = {
        "cep": digitos,
        "fonte": str(dados.get("fonte") or ""),
        "preenchidos": preenchidos,
        "erro": None,
    }


def _aplicar_dados_cnpj_minhas_lojas(cnpj=None):
    """Consulta o CNPJ e grava razão social/endereço na sessão ANTES do formulário.

    É chamada pelo `on_change` do campo CNPJ (que fica FORA do `st.form`) e pelo botão
    "🔎 Buscar dados". Callbacks e botões rodam antes dos widgets do formulário
    existirem no run, que é a única hora em que o Streamlit aceita trocar o valor de
    um widget (`st.session_state[chave] = valor`). Dentro do `st.form` nada dispara
    até o submit — por isso o CNPJ saiu de lá: com ele dentro, o endereço nunca era
    preenchido e o cadastro chegava à ACBr incompleto.
    """
    valor = cnpj if cnpj is not None else st.session_state.get("nova_loja_cnpj")
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) != 14:
        st.session_state[DADOS_CNPJ_MINHAS_LOJAS] = {
            "erro": "Informe os 14 dígitos do CNPJ para buscar os dados da Receita."
        }
        return
    dados, erro = consultar_dados_cnpj_api(digitos, st.session_state.get(SESSAO_TOKEN))
    if erro:
        st.session_state[DADOS_CNPJ_MINHAS_LOJAS] = {"erro": erro, "cnpj": digitos}
        return

    endereco = dados.get("endereco") or {}
    preenchidos = []
    for campo, chave in CAMPOS_ENDERECO_MINHAS_LOJAS.items():
        texto = str(endereco.get(campo) or "").strip()
        if not texto:
            continue
        st.session_state[chave] = texto
        preenchidos.append(campo)
    st.session_state[DADOS_CNPJ_MINHAS_LOJAS] = {
        "cnpj": digitos,
        "razao_social": str(dados.get("razao_social") or ""),
        "nome_loja": str(dados.get("nome_loja") or ""),
        "fonte": str(dados.get("fonte") or ""),
        "aviso": dados.get("aviso"),
        "encontrado": bool(dados.get("encontrado")),
        "da_conta": bool(dados.get("da_conta")),
        "de_outra_conta": bool(dados.get("de_outra_conta")),
        "dono_email": str(dados.get("dono_email") or ""),
        "preenchidos": preenchidos,
    }


def _renderizar_dados_cnpj(dados):
    """Resumo da consulta do CNPJ acima do formulário (só mensagens, sem JSON)."""
    if not isinstance(dados, dict) or not dados:
        return
    if dados.get("erro"):
        st.warning(str(dados["erro"]))
        return
    if dados.get("de_outra_conta"):
        dono_email = str(dados.get("dono_email") or "").strip()
        if not dono_email:
            st.error(
                "Este CNPJ já está vinculado a outra conta do sistema. "
                "Cada CNPJ pertence a um único cliente."
            )
            return
        # Administrador: a loja é de um cliente, mas ele pode reenviar o
        # certificado (o backend mantém o dono atual).
        st.warning(
            f"🔑 Este CNPJ já está vinculado à conta **{dono_email}**. Como "
            "administrador, o envio abaixo atualiza **somente o certificado** — a "
            "loja e os relatórios continuam com o cliente."
        )
    nome = str(dados.get("nome_loja") or dados.get("razao_social") or "").strip()
    if dados.get("encontrado"):
        st.success(
            f"🏢 **{nome or 'CNPJ localizado'}** — dados da Receita Federal carregados."
        )
        if dados.get("preenchidos"):
            st.caption(
                "Endereço fiscal preenchido automaticamente. Confira o **número** e o "
                "**complemento** (a Receita costuma devolvê-los vazios)."
            )
        if dados.get("aviso"):
            st.warning(str(dados["aviso"]))
        return
    st.warning(
        "A Receita não respondeu agora. Confira o **📍 Endereço fiscal** antes de "
        "salvar: é ele que vai para o cadastro da ACBr."
    )


def _erro_acbr_pede_endereco(texto):
    """True quando a recusa da ACBr é por cadastro/endereço incompleto."""
    alvo = limpar_texto(texto)
    return any(
        pista in alvo
        for pista in ("ENDERECO", "LOGRADOURO", "COMPLETE O CADASTRO", "DADOS DA EMPRESA")
    )


def _mensagem_vinculo_loja(cnpj, nome_loja="", admin=False):
    """Texto do "vinculado com sucesso" da tela Minhas Lojas (CNPJ + loja).

    Fica pronto antes do `st.rerun()` do envio e reaparece na lista no run
    seguinte — é a confirmação que fecha o fluxo de vincular o CNPJ.
    """
    numero = formatar_cnpj(cnpj) if cnpj else "CNPJ"
    nome = str(nome_loja or "").strip()
    texto = (
        f"✅ CNPJ {numero} vinculado com sucesso"
        + (f" — loja **{nome}**." if nome else ".")
    )
    if admin:
        texto += " (administrador: o certificado foi atualizado na loja do cliente.)"
    return texto


def _painel_certificado_loja(empresa, versao):
    """Aviso + concordância obrigatória + status do certificado A1 da loja."""
    empresa_id = empresa.get("id")
    certificado = empresa.get("certificado") or {}
    concordancia = empresa.get("cert_concordancia") or {}

    st.warning(
        "⚠️ **Atenção antes de ativar o certificado digital:** a SEFAZ entrega "
        "cada nota para **um único sistema**. Baixar as mesmas notas em dois "
        "lugares (aqui e na contabilidade/retaguarda) faz um dos sistemas perder "
        "a distribuição.\n\n"
        + TEXTO_CONCORDANCIA_SEFAZ
    )
    if certificado.get("validade"):
        st.caption(
            f"🔐 Certificado A1 válido até {certificado['validade']} "
            f"(enviado em {formatar_data(certificado.get('enviado_em'))})."
        )
    else:
        st.warning(
            "Esta loja ainda não tem o certificado A1 cadastrado. Depois de "
            "confirmar a concordância, envie o .pfx/.p12 e a senha no formulário "
            "**Vincular novo CNPJ** abaixo para ativar o download."
        )
    if concordancia.get("em"):
        st.success(
            "✅ Concordância registrada em "
            f"{_formatar_data_hora_brasilia(concordancia['em'])}"
            + (f" por {concordancia['por']}." if concordancia.get("por") else ".")
        )
    else:
        st.caption("A concordância ainda não foi registrada para esta loja.")

    confirmar = st.checkbox(
        TEXTO_CONCORDANCIA_SEFAZ,
        key=f"concordancia_sefaz_{empresa_id}_{versao}",
        help=(
            "O aceite fica gravado com data/hora e o e-mail da conta logada. "
            "Sem ele o certificado não é ativado."
        ),
    )
    if st.button(
        "💾 Confirmar e ativar fonte Certificado A1",
        key=f"btn_fonte_certificado_{empresa_id}",
        width="stretch",
        disabled=not confirmar,
        help="O botão só habilita depois de marcar a concordância.",
    ):
        dados, erro = definir_fonte_notas_loja(empresa_id, "certificado", True)
        if erro:
            st.error(erro)
        else:
            st.session_state[MENSAGEM_FONTE_NOTAS] = (
                dados.get("mensagem")
                or "Fonte das notas definida como certificado digital A1 (SEFAZ)."
            )
            _incrementar_versao_fonte_notas()
            st.cache_data.clear()
            st.rerun()


def _secao_fonte_notas(empresas):
    """Seletor "Fonte das notas" por loja (Omie x certificado A1).

    A lista `empresas` já vem recortada pelo backend: a conta comum vê só as
    próprias lojas; o administrador vê todas (com o e-mail do dono no rótulo) e
    pode gerenciar cada uma, como no restante da tela.
    """
    lojas = [empresa for empresa in empresas if empresa.get("id") is not None]
    if not lojas:
        return
    mapa = {int(empresa["id"]): empresa for empresa in lojas}
    ids = list(mapa)

    st.subheader("⚙️ Fonte das notas por loja")
    st.caption(
        "Cada loja baixa as notas de **um único lugar**. Escolha **Omie** quando "
        "os recebimentos já estão no Omie ou **Certificado A1** para baixar "
        "direto da SEFAZ — nunca os dois para a mesma empresa."
    )
    selecionado = st.session_state.get(SESSAO_LOJA_FONTE_NOTAS)
    if selecionado not in ids:
        st.session_state[SESSAO_LOJA_FONTE_NOTAS] = ids[0]
    escolhido = st.selectbox(
        "Loja",
        ids,
        key=SESSAO_LOJA_FONTE_NOTAS,
        format_func=lambda empresa_id: _rotulo_loja_fonte(mapa[int(empresa_id)]),
    )
    empresa = mapa[int(escolhido)]
    versao = st.session_state.get(CHAVE_VERSAO_FONTE_NOTAS, 0)
    if not isinstance(versao, int):
        versao = 0
    atual = str(empresa.get("fonte_notas") or "certificado").lower()
    fonte_rotulo = st.radio(
        "Fonte das notas desta loja",
        list(FONTES_NOTAS),
        index=0 if atual == "omie" else 1,
        key=f"{PREFIXO_WIDGET_FONTE_NOTAS}{empresa['id']}_{versao}",
        horizontal=True,
        help=(
            "Omie: importa os recebimentos pela API. Certificado A1: baixa os "
            "XMLs direto da Distribuição NF-e da SEFAZ."
        ),
    )
    if FONTES_NOTAS.get(fonte_rotulo) == "omie":
        _painel_omie_loja(empresa)
    else:
        _painel_certificado_loja(empresa, versao)


def _painel_omie_loja(empresa):
    """Credenciais + sincronização manual da fonte Omie de UMA loja."""
    empresa_id = empresa.get("id")
    cnpj = empresa.get("cnpj") or ""
    status, erro_status = consultar_status_omie(
        cnpj, st.session_state.get(SESSAO_TOKEN)
    )
    if erro_status:
        st.warning(erro_status)
    else:
        _renderizar_resumo_omie(status)
    erro_credenciais = st.session_state.pop(ERRO_CREDENCIAIS_OMIE, None)
    if erro_credenciais:
        st.error(str(erro_credenciais))

    st.caption(
        "As credenciais são do aplicativo criado em **app.omie.com.br** e ficam "
        "**cifradas no banco** — o App Secret não é exibido de volta."
    )
    # O campo NUNCA recebe `value=`: o segredo gravado não volta do backend, só a
    # máscara. O placeholder só avisa que já existe um segredo salvo.
    segredo_configurado = bool(
        isinstance(status, dict) and status.get("app_secret_configurado")
    )
    placeholder_segredo = (
        "•••••••• (já configurado — digite para trocar)"
        if segredo_configurado
        else "App Secret do aplicativo Omie"
    )
    with st.form(key=f"form_credenciais_omie_{empresa_id}"):
        app_key = st.text_input(
            "App Key do Omie",
            key=f"omie_app_key_{empresa_id}",
            help="Chave do aplicativo Omie desta loja.",
        )
        app_secret = st.text_input(
            "App Secret do Omie",
            type="password",
            key=f"{PREFIXO_APP_SECRET_OMIE}{empresa_id}",
            placeholder=placeholder_segredo,
            help=(
                "Segredo do mesmo aplicativo. Gravado cifrado e nunca reexibido; "
                "o campo sempre nasce vazio."
            ),
        )
        salvar = st.form_submit_button("💾 Salvar credenciais do Omie")
    if salvar:
        chave_texto = str(app_key or "").strip()
        segredo_texto = str(app_secret or "").strip()
        # Qualquer submit limpa o segredo no run seguinte (dê certo ou não): o
        # campo com "olhinho" nunca deve continuar revelável depois do envio.
        st.session_state[LIMPAR_APP_SECRET_OMIE] = True
        if not chave_texto or not segredo_texto:
            st.session_state[ERRO_CREDENCIAIS_OMIE] = (
                "Informe o App Key e o App Secret do Omie para salvar."
            )
            st.rerun()
        else:
            dados, erro = salvar_credenciais_omie(cnpj, chave_texto, segredo_texto)
            if erro:
                st.session_state[ERRO_CREDENCIAIS_OMIE] = erro
                st.rerun()
            else:
                st.session_state[MENSAGEM_FONTE_NOTAS] = (
                    dados.get("mensagem")
                    or "Credenciais do Omie gravadas (cifradas no banco)."
                )
                # O backend também ativa a fonte Omie; a versão nova faz o radio
                # e o cartão voltarem com o valor gravado.
                _incrementar_versao_fonte_notas()
                st.cache_data.clear()
                st.rerun()

    st.divider()
    simular = st.checkbox(
        "Simular (busca os recebimentos e não grava nada)",
        value=True,
        key=f"omie_simular_{empresa_id}",
        help=(
            "A simulação é o teste seguro: mostra quantas notas/itens entrariam "
            "sem alterar a base. Desmarque para gravar de verdade."
        ),
    )
    if st.button(
        "🔄 Sincronizar agora",
        key=f"btn_sincronizar_omie_{empresa_id}",
        width="stretch",
        help="Usa a janela padrão e as etapas do backend (Faturado/Recebido/Conferido).",
    ):
        with st.spinner(
            "Consultando os recebimentos do Omie... A simulação também pode "
            "levar alguns minutos."
        ):
            dados, erro = sincronizar_omie(cnpj, simular=simular)
        if erro:
            st.error(erro)
        else:
            contagens = dados.get("contagens") or {}
            rotulo = (
                "Simulação concluída (nada foi gravado)"
                if dados.get("simulado")
                else "Sincronização concluída"
            )
            st.success(
                f"{rotulo}: {int(contagens.get('novas') or 0)} nota(s) nova(s), "
                f"{int(contagens.get('duplicadas') or 0)} duplicada(s) e "
                f"{int(contagens.get('itens') or 0)} item(ns)."
            )
            resumo = dados.get("resumo") or {}
            erros = resumo.get("erros") or []
            if erros:
                with st.expander(f"⚠️ Avisos da sincronização ({len(erros)})"):
                    for aviso in erros:
                        st.caption(f"• {aviso}")
            if not dados.get("simulado"):
                # Os relatórios passam a refletir as notas importadas.
                st.cache_data.clear()


def _rotulo_loja_fonte(empresa):
    """Rótulo do seletor de fonte: loja + CNPJ (+ dono, na visão do admin)."""
    nome = empresa.get("nome_loja") or empresa.get("razao_social") or "Loja"
    rotulo = f"{nome} — {formatar_cnpj(empresa.get('cnpj'))}"
    dono = str(empresa.get("dono_email") or "").strip()
    if dono:
        rotulo += f" (conta: {dono})"
    return rotulo


def _renderizar_resumo_omie(status):
    """Resumo da integração Omie no painel da loja (sem expor o app_secret)."""
    if not isinstance(status, dict) or not status:
        return
    erro_credenciais = status.get("erro_credenciais")
    if erro_credenciais:
        st.error(
            "A credencial do Omie gravada nesta loja não pôde ser lida "
            f"({erro_credenciais}). Grave o App Key/App Secret novamente."
        )
    if not status.get("configurado"):
        st.info(
            "Nenhuma credencial do Omie configurada para esta loja. Grave o "
            "**App Key** e o **App Secret** abaixo (ficam cifrados no banco)."
        )
        return
    origem = status.get("origem_credenciais")
    onde = (
        "gravada nesta loja"
        if origem == "empresa"
        else "vinda do .env do servidor (loja ainda sem credencial própria)"
    )
    st.caption(
        f"🔑 App Key `{status.get('app_key') or '—'}` — {onde} | "
        f"segredo {'configurado' if status.get('app_secret_configurado') else 'ausente'}"
    )
    ultima = status.get("ultima_sincronizacao") or {}
    sincronizado = status.get("omie_sync_em") or ultima.get("sincronizado_em")
    if sincronizado:
        st.caption(
            "🕒 Última sincronização: "
            f"{_formatar_data_hora_brasilia(sincronizado)}"
        )
    else:
        st.caption("🕒 Nenhuma sincronização concluída ainda.")
    contagens = status.get("contagens") or {}
    if contagens:
        st.caption(
            f"📄 Notas novas: {int(contagens.get('novas') or 0)} | "
            f"duplicadas: {int(contagens.get('duplicadas') or 0)} | "
            f"itens: {int(contagens.get('itens') or 0)} | "
            f"recebimentos: {int(contagens.get('recebimentos') or 0)}"
        )
    erros = ultima.get("erros") or []
    if erros:
        with st.expander(f"⚠️ Avisos da última sincronização ({len(erros)})"):
            for aviso in erros:
                st.caption(f"• {aviso}")



def renderizar_minhas_lojas(empresas, erro_api=None):
    """Tela 'Minhas Lojas': CNPJs da conta e cadastro de novas lojas."""
    # A tela fica marcada como a página atual em todo run: o roteador cai em
    # `PAGINA_RELATORIOS` quando a sessão perde a página.
    st.session_state[SESSAO_PAGINA] = PAGINA_MINHAS_LOJAS
    st.title("🏬 Minhas Lojas")
    if usuario_e_admin():
        # Visão de suporte: o admin (rodrigo) vê e gerencia TODAS as lojas, de todas
        # as contas. A API devolve `dono_email` só para ele e as ações de certificado
        # são liberadas no backend (`_pode_gerenciar_loja`).
        st.caption(
            "**Visão do administrador:** todas as lojas de todas as contas, com o "
            "e-mail do dono em cada cartão. Você pode conferir, reenviar (digitando o "
            "CNPJ + certificado no formulário abaixo) e excluir o certificado de "
            "qualquer loja sem entrar na conta do cliente."
        )
    else:
        st.caption(
            "CNPJs da sua conta. Cada nota fiscal é ligada à loja pelo CNPJ destinatário "
            "do XML, então cadastrar o CNPJ aqui já libera os relatórios das notas "
            "importadas."
        )
    # Log no console do Streamlit: o ID abaixo deve ser o mesmo `usuario_id`
    # impresso pela API em "Buscando empresas para usuario_id=...". É o que separa
    # "conta errada" de "loja não cadastrada" no suporte.
    usuario_sessao = usuario_logado() or {}
    print(
        "[Minhas Lojas] "
        f"usuario_sessao_id={usuario_sessao.get('id')} "
        f"email={usuario_sessao.get('email')} "
        f"token_presente={bool(st.session_state.get(SESSAO_TOKEN))} "
        f"lojas_recebidas={len(empresas or [])}",
        flush=True,
    )
    if erro_api:
        st.error(f"A lista de lojas não pôde ser carregada: {erro_api}")
    # A limpeza acontece ANTES de criar os widgets: o Streamlit não permite alterar
    # o valor de um campo já instanciado no mesmo run. O cadastro marca o flag e o
    # próximo run (clique em "🔄 Atualizar Lista" ou qualquer interação) recria
    # CNPJ, arquivo, senha e confirmações vazios/desmarcados.
    if st.session_state.pop(LIMPAR_FORM_MINHAS_LOJAS, False):
        _limpar_formulario_minhas_lojas()
    if st.session_state.pop(LIMPAR_APP_SECRET_OMIE, False):
        # Segredo do Omie já enviado: some da sessão antes do campo ser recriado.
        _limpar_app_secret_omie()
    if st.session_state.pop(LIMPAR_CONFIRMACAO_EXCLUSAO_CERT, False):
        _limpar_confirmacoes_exclusao_certificado()

    aviso_cadastro = st.session_state.pop(MENSAGEM_MINHAS_LOJAS, None)
    if aviso_cadastro:
        st.success(aviso_cadastro)
    aviso_fonte = st.session_state.pop(MENSAGEM_FONTE_NOTAS, None)
    if aviso_fonte:
        st.success(aviso_fonte)
    resultado_cadastro = st.session_state.pop(RESULTADO_CADASTRO_MINHAS_LOJAS, None)
    if isinstance(resultado_cadastro, dict) and resultado_cadastro:
        nome_nova = str(
            resultado_cadastro.get("nome_loja")
            or resultado_cadastro.get("razao_social")
            or ""
        ).strip()
        st.caption(
            f"CNPJ {formatar_cnpj(resultado_cadastro.get('cnpj'))}"
            f" | loja ID {resultado_cadastro.get('id')}"
            + (f" | {nome_nova}" if nome_nova else "")
        )
        if resultado_cadastro.get("mensagem"):
            st.caption(str(resultado_cadastro["mensagem"]))

    # Diagnóstico de rede da tela saiu daqui (decisão do cliente): os detalhes de
    # cada envio continuam no expander "🛠 Diagnóstico do envio do cadastro" e no log
    # da API, sem botões de teste nem o endereço da API expostos ao cliente.
    st.divider()

    if empresas:
        st.subheader("CNPJs vinculados")
        mostrar_acoes_acbr = usuario_e_admin()
        colunas = st.columns(3)
        for indice, empresa in enumerate(empresas):
            with colunas[indice % 3]:
                _cartao_empresa(empresa, mostrar_acoes_acbr=mostrar_acoes_acbr)
    else:
        st.warning(
            "Nenhuma loja encontrada para este usuário. Verifique se o CNPJ foi cadastrado "
            "corretamente."
        )
        st.caption(
            f"Conta consultada: {usuario_sessao.get('email') or 'sem e-mail'} "
            f"(ID {usuario_sessao.get('id')})."
        )

    if empresas:
        # Fonte das notas de cada loja (Omie x certificado A1) + concordância
        # obrigatória do aviso da SEFAZ. Fica acima do cadastro de CNPJ novo.
        _secao_fonte_notas(empresas)

    st.divider()
    st.subheader("➕ Vincular novo CNPJ")
    st.caption(
        "Buscamos os dados da empresa na Receita Federal e vinculamos o CNPJ à sua "
        "conta. Lojas importadas antes do login são adotadas pela conta ao cadastrar o CNPJ."
    )
    # ── CNPJ FORA do `st.form` ─────────────────────────────────────────────────
    # Fora do formulário o campo dispara run ao ser confirmado (Enter/saída do campo),
    # e é isso que permite consultar a Receita e preencher o endereço ANTES de o
    # formulário existir no run. Dentro do `st.form` nada dispara até o submit: os
    # campos de endereço nasciam vazios e o cadastro chegava à ACBr incompleto.
    cnpj = st.text_input(
        "CNPJ",
        key="nova_loja_cnpj",
        placeholder="00.000.000/0000-00",
        max_chars=18,
        on_change=_aplicar_dados_cnpj_minhas_lojas,
        help="Digite o CNPJ e confirme (Enter): os dados da Receita são carregados sozinhos.",
    )
    coluna_buscar, coluna_dica = st.columns([1, 2])
    if coluna_buscar.button(
        "🔎 Buscar dados do CNPJ", key="btn_buscar_cnpj_loja", width="stretch"
    ):
        _aplicar_dados_cnpj_minhas_lojas(cnpj)
    coluna_dica.caption(
        "Razão social e endereço fiscal vêm da Receita Federal. Se ela não responder, "
        "informe o **CEP** para completar o endereço."
    )
    _renderizar_dados_cnpj(st.session_state.get(DADOS_CNPJ_MINHAS_LOJAS))

    # ── Endereço FORA do `st.form` ─────────────────────────────────────────────
    # O CEP precisa de `on_change` para preencher rua/bairro/município/UF; dentro do
    # formulário nenhum campo dispara até o submit (era por isso que o endereço
    # continuava vazio e a ACBr recusava o cadastro). O código IBGE não tem campo: vem
    # da Receita/CEP e fica só na sessão — a ACBr exige os 7 dígitos, mas o cliente
    # não precisa digitá-los.
    with st.expander(
        "📍 Endereço fiscal (usado no cadastro da ACBr)",
        expanded=bool(st.session_state.get(ABRIR_ENDERECO_MINHAS_LOJAS)),
    ):
        st.caption(
            "Preenchido pela Receita ao confirmar o CNPJ e pelo CEP abaixo. Confira o "
            "**número** e o **complemento**: é este endereço que vai para a ACBr."
        )
        coluna_cep, coluna_numero = st.columns(2)
        cep = coluna_cep.text_input(
            "CEP",
            key="nova_loja_cep",
            max_chars=9,
            placeholder="00000-000",
            on_change=_aplicar_cep_minhas_lojas,
            help=(
                "Digite o CEP e confirme (Enter): rua, bairro, município e UF são "
                "preenchidos automaticamente."
            ),
        )
        numero = coluna_numero.text_input(
            "Número", key="nova_loja_numero", max_chars=10
        )
        logradouro = st.text_input("Logradouro", key="nova_loja_logradouro")
        coluna_complemento, coluna_bairro = st.columns(2)
        complemento = coluna_complemento.text_input(
            "Complemento", key="nova_loja_complemento"
        )
        bairro = coluna_bairro.text_input("Bairro", key="nova_loja_bairro")
        coluna_cidade, coluna_uf = st.columns(2)
        cidade = coluna_cidade.text_input("Município", key="nova_loja_municipio")
        uf = coluna_uf.text_input("UF", key="nova_loja_uf", max_chars=2)
        codigo_municipio = str(
            st.session_state.get("nova_loja_codigo_municipio") or ""
        )
    dados_cep = st.session_state.pop(DADOS_CEP_MINHAS_LOJAS, None)
    if isinstance(dados_cep, dict) and dados_cep:
        if dados_cep.get("erro"):
            st.warning(str(dados_cep["erro"]))
        else:
            st.caption(
                "Endereço preenchido pelo CEP. Confira o número e o complemento."
            )

    # ── Fonte da nova loja FORA do `st.form` ───────────────────────────────────
    # O radio precisa de run para trocar o formulário (upload do certificado x
    # App Key/App Secret do Omie); dentro de um `st.form` nada dispara até o
    # submit. O padrão é o certificado (fluxo histórico).
    fonte_nova_rotulo = st.radio(
        "Como as notas fiscais desta loja serão baixadas?",
        [ROTULO_FONTE_OMIE, ROTULO_FONTE_CERTIFICADO],
        index=1,
        key=NOVA_LOJA_FONTE_NOTAS,
        horizontal=True,
        help=(
            "Omie: importa os recebimentos pela API. Certificado A1: baixa os "
            "XMLs direto da SEFAZ. Escolha apenas um — as mesmas notas não podem "
            "ser baixadas em dois sistemas."
        ),
    )
    fonte_nova = FONTES_NOTAS.get(fonte_nova_rotulo, "certificado")
    if fonte_nova == "omie":
        st.caption(
            "🔑 **Fonte Omie:** informe o App Key e o App Secret do aplicativo "
            "criado em app.omie.com.br. O certificado A1 não é necessário e os "
            "segredos ficam **cifrados no banco**. Depois de vincular, a loja "
            "aparece acima para sincronizar os recebimentos."
        )
    else:
        st.caption(
            "🔐 **Certificado digital obrigatório:** envie o certificado A1 da "
            "empresa (.pfx/.p12) e a senha dele, depois de confirmar a "
            "concordância com o aviso da SEFAZ. Sem o arquivo e a senha o "
            "servidor recusa o vínculo. Depois de salvar, o sistema baixa e "
            "importa as notas automaticamente."
        )
    # `st.form`: certificado/credenciais, senha e o botão de salvar ficam juntos
    # para o envio só acontecer no submit (sem run intermediário durante a janela
    # do arquivo). O endereço está FORA do formulário porque o CEP precisa de
    # `on_change`; os valores continuam nos widgets/sessão até o clique em
    # "Salvar e vincular loja".
    certificado = None
    senha_certificado = ""
    concordancia_nova = False
    omie_app_key = ""
    omie_app_secret = ""
    with st.form(key="form_vincular_loja", clear_on_submit=False):
        if fonte_nova == "omie":
            omie_app_key = st.text_input(
                "App Key do Omie",
                key=NOVA_LOJA_OMIE_APP_KEY,
                help="Chave do aplicativo Omie desta loja.",
            )
            omie_app_secret = st.text_input(
                "App Secret do Omie",
                type="password",
                key=NOVA_LOJA_OMIE_APP_SECRET,
                help="Segredo do mesmo aplicativo. Gravado cifrado e nunca reexibido.",
            )
        else:
            certificado = st.file_uploader(
                "Certificado digital A1 (.pfx ou .p12)",
                type=["pfx", "p12"],
                key=_chave_uploader_certificado(),
                help=(
                    "Arquivo do certificado A1 da empresa. O backend confere a senha e guarda "
                    "apenas o titular e a validade — o arquivo não é gravado no banco."
                ),
            )
            senha_certificado = st.text_input(
                "Senha do certificado digital",
                type="password",
                key="nova_loja_senha_certificado",
                help="A mesma senha usada ao exportar/instalar o .pfx. Também não é gravada.",
            )
            st.warning(
                "⚠️ **Antes de ativar o certificado:** a SEFAZ entrega cada nota "
                "para **um único sistema**. Baixar as mesmas notas aqui e na "
                "contabilidade/retaguarda faz um dos sistemas perder a "
                "distribuição.\n\n" + TEXTO_CONCORDANCIA_SEFAZ
            )
            concordancia_nova = st.checkbox(
                TEXTO_CONCORDANCIA_SEFAZ,
                key=NOVA_LOJA_CONCORDANCIA_SEFAZ,
                help=(
                    "O aceite fica gravado com data/hora e o e-mail da conta "
                    "logada. Sem ele o certificado não é salvo."
                ),
            )
        vincular = st.form_submit_button(
            "💾 Salvar e vincular loja", key="btn_vincular_loja", type="primary"
        )
    if not vincular:
        return
    if fonte_nova == "omie":
        # Vínculo SEM certificado: o backend aceita `fonte_notas="omie"` e a
        # credencial é gravada na chamada seguinte (cifrada). Sem as duas chaves
        # não há como sincronizar — a tela pede antes de chamar a API.
        digitos_omie = re.sub(r"\D", "", cnpj or "")
        if not str(omie_app_key or "").strip() or not str(
            omie_app_secret or ""
        ).strip():
            st.error(
                "Informe o App Key e o App Secret do Omie para vincular a loja "
                "com essa fonte."
            )
            return
        empresa_omie, _mensagem_omie, erro_omie = cadastrar_loja(
            digitos_omie,
            None,
            "",
            fonte_notas="omie",
            enviar_acbr=False,
        )
        if erro_omie:
            st.error(erro_omie)
            return
        _dados_credenciais, erro_credenciais = salvar_credenciais_omie(
            digitos_omie, omie_app_key, omie_app_secret
        )
        confirmada = empresa_omie or {}
        cnpj_confirmado = confirmada.get("cnpj") or digitos_omie
        nome_confirmado = confirmada.get("nome_loja") or ""
        st.session_state[RESULTADO_CADASTRO_MINHAS_LOJAS] = {
            "id": confirmada.get("id"),
            "cnpj": cnpj_confirmado,
            "nome_loja": nome_confirmado,
            "razao_social": confirmada.get("razao_social") or "",
            "mensagem": "Fonte das notas: Omie.",
        }
        mensagem_final = _mensagem_vinculo_loja(cnpj_confirmado, nome_confirmado)
        if erro_credenciais:
            mensagem_final += (
                " ⚠️ A loja foi vinculada, mas as credenciais do Omie não foram "
                f"gravadas: {erro_credenciais}. Use o painel da loja acima para "
                "tentar novamente."
            )
        else:
            mensagem_final += (
                " Credenciais do Omie gravadas (cifradas no banco). Fonte das "
                "notas: **Omie**."
            )
        st.session_state[MENSAGEM_MINHAS_LOJAS] = mensagem_final
        # O formulário é limpo no PRÓXIMO run (o Streamlit não permite limpar um
        # widget já instanciado no mesmo run).
        st.session_state[LIMPAR_FORM_MINHAS_LOJAS] = True
        st.session_state[LIMPAR_CONFIRMACAO_EXCLUSAO_CERT] = True
        st.session_state.pop(DADOS_CNPJ_MINHAS_LOJAS, None)
        st.session_state[ABRIR_ENDERECO_MINHAS_LOJAS] = False
        st.cache_data.clear()
        st.rerun()
        return
    if not concordancia_nova:
        # Regra da tela (e do backend): sem o aceite o certificado não é salvo.
        st.error(
            "Marque a concordância de que as notas fiscais desta empresa NÃO são "
            "baixadas em nenhum outro sistema antes de salvar o certificado."
        )
        return
    # Endereço informado à mão: vai no POST da ACBr e o backend persiste na loja
    # (o formulário tem prioridade sobre o endereço já gravado e sobre a Receita).
    endereco_fiscal = {
        "cep": re.sub(r"\D", "", cep or ""),
        "logradouro": (logradouro or "").strip(),
        "numero": (numero or "").strip(),
        "complemento": (complemento or "").strip(),
        "bairro": (bairro or "").strip(),
        "cidade": (cidade or "").strip(),
        "uf": (uf or "").strip().upper(),
        "codigo_municipio": re.sub(r"\D", "", codigo_municipio or ""),
    }
    # ── Daqui para baixo NADA impede a chamada ────────────────────────
    # CNPJ vazio/curto, certificado ausente ou senha em branco são recusados pelo
    # backend (HTTP 400 com `detail`), não pela tela: o clique sempre chega à API e
    # o erro amigável aparece na tela (o JSON cru fica no expander de diagnóstico).
    digitos = re.sub(r"\D", "", cnpj or "")
    nome_arquivo = getattr(certificado, "name", "") if certificado is not None else ""
    try:
        conteudo_certificado = (
            certificado.getvalue()
            if certificado is not None and hasattr(certificado, "getvalue")
            else None
        )
    except Exception:
        conteudo_certificado = None
    diagnostico = {
        "api_base": API_BASE,
        "cnpj": digitos or cnpj,
        "url_vinculo": EMPRESA_CADASTRAR_URL,
        "url_acbr": EMPRESA_ACBR_URL,
        "certificado": nome_arquivo,
        "certificado_bytes": len(conteudo_certificado or b""),
        "senha_informada": bool((senha_certificado or "").strip()),
        "endereco_informado": {
            campo: valor for campo, valor in endereco_fiscal.items() if valor
        },
        "token_enviado": bool(st.session_state.get(SESSAO_TOKEN)),
        "etapas": [],
        "respostas": {},
    }

    # ── 1) POST /api/empresas/cadastrar: cria/adota a loja e valida o certificado.
    # Se esta etapa falhar, a etapa 2 ainda cria a loja pela própria rota da ACBr.
    empresa_criada, mensagem, erro_vinculo = None, None, None
    try:
        with st.spinner("Vinculando o CNPJ à sua conta..."):
            empresa_criada, mensagem, erro_vinculo = cadastrar_loja(
                digitos,
                certificado,
                senha_certificado,
                diagnostico=diagnostico,
                enviar_acbr=False,
                concordancia_sefaz=concordancia_nova,
            )
    except Exception as e:  # noqa: BLE001 - a tela nunca pode falhar calada
        st.error(f"Erro: {e}")
        erro_vinculo = str(e)

    resposta_vinculo = diagnostico["respostas"].get("vinculo")
    corpo_vinculo = None
    if resposta_vinculo is not None:
        try:
            corpo_vinculo = resposta_vinculo.json()
        except ValueError:
            corpo_vinculo = None
        # Receita indisponível (429/offline): a loja foi criada, mas pode faltar
        # endereço. O aviso do backend diz o que fazer antes da ACBr recusar.
        if isinstance(corpo_vinculo, dict) and corpo_vinculo.get("aviso"):
            st.warning(str(corpo_vinculo["aviso"]))

    # ── 2) POST /api/acbr/empresas: SEMPRE executado no clique do botão ──
    arquivos_acbr = None
    if conteudo_certificado is not None:
        arquivos_acbr = {
            "certificado": (
                nome_arquivo or "certificado.pfx",
                conteudo_certificado,
                "application/x-pkcs12",
            )
        }
    inicio_acbr = time.perf_counter()
    try:
        with st.spinner(
            "Enviando o certificado para a ACBr e iniciando a importação. "
            "Isso pode levar alguns minutos..."
        ):
            r = requests.post(
                EMPRESA_ACBR_URL,
                data={
                    "cnpj": digitos,
                    "senha_certificado": senha_certificado or "",
                    # Aceite obrigatório do aviso da SEFAZ (gravado pelo backend
                    # junto com os dados públicos do certificado).
                    "concordancia_sefaz": "true" if concordancia_nova else "false",
                    # Endereço fiscal digitado no expander: o backend usa este valor
                    # antes do endereço já gravado e do retorno da Receita.
                    **endereco_fiscal,
                },
                files=arquivos_acbr,
                headers=cabecalhos_api(),
                timeout=90,
            )
    except Exception as e:  # noqa: BLE001 - rede/DNS/timeout/SSL aparecem na tela
        st.error(f"Erro: {e}")
        _registrar_etapa(
            diagnostico,
            "POST",
            EMPRESA_ACBR_URL,
            elapsed_ms=int((time.perf_counter() - inicio_acbr) * 1000),
            erro=e,
        )
        _renderizar_diagnostico_cadastro(diagnostico)
        return

    tempo_acbr = int((time.perf_counter() - inicio_acbr) * 1000)
    corpo_resumo = _resumo_corpo_api(r, limite=1000)
    _registrar_etapa(
        diagnostico,
        "POST",
        EMPRESA_ACBR_URL,
        status_code=r.status_code,
        elapsed_ms=tempo_acbr,
        corpo=corpo_resumo,
    )
    _log_chamada_api(
        "POST",
        EMPRESA_ACBR_URL,
        status_code=r.status_code,
        elapsed_ms=tempo_acbr,
        corpo=corpo_resumo,
    )
    # Prova crua da resposta: vai para o expander de diagnóstico (fechado), nunca solta
    # na tela. O cliente vê só a mensagem amigável abaixo.
    try:
        corpo_acbr = r.json()
    except ValueError:
        corpo_acbr = {"detail": corpo_resumo or "(corpo vazio)"}
    empresa_acbr = corpo_acbr.get("empresa") if isinstance(corpo_acbr, dict) else None
    empresa_confirmada = (
        empresa_acbr
        if isinstance(empresa_acbr, dict)
        and empresa_acbr.get("id")
        and empresa_acbr.get("cnpj")
        else empresa_criada
    )
    loja_no_banco = bool(
        isinstance(empresa_confirmada, dict)
        and empresa_confirmada.get("id")
        and empresa_confirmada.get("cnpj")
    )
    acbr_confirmou = r.status_code == 200 and (
        not isinstance(corpo_acbr, dict) or corpo_acbr.get("sucesso") is not False
    )
    mensagem_acbr = corpo_acbr.get("mensagem") if isinstance(corpo_acbr, dict) else None

    if acbr_confirmou:
        # O resultado é guardado na sessão e a tela recarrega UMA vez: o único
        # `st.rerun()` desta função. Com a lista já atualizada (cache limpo), o
        # cliente vê a loja nova no topo em vez de um JSON de resposta.
        confirmada = empresa_confirmada if loja_no_banco else {}
        st.session_state[RESULTADO_CADASTRO_MINHAS_LOJAS] = {
            "id": confirmada.get("id"),
            "cnpj": confirmada.get("cnpj") or digitos,
            "nome_loja": confirmada.get("nome_loja") or "",
            "razao_social": confirmada.get("razao_social") or "",
            "mensagem": str(mensagem_acbr or mensagem or ""),
        }
        # Enviar o certificado também define a fonte da loja como certificado:
        # a versão nova faz o seletor de fonte reler o valor gravado.
        _incrementar_versao_fonte_notas()
        # Os campos são limpos no PRÓXIMO run: o Streamlit não permite limpar um
        # widget já instanciado no mesmo run.
        st.session_state[LIMPAR_FORM_MINHAS_LOJAS] = True
        st.session_state[LIMPAR_CONFIRMACAO_EXCLUSAO_CERT] = True
        # Mensagem explícita com o CNPJ e a loja: é o "vinculado com sucesso" que o
        # cliente (e o admin no suporte) precisa ver ao voltar para a lista.
        st.session_state[MENSAGEM_MINHAS_LOJAS] = _mensagem_vinculo_loja(
            confirmada.get("cnpj") or digitos,
            confirmada.get("nome_loja") or "",
            admin=(
                isinstance(corpo_vinculo, dict)
                and corpo_vinculo.get("acao_banco") == "certificado_admin"
            ),
        )
        st.session_state.pop(DADOS_CNPJ_MINHAS_LOJAS, None)
        st.session_state[ABRIR_ENDERECO_MINHAS_LOJAS] = False
        st.cache_data.clear()
        st.rerun()
    else:

        # A ACBr não confirmou: `mensagem_erro_http` mostra o status e o `detail`
        # crus, no formato "Erro: <código> - <motivo>".
        if r.status_code != 200:
            detalhe_erro = mensagem_de_erro(r)
        else:
            detalhe_erro = (
                mensagem_acbr
                or (corpo_acbr.get("aviso") if isinstance(corpo_acbr, dict) else None)
                or erro_vinculo
                or f"A ACBr respondeu HTTP {r.status_code} sem confirmar o cadastro."
            )
        detalhe_erro = str(detalhe_erro).strip()
        st.error(mensagem_erro_http(r))
        pede_endereco = _erro_acbr_pede_endereco(detalhe_erro)
        if loja_no_banco:
            # A loja já está no banco; só a ACBr recusou o certificado. Mostrar
            # apenas o erro dava a impressão de que nada foi cadastrado.
            st.warning(
                "A loja já está na sua conta, mas o certificado não foi aceito "
                f"pela ACBr: {detalhe_erro}"
            )
            st.session_state[MENSAGEM_MINHAS_LOJAS] = _mensagem_vinculo_loja(
                (empresa_confirmada or {}).get("cnpj") or digitos,
                (empresa_confirmada or {}).get("nome_loja") or "",
                admin=(
                    isinstance(corpo_vinculo, dict)
                    and corpo_vinculo.get("acao_banco") == "certificado_admin"
                ),
            )
            if not pede_endereco:
                # Endereço incompleto não pode varrer o formulário: o cliente
                # precisa corrigir o endereço e salvar de novo.
                st.session_state[LIMPAR_FORM_MINHAS_LOJAS] = True
                st.session_state[LIMPAR_CONFIRMACAO_EXCLUSAO_CERT] = True
            st.cache_data.clear()
        elif erro_vinculo and str(erro_vinculo).strip() not in detalhe_erro:
            st.caption(f"Vínculo: {erro_vinculo}")
        if pede_endereco:
            # Erro acionável: o expander do endereço nasce aberto no próximo run e
            # o botão abaixo força esse run sem perder o que já foi digitado.
            st.session_state[ABRIR_ENDERECO_MINHAS_LOJAS] = True
            st.warning(
                "A ACBr recusou o cadastro porque o **endereço fiscal** está "
                "incompleto. Confira os campos em **📍 Endereço fiscal** e salve "
                "de novo."
            )
            if st.button(
                "📍 Abrir endereço fiscal agora",
                key="btn_abrir_endereco_fiscal",
                type="primary",
            ):
                st.rerun()

    _renderizar_diagnostico_cadastro(diagnostico)


TIPOS_RELATORIO_UI = {
    "Diário": "diario",
    "Semanal (semana anterior)": "semanal",
    "Mensal (mês fechado)": "mensal",
}
# Mesmo critério do backend (backend/inflacao.py): abaixo disso o índice é "amostra
# pequena" e a tela avisa que a cobertura da cesta é baixa.
COBERTURA_MINIMA_UI = 60.0


def _tipo_relatorio_escolhido() -> str:
    """Tipo do selectbox da prévia (Diário/Semanal/Mensal) -> 'diario'/'semanal'/'mensal'."""
    rotulo = st.session_state.get(CHAVE_TIPO_RELATORIO_ALERTA)
    if rotulo not in TIPOS_RELATORIO_UI:
        rotulo = next(iter(TIPOS_RELATORIO_UI))
    return TIPOS_RELATORIO_UI[rotulo]


def _selos_relatorio(linha) -> str:
    """Selos da linha na tela (mesmos do e-mail)."""
    selos = []
    if linha.get("conferir"):
        selos.append("⚠️ conferir")
    if linha.get("atrasada"):
        selos.append("⚠️ atraso")
    if linha.get("recomprado"):
        selos.append("🔁 recompra")
    return " · ".join(selos)


def _dataframe_variacoes(linhas):
    """Tabela de variações pronta para o `st.dataframe` (valores já formatados).

    "Diferença" é a variação absoluta por unidade-base (o que a conta de R$ 0,01
    da regra de inclusão usa); "Fornecedor (antes → agora)" mostra de quem era a
    última compra e de quem é a atual — ou "N compras (média)" no relatório de
    período, quando o preço é a média das compras da semana/mês.
    """
    return pd.DataFrame(
        [
            {
                "Produto": linha.get("produto"),
                "Loja": linha.get("loja"),
                "Antes": formatar_moeda(linha.get("preco_anterior")),
                "Agora": formatar_moeda(linha.get("preco_atual")),
                "Diferença": (
                    formatar_moeda(linha.get("variacao_abs"))
                    if linha.get("variacao_abs") is not None
                    else "—"
                ),
                "Variação": formatar_percentual(linha.get("variacao_pct")),
                "R$/mês": formatar_moeda(linha.get("impacto_mensal")),
                "Consumo/mês": linha.get("consumo_mensal"),
                "Fornecedor (antes → agora)": _fornecedores_relatorio(linha),
                "Obs.": _selos_relatorio(linha),
            }
            for linha in linhas or []
        ]
    )


def _fornecedores_relatorio(linha):
    """Fornecedor/quantidade de compras de cada lado do relatório.

    Diário: fornecedor da última e da penúltima compra. Semanal/mensal: quando o lado
    tem mais de uma compra, "N compras (média)" — o detalhe de cada compra fica no
    tooltip do e-mail.
    """

    def _lado(lado):
        compras = linha.get(f"compras_{lado}")
        if compras and len(compras) > 1:
            return f"{len(compras)} compras (média)"
        fornecedor = str(linha.get(f"fornecedor_{lado}") or "").strip()
        if not fornecedor and lado == "atual":
            fornecedor = str(linha.get("fornecedor") or "").strip()
        return fornecedor

    anterior = _lado("anterior")
    atual = _lado("atual")
    if not anterior and not atual:
        return ""
    if anterior and atual and anterior != atual:
        return f"{anterior} → {atual}"
    return atual or anterior


def _padrao_alerta(chave, valor):
    """Deixa o valor da API como padrão do widget SEM sobrescrever o que o usuário mudou."""
    if chave not in st.session_state:
        st.session_state[chave] = valor


def renderizar_alertas(empresas):
    """Aba "🔔 Configurar Alertas": relatórios por e-mail + prévia e envio manual.

    O relatório lista toda variação de preço a partir de **R$ 0,01 por unidade-base**
    em relação à última compra do mesmo produto na mesma loja (diário) ou à média do
    período anterior (semanal/mensal; abaixo disso é arredondamento), com impacto
    mensal em R$, itens novos/recomprados, notas recebidas com atraso, a inflação da
    cesta e a série histórica. A configuração é
    por conta (`configuracoes_alerta`) e nasce pronta no cadastro: alerta ligado,
    canal e-mail e destinatário o próprio e-mail da conta.
    """
    st.title("🔔 Configurar Alertas")
    st.caption(
        "Relatório por **e-mail** com toda variação de preço (a partir de R$ 0,01/unidade) "
        "em relação à última compra da mesma loja no diário — e à **média do período "
        "anterior** no semanal/mensal —, impacto mensal em R$, inflação da sua "
        "cesta e série histórica. Bonificação, doação e descrições pendentes na Auditoria "
        "ficam de fora, como nos relatórios."
    )
    # Confirmação do salvamento: o `st.rerun()` apaga a tela, então o aviso é recuperado
    # aqui (uma vez) em vez de se perder junto com o run que salvou a configuração.
    aviso_salvo = st.session_state.pop(MENSAGEM_ALERTAS, None)
    if aviso_salvo:
        st.success(aviso_salvo)

    dados, erro = carregar_config_alertas(st.session_state.get(SESSAO_TOKEN))
    if erro:
        st.error(erro)
        return
    config = (dados or {}).get("configuracao") or {}

    st.info(
        "**Quando chega:** todo dia às 06:00 (notas novas), segunda às 06:00 (resumo da "
        "semana anterior) e dia 1º às 06:00 (mês fechado). O endereço principal já vem "
        "preenchido com o e-mail do seu cadastro; o secundário (opcional) recebe cópia."
    )

    _padrao_alerta("alerta_ativo", bool(config.get("ativo", True)))
    _padrao_alerta("alerta_diario", bool(config.get("alerta_diario", True)))
    _padrao_alerta("alerta_semanal", bool(config.get("alerta_semanal", True)))
    _padrao_alerta("alerta_mensal", bool(config.get("alerta_mensal", True)))
    _padrao_alerta("alerta_email", str(config.get("email") or ""))
    _padrao_alerta(
        "alerta_email_secundario", str(config.get("email_secundario") or "")
    )
    # Campos que saíram da tela continuam sendo reenviados com o valor gravado (o
    # cliente que já tinha 5%/30 dias não muda de comportamento ao abrir a tela).
    telefone = str(config.get("telefone") or "")
    variacao_minima = float(config.get("variacao_minima") or 0.0)
    janela_dias = int(config.get("janela_dias") or 30)
    incluir_aumentos = bool(config.get("incluir_aumentos", True))
    incluir_divergencias = bool(config.get("incluir_divergencias", True))
    lojas = []
    canal = "email"

    with st.form("form_alertas"):
        ativo = st.toggle(
            "Alertas ativos",
            key="alerta_ativo",
            help="Desligado = nenhum relatório é enviado para esta conta.",
        )
        col_diario, col_semanal, col_mensal = st.columns(3)
        diario = col_diario.toggle(
            "Relatório diário",
            key="alerta_diario",
            help="Toda nota importada, todos os dias às 06:00.",
        )
        semanal = col_semanal.toggle(
            "Resumo semanal",
            key="alerta_semanal",
            help="Segunda às 06:00 com as compras da semana anterior.",
        )
        mensal = col_mensal.toggle(
            "Fechamento mensal",
            key="alerta_mensal",
            help=(
                "Dia 1º às 06:00 com o mês anterior fechado: inflação da cesta, "
                "série histórica e memória de cálculo em Excel."
            ),
        )
        col_email, col_email_secundario = st.columns(2)
        email = col_email.text_input(
            "E-mail para receber os alertas",
            key="alerta_email",
            placeholder="compras@empresa.com.br",
            help="Endereço principal que recebe os relatórios.",
        )
        email_secundario = col_email_secundario.text_input(
            "E-mail secundário (opcional)",
            key="alerta_email_secundario",
            placeholder="financeiro@empresa.com.br",
            help="Recebe uma cópia (Cc) de cada relatório.",
        )
        salvar = st.form_submit_button(
            "💾 Salvar preferências", type="primary", width="stretch"
        )

    if salvar:
        salvo, erro_salvar = salvar_config_alertas(
            ativo,
            telefone,
            variacao_minima,
            janela_dias,
            incluir_aumentos,
            incluir_divergencias,
            lojas,
            email,
            canal,
            email_secundario,
            diario,
            semanal,
            mensal,
        )
        if erro_salvar:
            st.error(erro_salvar)
        else:
            st.session_state[MENSAGEM_ALERTAS] = "Preferências salvas com sucesso!"
            st.session_state.pop(SESSAO_CONFIRMAR_EMAIL_ALERTA, None)
            st.cache_data.clear()
            st.rerun()

    # ── Prévia e envio manual ────────────────────────────────────────────────
    st.divider()
    st.subheader("🧪 Prévia e envio")
    st.caption(
        "A prévia mostra o **mesmo conteúdo** que o e-mail do agendador enviaria agora "
        "(notas novas/semana/mês + atrasadas que ainda não foram relatadas)."
    )
    col_tipo, col_gerar, col_enviar = st.columns([2, 1, 1], vertical_alignment="bottom")
    col_tipo.selectbox(
        "Relatório",
        list(TIPOS_RELATORIO_UI),
        key=CHAVE_TIPO_RELATORIO_ALERTA,
        help="Diário = notas novas; semanal = semana anterior; mensal = mês fechado.",
    )
    resultado_previa = None
    if col_gerar.button("🧪 Gerar prévia", type="primary", width="stretch"):
        with st.spinner("Montando o relatório..."):
            resultado_previa, erro_previa = prever_relatorio_alertas(
                _tipo_relatorio_escolhido()
            )
        if erro_previa:
            st.error(erro_previa)
        else:
            st.session_state[SESSAO_PREVIA_RELATORIO_ALERTA] = resultado_previa or {}
            st.session_state.pop(SESSAO_CONFIRMAR_EMAIL_ALERTA, None)

    preview = (
        resultado_previa
        if isinstance(resultado_previa, dict) and resultado_previa.get("resumo")
        else st.session_state.get(SESSAO_PREVIA_RELATORIO_ALERTA)
    )
    if not isinstance(preview, dict):
        preview = {}
    resumo = preview.get("resumo") or {}
    pode_enviar = bool(resumo.get("notas_lote"))
    if col_enviar.button(
        "📧 Enviar agora",
        width="stretch",
        disabled=not pode_enviar,
        help=(
            "Envia o relatório da prévia por e-mail para o endereço salvo na conta."
            if pode_enviar
            else "Gere uma prévia com notas para liberar o envio."
        ),
    ):
        st.session_state[SESSAO_CONFIRMAR_EMAIL_ALERTA] = True

    if st.session_state.get(SESSAO_CONFIRMAR_EMAIL_ALERTA) and pode_enviar:
        destino_email = str(config.get("email") or "").strip()
        copia_email = str(config.get("email_secundario") or "").strip()
        st.warning(
            f"Confirmar o envio de **{preview.get('titulo') or 'relatório'}** "
            f"({resumo.get('notas_lote') or 0} nota(s), "
            f"{resumo.get('variacoes') or 0} variação(ões)) para "
            f"**{destino_email or 'e-mail não configurado'}**"
            + (f" (Cc: {copia_email})" if copia_email else "")
            + "?"
        )
        col_confirmar, col_cancelar = st.columns(2)
        if col_confirmar.button(
            "✅ Confirmar envio",
            key="btn_confirmar_email_alerta",
            type="primary",
            width="stretch",
        ):
            st.session_state.pop(SESSAO_CONFIRMAR_EMAIL_ALERTA, None)
            with st.spinner("Enviando o relatório..."):
                envio, erro_envio = enviar_relatorio_alertas(
                    preview.get("tipo") or _tipo_relatorio_escolhido()
                )
            if erro_envio:
                st.error(erro_envio)
            elif envio and envio.get("enviado"):
                st.success(
                    f"Relatório enviado para **{envio.get('destinatario')}**"
                    + (
                        f" (Cc: {envio.get('copia')})."
                        if envio.get("copia")
                        else "."
                    )
                )
                st.session_state[SESSAO_PREVIA_RELATORIO_ALERTA] = (
                    envio.get("relatorio") or {}
                )
                st.cache_data.clear()
            else:
                st.warning(
                    (envio or {}).get("motivo") or "Nada foi enviado pelo servidor."
                )
        if col_cancelar.button(
            "Cancelar", key="btn_cancelar_email_alerta", width="stretch"
        ):
            st.session_state.pop(SESSAO_CONFIRMAR_EMAIL_ALERTA, None)
            st.rerun()

    if preview.get("resumo"):
        _renderizar_previa_alerta(preview)


def _renderizar_previa_alerta(resultado):
    """Prévia do relatório: resumo, tabelas, inflação e o texto exato do e-mail.

    Recebe o dict guardado em `SESSAO_PREVIA_RELATORIO_ALERTA` (ou devolvido pelo
    envio). Nada é enviado aqui: a tela mostra o que iria no e-mail do agendador.
    """
    resultado = resultado or {}
    resumo = resultado.get("resumo") or {}
    st.divider()
    col_titulo, col_limpar = st.columns([4, 1], vertical_alignment="bottom")
    col_titulo.subheader(f"📄 {resultado.get('titulo') or 'Relatório'}")
    if col_limpar.button(
        "🧹 Limpar prévia", key="btn_limpar_previa_alerta", width="stretch"
    ):
        st.session_state.pop(SESSAO_PREVIA_RELATORIO_ALERTA, None)
        st.session_state.pop(SESSAO_CONFIRMAR_EMAIL_ALERTA, None)
        st.rerun()
    st.caption(
        f"Período: **{resultado.get('inicio') or '—'}** a "
        f"**{resultado.get('fim') or '—'}** • {resumo.get('notas_lote') or 0} nota(s) "
        f"• {resumo.get('itens_lote') or 0} item(ns)"
    )
    if resultado.get("aviso"):
        st.warning(str(resultado["aviso"]))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Aumentos", formatar_numero(resumo.get("aumentos") or 0))
    m2.metric("Quedas", formatar_numero(resumo.get("quedas") or 0))
    m3.metric(
        "Impacto/mês",
        formatar_moeda(resumo.get("impacto_liquido") or 0.0),
        help="Diferença de preço x consumo médio mensal dos últimos 90 dias.",
    )
    m4.metric(
        "Produtos novos",
        formatar_numero(resumo.get("novos") or 0),
        help="Primeira compra do produto nesta loja.",
    )
    m5.metric(
        "Para conferir",
        formatar_numero(resumo.get("conferir") or 0),
        help="Variação de 50% ou mais: confira unidade/cadastro antes de usar o preço.",
    )
    if resumo.get("recomprados"):
        st.caption(
            f"🔁 {resumo['recomprados']} produto(s) voltaram a ser comprados depois de "
            f"mais de {30} dias sem compra."
        )
    if resumo.get("atrasadas"):
        st.warning(
            f"⚠️ {resumo['atrasadas']} nota(s) deste relatório foram recebidas com "
            "atraso (importadas depois da data de emissão)."
        )

    # ── Inflação da cesta ────────────────────────────────────────────────────
    inflacao = resultado.get("inflacao") or {}
    fechado = inflacao.get("fechado") or {}
    previa30 = inflacao.get("previa_30_dias") or {}
    col_fechado, col_30 = st.columns(2)
    if fechado.get("variacao_pct") is not None:
        col_fechado.metric(
            f"Inflação {fechado.get('rotulo') or 'do mês fechado'}",
            formatar_percentual(fechado.get("variacao_pct")),
            help=(
                f"{fechado.get('itens_comparados')} de "
                f"{fechado.get('itens_cesta')} produtos da cesta anterior "
                "encontraram o mesmo produto no mês — cada um entra com o peso do "
                "gasto. A conta aberta está no Excel abaixo."
            ),
        )
    else:
        col_fechado.metric("Inflação do mês fechado", "—")
    if previa30.get("disponivel"):
        col_30.metric(
            "Últimos 30 dias (prévia)",
            formatar_percentual(previa30.get("variacao_pct")),
            help=(
                f"{previa30.get('itens_comparados')} de "
                f"{previa30.get('itens_cesta')} produtos comparados em janelas "
                "móveis de 30 dias (tendência, não substitui o mês fechado)."
            ),
        )
    else:
        col_30.metric(
            "Últimos 30 dias (prévia)", "—", help=previa30.get("motivo") or ""
        )
    if fechado.get("amostra_pequena"):
        st.caption(
            "⚠️ **Amostra pequena** no mês fechado: menos de "
            f"{int(COBERTURA_MINIMA_UI)}% do gasto da cesta encontrou o mesmo produto. "
            "O número é só uma tendência — confira item a item no Excel."
        )
    serie = inflacao.get("serie") or []
    if serie:
        # Nome pedido pelo cliente: "série histórica". Índice (base 100) e
        # cobertura saíram da tabela: o cliente quer o mês e a variação, e a conta
        # completa fica na memória de cálculo em Excel.
        st.markdown("**Série histórica**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Mês": item.get("rotulo"),
                        "Variação": (
                            formatar_percentual(item.get("variacao_pct"))
                            if item.get("variacao_pct") is not None
                            else "—"
                        ),
                        "Obs.": (
                            "amostra pequena"
                            if item.get("amostra_pequena")
                            else (
                                ""
                                if item.get("variacao_pct") is not None
                                else "sem comparação"
                            )
                        ),
                    }
                    for item in serie
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    contribuicoes = fechado.get("contribuicoes") or []
    if contribuicoes:
        st.markdown("**Maiores responsáveis no mês fechado**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Produto": linha.get("produto"),
                        "Variação": formatar_percentual(linha.get("variacao_pct")),
                        "Peso na cesta": formatar_percentual(linha.get("peso_pct")),
                        "Contribuição": formatar_percentual(
                            linha.get("contribuicao_pct")
                        ),
                    }
                    for linha in contribuicoes
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    variacoes = resultado.get("variacoes") or []
    st.markdown(f"#### 💰 Variações de preço ({len(variacoes)})")
    if variacoes:
        st.dataframe(
            _dataframe_variacoes(variacoes), hide_index=True, width="stretch"
        )
    else:
        st.caption("Nenhuma variação de preço nas compras deste período.")

    lojas = resultado.get("lojas") or []
    if lojas:
        st.markdown("#### 🏬 Resumo por loja")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Loja": loja.get("loja"),
                        "Aumentos": loja.get("aumentos"),
                        "Quedas": loja.get("quedas"),
                        "Impacto/mês": formatar_moeda(loja.get("impacto")),
                        "Itens com variação": loja.get("itens"),
                    }
                    for loja in lojas
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    novos = resultado.get("novos") or []
    if novos:
        st.markdown(f"#### 🆕 Produtos novos ({len(novos)})")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Produto": linha.get("produto"),
                        "Loja": linha.get("loja"),
                        "Preço": formatar_moeda(linha.get("preco_atual")),
                        "Consumo/mês": linha.get("consumo_mensal"),
                        "Obs.": _selos_relatorio(linha),
                    }
                    for linha in novos
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    recomprados = resultado.get("recomprados") or []
    if recomprados:
        with st.expander(f"🔁 Recomprados ({len(recomprados)})", expanded=False):
            st.caption(
                "Produtos que voltaram a ser comprados depois de mais de 30 dias sem "
                "compra (no diário a variação continua sendo contra a última compra; "
                "no semanal/mensal, contra a média do período anterior)."
            )
            st.dataframe(
                _dataframe_variacoes(recomprados), hide_index=True, width="stretch"
            )

    texto = resultado.get("texto_email") or ""
    if texto:
        with st.expander("✉️ Ver o texto do e-mail", expanded=False):
            st.code(texto, language=None)

    _renderizar_memoria_calculo_alerta()


def _renderizar_memoria_calculo_alerta():
    """Memória de cálculo em Excel: a conta aberta do mês fechado do relatório.

    O arquivo é gerado no backend com a MESMA base do e-mail (fórmulas reais do
    Excel: preço, quantidade, peso, variação e a soma ponderada). O botão de
    preparar é separado do download porque a conta percorre o histórico do mês —
    não deve rodar a cada rerun da tela.
    """
    st.divider()
    st.markdown("**🧾 Memória de cálculo (Excel)**")
    st.caption(
        "Abre a conta do mês fechado **produto a produto**, com as fórmulas do Excel "
        "(preço, quantidade, peso, variação e a soma ponderada), a série histórica e "
        "a metodologia. É o arquivo para conferir o número do e-mail célula por célula."
    )
    if st.button(
        "🔄 Preparar memória de cálculo",
        key="btn_preparar_memoria_calculo_alerta",
        width="stretch",
        help=(
            "Monta o .xlsx no backend com a mesma base da prévia/envio. Nada é "
            "gravado em disco: o arquivo fica em memória para o download."
        ),
    ):
        with st.spinner("Montando a memória de cálculo..."):
            conteudo, nome_arquivo, mes_referencia, erro = (
                carregar_memoria_calculo_alertas("mensal")
            )
        if erro:
            st.session_state.pop(SESSAO_MEMORIA_CALCULO, None)
            st.error(erro)
        else:
            st.session_state[SESSAO_MEMORIA_CALCULO] = {
                "conteudo": conteudo,
                "nome": nome_arquivo,
                "mes": mes_referencia,
                "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            }
            st.success("Memória de cálculo pronta. Use o botão abaixo para baixar.")

    memoria = st.session_state.get(SESSAO_MEMORIA_CALCULO)
    if memoria:
        st.download_button(
            "📥 Baixar memória de cálculo (.xlsx)",
            data=memoria.get("conteudo") or b"",
            file_name=memoria.get("nome") or "memoria_calculo_inflacao.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            key="btn_baixar_memoria_calculo_alerta",
            width="stretch",
        )
        st.caption(
            f"Mês de referência: {memoria.get('mes') or '—'} | gerado em "
            f"{memoria.get('gerado_em') or '—'}. O download não consulta o backend "
            "de novo: o arquivo já está pronto na memória."
        )
    else:
        st.info(
            "Clique em **Preparar memória de cálculo** para gerar o Excel do mês "
            "fechado."
        )


def _aviso_conta_sem_lojas():
    """Relatórios de uma conta ainda sem CNPJ vinculado (nada a exibir)."""
    st.title("📊 Relatórios de Preços")
    st.warning(
        "Sua conta ainda não tem nenhuma loja vinculada. Cadastre um CNPJ para começar a ver "
        "os relatórios das suas notas."
    )
    if st.button("🏬 Ir para Minhas Lojas", key="btn_ir_minhas_lojas", type="primary"):
        ir_para(PAGINA_MINHAS_LOJAS)


def _aviso_lojas_sem_notas(empresas):
    """Lojas vinculadas, mas sem nenhuma compra importada no período."""
    st.title("📊 Relatórios de Preços")
    nomes = [empresa.get("nome_loja") for empresa in empresas or [] if empresa.get("nome_loja")]
    lista = f" ({', '.join(nomes)})" if nomes else ""
    st.info(
        f"As suas lojas{lista} ainda não têm compras importadas. Importe os XMLs "
        "(python import_xmls.py) e recarregue os relatórios."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Sessão do usuário, menu lateral e roteamento entre as telas
# ──────────────────────────────────────────────────────────────────────────────
aplicar_navegacao_pendente()
exigir_login()
# Depois do login e antes do widget do menu: a URL manda (sobrevive à perda do
# `st.session_state`, que era o que jogava a tela para "Relatórios" no cadastro).
restaurar_pagina_da_url()

usuario = usuario_logado() or {}
with st.sidebar:
    st.markdown("### 🧾 Inteligência de Custos")
    st.markdown(f"**👤 {usuario.get('nome') or 'Usuário'}**")
    st.caption(usuario.get("email") or "")
    st.divider()

    empresas, erro_empresas, sessao_invalida = carregar_empresas_vinculadas(
        st.session_state.get(SESSAO_TOKEN)
    )
    if sessao_invalida:
        sair_do_sistema(erro_empresas or "Sua sessão expirou. Entre novamente.")
        st.rerun()
    if erro_empresas:
        st.error(erro_empresas)
    if not empresas:
        st.caption("Nenhuma loja vinculada ainda — cadastre um CNPJ em **🏬 Minhas Lojas**.")

    st.divider()
    paginas = paginas_disponiveis()
    pagina_atual = st.session_state.get(SESSAO_PAGINA) or PAGINA_RELATORIOS
    if pagina_atual not in paginas:
        pagina_atual = PAGINA_RELATORIOS
    pagina = st.radio("Navegação", paginas, index=paginas.index(pagina_atual), key=CHAVE_MENU)
    # Log ANTES de gravar `SESSAO_PAGINA`: mostra o valor com que a sessão chegou
    # neste run (é ele que denuncia a perda de estado / login silencioso).
    _diagnosticar_menu(paginas, pagina)
    st.session_state[SESSAO_PAGINA] = pagina
    st.caption(LEGENDAS_PAGINAS.get(pagina, ""))
    st.divider()
    if st.button("🚪 Sair", key="btn_sair", use_container_width=True):
        sair_do_sistema()
        st.rerun()

if pagina == PAGINA_MINHAS_LOJAS:
    renderizar_minhas_lojas(empresas, erro_empresas)
    st.stop()

# Alertas não dependem dos relatórios carregados: a aba monta a mensagem direto na API.
if pagina == PAGINA_ALERTAS:
    renderizar_alertas(empresas)
    st.stop()

df_bruto = carregar_dados()
if df_bruto.empty:
    st.warning("Nenhum dado encontrado. Verifique se o backend está rodando.")
    st.stop()

df_full = preparar_dataframe(df_bruto)
with st.sidebar:
    st.caption(f"{formatar_numero(len(df_full))} itens carregados")

if pagina == PAGINA_AUDITORIA:
    # Defesa em profundidade: o menu já esconde a Auditoria de quem não é admin, a página
    # exige o 2FA e o backend responde 403/401 nas rotas da área (inclusive sem 2FA).
    if not usuario_e_admin():
        st.error(
            "A Auditoria é restrita aos administradores do sistema. "
            "Fale com o responsável pela conta para liberar o acesso."
        )
        st.stop()
    renderizar_auditoria(df_full)
elif not empresas:
    _aviso_conta_sem_lojas()
else:
    df_empresa = itens_da_empresa(df_full, empresas)
    if df_empresa.empty:
        _aviso_lojas_sem_notas(empresas)
    else:
        renderizar_relatorios(df_empresa, empresas)
