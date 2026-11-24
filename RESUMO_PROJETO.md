# Resumo do Projeto — Inteligência de Custos (NF-e)

**Última atualização:** 13/11/2026
**Estado geral:** aplicação funcional em ambiente local (API FastAPI + front Streamlit + SQLite), com importação de NF-e, normalização de produtos/embalagens, inteligência de marcas, auditoria administrativa com 2FA, relatórios gerenciais, motor de alertas (simulação + envio real por e-mail) e integração ACBr de download de XMLs.
**Pendências principais:** envio real de alertas por WhatsApp, cofre do certificado digital, importação automática dos XMLs baixados da ACBr, correção de um teste dependente do estado do banco e endurecimento de autenticação/multitenancy nas rotas de relatórios.

> Este documento substitui a ausência de README e registra o estado real do código em 13/11/2026. Não contém segredos: as chaves ficam apenas no `.env` local (ignorado pelo `.gitignore`).

---

## 1. Visão geral

Sistema de inteligência de custos para um grupo de restaurantes/lojas. O fluxo principal é:

1. **Importar XMLs de NF-e** (arquivo local ou download pela ACBr API).
2. **Normalizar a identidade do produto**: EAN/descrição + marca + volume + embalagem, preservando os valores crus do XML.
3. **Aprender o de-para fornecedor + cProd** para reconhecer o mesmo produto nas notas seguintes.
4. **Inferir a marca** por heurística de NCM, catálogo mestre e, quando necessário, DeepSeek (com cache).
5. **Auditar divergências** de descrição/agrupamento e variações bruscas de preço (admin + 2FA).
6. **Calcular relatórios** de evolução de preços, comparativo entre lojas e impacto financeiro.
7. **Gerar/simular alertas** de aumento, queda e divergência (WhatsApp/e-mail).
8. **Exportar** raio-x da base e catálogo completo em Excel.

Contas são multiusuário/multilojas: cada usuário é dono de um conjunto de empresas (CNPJs). A Auditoria é exclusiva de administradores (`is_admin`) com TOTP ativo.

---

## 2. Stack e ambiente

Dependências principais (`requirements.txt`):

- **API:** FastAPI 0.162.2, Uvicorn 0.54.0, SQLAlchemy 2.1.8, Pydantic 2.14.3.
- **Banco:** PostgreSQL em nuvem via `DATABASE_URL` (driver `psycopg2-binary`, fixado como `postgresql+psycopg2://` em `criar_engine`) ou SQLite local como fallback, com migrações aditivas em `backend/database.py`.
- **Front:** Streamlit 1.67.0, pandas 3.1.1, openpyxl 3.1.5.
- **Autenticação:** passlib 1.7.4 + bcrypt 4.0.1, PyJWT 2.15.1, pyotp 2.9.0, qrcode 8.2.
- **Integrações:** requests 2.34.2, python-dotenv 1.2.4, python-multipart 0.0.33, cryptography 50.0.2.
- **IA:** DeepSeek via HTTP (`DEEPSEEK_API_KEY`), sem SDK proprietário.
- **ACBr:** API REST com OAuth2 `client_credentials`.

### Banco de dados

- Caminho efetivo local (fallback): **`C:\sistema_nfe\sistema_nfe_pro.db`** (pasta PAI do projeto), definido em `backend/database.py`.
- Com `DATABASE_URL` definido (`.env` ou ambiente), a API usa o PostgreSQL da nuvem; `postgres://` é normalizado e o driver `psycopg2` é fixado automaticamente (`criar_engine`), porque o SQLAlchemy 2.1 usaria `psycopg` v3 por padrão.
- **Migração dos dados:** `python migrar_para_postgres.py --simular` compara origem x destino; sem `--simular` copia tudo (IDs, datas, booleanos, sequências) para o PostgreSQL vazio. `--limpar` refaz a carga do zero.
- Tamanho atual: ~2,5 MB.
- Existe um `sistema_nfe_pro.db` de **0 bytes** na raiz do projeto; é um resíduo e não é o banco usado. Pode ser removido.
- `init_db()` cria tabelas e aplica colunas novas de forma idempotente (não há Alembic).

### Variáveis de ambiente (`.env` na raiz)

Presentes hoje: `DEEPSEEK_API_KEY`, `ACBR_CLIENT_ID`, `ACBR_CLIENT_SECRET`, `ACBR_AMBIENTE`, `ACBR_AUTH_URL`, `ACBR_API_URL`, `ACBR_SCOPES`, `DIAS_PRIMEIRO_DOWNLOAD`.
Opcionais previstas no código: `ACBR_TIMEOUT`, `DEEPSEEK_MODEL`, `DEEPSEEK_API_URL`, `DEEPSEEK_TIMEOUT`, `NFE_SECRET_KEY`, `NFE_TOKEN_HORAS`, `NFE_2FA_HORAS`.
Alertas/consultas: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_NAME`, `SMTP_TIMEOUT`, `NFE_SMTP_DESATIVADO` (teste), `NFE_ALERTAS_AGENDADOR` (`0` desliga a thread), `NFE_ALERTAS_HORA` (0-23, padrão 6), `NFE_ESPERA_ENTRE_PROVEDORES` (pausa ao trocar de provedor do CNPJ, padrão 1 s), `NFE_CACHE_CADASTRAL_HORAS` (TTL do cache de CNPJ/CEP, padrão 720 h).
Omie: `OMIE_APP_KEY`/`OMIE_APP_SECRET` (credencial de fallback quando a loja não tem a sua; `testar_omie_sync.py --gravar-credenciais` grava a da loja cifrada), `OMIE_ENC_KEY` (chave do Fernet; sem ela o módulo usa `NFE_SECRET_KEY` ou o `.secret_key`), `OMIE_TIMEOUT` (segundos por chamada, padrão 60). Agendamento Omie: `NFE_OMIE_AGENDADOR` (`0` desliga a thread diária), `NFE_OMIE_HORA` (0-23, padrão 3) e `NFE_OMIE_PAUSA_SEGUNDOS` (espera entre lojas, padrão 2 s).

`.gitignore` protege `.env`, `.secret_key`, `__pycache__/`, `*.py[cod]` e `*.log`. **Ainda não existe repositório Git inicializado** (`git status` retorna “SEM_GIT”).

---

## 3. Estrutura de arquivos

```text
sistema_nfe_pro/
├── backend/
│   ├── main.py                  # API FastAPI: rotas, regras de relatório, auditoria, IA, ACBr, alertas
│   ├── models.py                # ORM: Usuario, Empresa, Fornecedor, NotaFiscal, ItemNota, ItemAuditado,
│   │                            #      RegraAgrupamento, SugestaoCorrecaoIA, ConfiguracaoAlerta,
│   │                            #      ConfiguracaoSistema, MarcaIA, MarcaMaster
│   ├── database.py              # engine SQLite, SessionLocal, init_db e migrações aditivas
│   ├── auth.py                  # hash de senha, JWT, 2FA TOTP, dependências de admin
│   ├── certificados.py          # validação do .pfx/.p12, titular/CNPJ/validade
│   ├── cripto_credenciais.py    # Fernet/PBKDF2 dos segredos de integração gravados no banco
│   ├── acbr_client.py           # cliente OAuth2 ACBr: empresa, certificado, distribuição e XMLs
│   ├── omie_client.py           # cliente HTTP Omie: ListarRecebimentos, ListarDocumentos (XML)
│   ├── omie_sync.py             # recebimentos Omie → fornecedor/nota/itens (dedupe por chave)
│   ├── marcas.py                # heurística local de marca por variação de NCM
│   ├── ia_consenso.py           # DeepSeek: marcas, ambiguidade, sugestões de correção, cache
│   ├── aprendizado_produtos.py  # embalagens, de-para fornecedor+cProd, sincronização
│   ├── auditoria_relatorio.py   # espelho backend das regras de similaridade da Auditoria
│   ├── alertas.py               # motor de mensagens de alerta (compatibilidade)
│   ├── relatorios_alerta.py     # relatório diário/semanal/mensal: variações, R$/mês, novos/recomprados, HTML
│   ├── inflacao.py              # índice de inflação da cesta (Laspeyres encadeado + prévia de 30 dias)
│   ├── consulta_cadastral.py    # CNPJ/CEP com troca de provedor (BrasilAPI/minhareceita/cnpj.ws, ViaCEP)
│   ├── agendador.py             # thread do envio 06:00 (diário), segunda (semanal) e dia 1º (mensal)
│   ├── email_service.py         # envio SMTP dos alertas (texto + HTML, credenciais só no .env)
│   ├── processar_lote.py        # LEGADO: apaga o banco e processa XMLs direto (NÃO USAR)
│   ├── xml_parser.py            # LEGADO: parser SQLite direto usado pelo processar_lote
│   └── testar_parser.py         # LEGADO: teste manual do parser antigo
├── frontend/
│   └── app.py                   # Streamlit: login, relatórios, auditoria, lojas e alertas
├── tests/
│   ├── test_integridade_relatorios.py  # suíte principal (74 testes registrados)
│   ├── test_acbr_rotas.py              # ACBr fake end-to-end
│   ├── test_acbr_endereco.py           # endereço da ACBr + troca de provedor do CNPJ
│   ├── test_acbr_auto_import.py        # certificado → download → importação (unitário)
│   ├── test_multitenancy_lojas.py      # /api/lojas e consultar-cnpj por conta
│   ├── test_alertas_relatorio.py       # relatório por e-mail + decisão do agendador (11 testes)
│   ├── test_inflacao.py                # Laspeyres encadeado + prévia de 30 dias (9 testes)
│   ├── test_email_service.py           # SMTP falso (handshake, Cc, multipart/HTML)
│   ├── test_verificacao_email.py       # cadastro/confirmação de e-mail
│   ├── test_marcas.py                  # script de validação da inteligência de marcas
│   ├── test_marcas_master.py           # 7 testes do catálogo mestre/cache
│   ├── test_omie_sync.py               # sincronização Omie offline (cliente falso, SQLite temporário)
│   ├── test_omie_rotas.py              # rotas /api/omie/* + agendador diário (12 testes, mock)
│   └── _verificar_auditoria_tmp.py     # inspeção temporária da tela de Auditoria
├── xmls/                        # XMLs locais de NF-e (grande volume, dados de operação)
├── import_xmls.py               # importador principal dos XMLs
├── backfill_relatorios.py       # backfill de CFOP e vínculo nota → loja (CNPJ destinatário)
├── backfill_aprendizado.py      # reprocessa embalagens e memória de-para na base existente
├── backfill_marcas.py           # preenche itens_nota.marca na base existente
├── reimportar_zerado.py         # apaga notas/itens e reimporta tudo (preserva auditoria)
├── vincular_notas.py            # atalho para o vínculo nota → loja
├── atualizar_empresas.py        # corrige cadastros das lojas do grupo
├── criar_admin.py               # promove admin, remove admin ou reseta 2FA
├── popular_marcas_master.py     # popula o catálogo mestre de marcas
├── importar_marcas_novas.py     # importa marcas novas de marcas_novas.txt
├── marcas_novas.txt             # lista de marcas para importação
├── validar_marcas_base.py       # revalida marcas gravadas (cache/IA), com backup SQLite
├── marcar_certificado_manual.py # grava dados públicos de certificado manualmente
├── atualizar_nsu_manual.py      # lê os XMLs antigos e grava o maior NSU por loja
├── limpar_certificados_teste.py # limpa certificados de teste (script operacional)
├── testar_acbr.py               # teste manual de autenticação/CEP na ACBr
├── testar_omie.py               # teste manual do cliente Omie (listagem/XML, sem banco)
├── testar_omie_sync.py          # sincronização Omie real (--simular/--gravar-credenciais)
├── diagnostico_omie_cetapa.py   # diagnóstico: quantos recebimentos por etapa/período
├── diagnostico_omie_xml.py      # diagnóstico: compara recebimento do Omie com o XML local
├── ver_empresas.py              # lista empresas do banco
├── requirements.txt
├── .env                         # segredos locais (IGNORADO pelo Git): OMIE_APP_KEY/SECRET, OMIE_ENC_KEY
├── .secret_key                  # segredo JWT gerado no primeiro start (IGNORADO)
└── RESUMO_PROJETO.md            # este documento
```

### Tabelas principais

- `usuarios`: conta, hash de senha, `is_admin`, segredo/estado do 2FA.
- `empresas`: CNPJ, nomes, `usuario_id` (dono), dados públicos do certificado, fonte das notas (`fonte_notas` = `omie`/`certificado`), concordância SEFAZ (`cert_concordancia_em`/`cert_concordancia_por`), cursor `ultimo_nsu_acbr` e as credenciais Omie **cifradas** (`omie_app_key`/`omie_app_secret`) + `omie_sync_em` (última sincronização).
- `fornecedores`, `notas_fiscais`, `itens_nota`: NF-e, itens crus e campos derivados (`cfop`, `marca`, `c_prod`, `unidade_compra`, `qtd_por_embalagem`, unidades/base).
- `mapeamento_fornecedor_produtos`: memória de-para por CNPJ do fornecedor + `cProd`, com `origem` automática/manual.
- `itens_auditados`: chave aprovada ou pendente (`auditado` + `motivo` manual/aberrante).
- `regras_agrupamento`: regras `juntar` (grupo → grupo) e `mover` (descrição → grupo).
- `sugestoes_correcao_ia`: sugestões pendentes/aprovadas/ignoradas.
- `configuracoes_alerta`: preferências por conta (telefone, e-mail, canal, janela, lojas), tipos do relatório (`alerta_diario/semanal/mensal`), cursores de idempotência (`ultima_nota_*_id`) e marcadores de envio (`ultimo_envio_*`).
- `alertas_notas`: notas já cobertas por relatório (conta + nota + tipo, única) — auditoria do que foi enviado.
- `configuracoes_sistema`: chave/valor global (token do WhatsApp, mascarado na API).
- `marcas_ia`: cache de marca por descrição normalizada (`SEM MARCA` é sentinela).
- `marcas_master`: catálogo mestre de marcas (categoria + nome único).

---

## 4. O que já está implementado

### 4.1 Importação de NF-e e normalização de produtos

- `import_xmls.py` lê os XMLs de `xmls/`, grava fornecedor/nota/itens, deduplica pela `chave_acesso` e vincula a nota ao **CNPJ destinatário** (multilojas).
- Cada item preserva os valores **crus** do XML (`quantidade`, `valor_unitario`, `valor_total`) e grava também `c_prod`, `uCom`, CFOP, NCM e EAN.
- **Embalagem é identidade, não conversão**: “ORIGINAL 600ML CX C/24” e “ORIGINAL 600ML UN” são produtos diferentes. `normalizar_embalagem`/`embalagem_para_chave` geram tokens canônicos (ex.: `CX24`, `FD10X1KG`, `UN`) apenas para compor a chave do produto.
- `normalizar_quantidades` mantém quantidade e preço crus (a regra antiga de converter caixa em unidade foi removida de propósito).
- **Memória de aprendizado**: `mapeamento_fornecedor_produtos` guarda EAN/descrição/NCM/embalagem canônicos por (CNPJ do fornecedor + `cProd`). XMLs seguintes com “SEM GTIN” ou descrição abreviada reaproveitam o aprendizado.
- Correção manual na Auditoria tem prioridade e é reaplicada a todo o histórico (`aplicar_mapeamento_historico`); a importação não sobrescreve `origem="manual"`.
- **CFOP 5910/6910**, valor unitário R$ 0,00 e textos de bonificação/doação/brinde são excluídos dos cálculos de preço.
- Backfills idempotentes: `backfill_relatorios.py` (CFOP + loja), `backfill_aprendizado.py` (embalagens + de-para), `backfill_marcas.py` (marca).
- `init_db()` faz migrações aditivas para bases antigas (colunas de usuário, certificado, CFOP, marca, cProd, embalagem, canal/e-mail de alerta).

### 4.2 Inteligência de marcas

- `backend/marcas.py` extrai a marca pela **variação das descrições do mesmo NCM**: palavras que se repetem nomeiam o produto; a marca é o primeiro token restante.
- A marca só é gravada quando a descrição tem uma “irmã” (outra descrição do mesmo NCM com o produto igual após remover a marca, similaridade > 80%). Isso evita transformar “ALFACE CRESPA” x “ALFACE AMERICANA” em marca.
- Limitações conhecidas e documentadas: NCM com uma única descrição não gera marca; a heurística não conhece catálogo, só o contraste entre descrições.
- **Catálogo mestre** (`marcas_master`, 498 marcas): consultado por token antes de gastar IA. Inclui categorias e é populado por `popular_marcas_master.py` / `importar_marcas_novas.py`.
- **Cache da IA** (`marcas_ia`, 742 descrições): a chave é a descrição normalizada (maiúsculas, sem acento, sem número de controle). Respostas incluem o sentinela `SEM MARCA`.
- Cascata de extração em `ia_consenso.obter_marca_inteligente`: cache exato → catálogo mestre/aliases/abreviações → tokenização local → DeepSeek apenas para descrição inédita.
- Lista conservadora `MARCAS_INVALIDAS` bloqueia termos genéricos/cortes/embalagens que a heurística ou a IA possam devolver (OSSO, LIXO, CARNE, CAIXA etc.).
- `recalcular_marcas(db, usar_ia=..., apenas_cache=...)` atualiza `itens_nota.marca`; devolve contadores de consultas, cache hits, sem marca e erros. O importador usa `apenas_cache`; o botão do admin processa lotes e depois aplica só o cache.
- A marca entra na **chave inteligente Produto + Marca** (Arrico e El Chef não compartilham média) e no **tooltip de marcas substitutas** do mesmo NCM. Ela **não** entra como valor na média: cada grupo usa o preço exato das suas compras.
- Scripts de manutenção: `validar_marcas_base.py` (revalida a coluna com cache/IA e cria backup SQLite), `backfill_marcas.py` (preenche base antiga).
- Correções recentes no prompt da IA: marca é critério de separação de produto, exemplo “Queijo Minas Camanducaia x Ipanema” e cláusula explícita para **nunca** tratar marca como palavra genérica.

### 4.3 Auditoria administrativa

- Acesso **fail-closed**: todas as rotas de auditoria usam `auth.get_admin_user_2fa`. Sem sessão = 401; conta comum = 403; admin sem 2FA = 403 (a tela mostra o QR Code); admin com 2FA e sem `X-2FA-Token` = 401.
- Promoção a admin e reset de 2FA só por comando local (`criar_admin.py`), nunca pela API.
- O front tem seis abas: **Auditoria de Divergências**, **Resumo Geral**, **Aprendizado da IA (Marcas)**, **Configurações**, **Exportar Base** e **Sugestões da IA**.
- **Agrupamento** usa a chave final do produto: EAN (quando válido) ou descrição normalizada, mais marca, volume e embalagem. Regras `juntar` fundem grupos; regras `mover` movem uma descrição para outro grupo. O backend bloqueia origem = destino, destino do tipo `DESC_` e ciclos.
- **Similaridade** (espelhada em `auditoria_relatorio.py` e no front): 0,70 com EAN válido, 0,85 sem EAN, 0,80 de tokens principais, com travas de medida/qualificadores. O teste de integridade compara as duas pontas.
- **Pendências**:
  - `motivo="manual"`: descrição enviada pelo usuário; sai dos relatórios até ser aprovada.
  - `motivo="aberrante"`: variação ≥ 50%; continua visível no Impacto com alerta, mas **não soma** nos totais até o auditor validar.
- **Aprovação por descrição**: aprovar uma descrição também valida as chaves legadas `DESC_<grupo>_...` da mesma descrição, mesmo quando a chave do grupo ganhou sufixo novo (marca/volume/embalagem). Isso limpa o aviso amarelo de variação brusca.
- **Consistência aviso ↔ lista**: o aviso é calculado sobre o mesmo `df_trabalho` da lista (inclusive busca) e a correspondência é feita pelo **sufixo normalizado da descrição**, não pela chave completa. Antes, 7 chaves legadas apareciam como 7 avisos para 5 descrições; hoje aviso e lista mostram 5.
- Destaque visual de aberração é por descrição normalizada, então a mesma descrição em mais de uma chave antiga não duplica o destaque.
- Auditor pode aprovar, desfazer aprovação, marcar manual como pendente, mover descrição para grupo existente/novo e remover a pendência.
- As exportações de Excel (raio-x de grupos e catálogo completo) usam a mesma chave inteligente e exigem admin + 2FA.

#### Auditoria em massa com IA (DeepSeek)

- A IA é **segunda opinião**: nunca altera `itens_nota`, `itens_auditados` ou `regras_agrupamento` sozinha. Tudo vira sugestão `pendente` até o admin clicar em **Aplicar**.
- Rotas: `POST /api/auditoria/ia/validar-marcas`, `POST /api/auditoria/ia/resolver-ambiguidade` (apenas devolvem veredito), `POST /api/ia/auditar-base-completa`, `GET /api/ia/sugestoes`, `POST /api/ia/aplicar-sugestao/{id}`, `POST /api/ia/ignorar-sugestao/{id}`.
- `_auditar_base_completa` analisa somente grupos com **2+ descrições não equivalentes**, com limite opcional por chamada, e devolve resumo + erros pontuais sem derrubar a rodada.
- **Números de controle** (“- 1”, “- 2”, “- 3”) são removidos para a consulta e para a chave de equivalência; as descrições equivalentes viram uma única entrada para a IA.
- **Sem sugestões duplicadas**: antes de criar, o código indexa as sugestões já existentes pela descrição equivalente; se há uma pendente para a mesma descrição (mesmo em chave de grupo antiga), ela é **reaproveitada/atualizada** em vez de duplicada.
- **Respeita decisões anteriores**: sugestões já `aprovada` ou `ignorada` não são recriadas.
- `_sugestao_ainda_valida` descarta sugestões presas a chaves que não existem mais e grupos que, após remover números de controle, têm só uma descrição real.
- `_consolidar_sugestoes_pendentes` marca duplicatas antigas como `ignorada` (preserva histórico) na próxima rodada de auditoria.
- `GET /api/ia/sugestoes` com status `pendente` filtra inválidas/duplicadas; `status_aprovacao NULL` é tratado como pendente. O histórico aprovado/ignorado continua disponível quando filtrado.
- **Aplicar** move **todas as variações equivalentes** da descrição de uma vez:
  - sem `chave_destino`, cria um grupo `NOVO_`;
  - com `chave_destino`, move para um grupo existente;
  - grava/atualiza regra `mover`, aprova a chave da descrição no destino e remove a chave antiga de `itens_auditados`.
- **Ignorar** só muda o status; a base permanece intacta. Sugestão já aplicada não pode ser ignorada (409).
- Memória/custo: `marcas_ia` evita repetir consultas; o processamento em lotes (`/api/marcas/ia/processar`) devolve `consultas_ia`, `cache_hits`, `sem_marca` e `erro`, permitindo continuar apenas pelo cache se a IA falhar.

### 4.4 Relatórios gerenciais

- **Evolução de preços** (`GET /api/relatorio/evolucao`): preço médio da loja x mercado, período a período; janelas 7/15/30/90/180/360 dias e periodicidade diária a trimestral; filtro por loja e produto.
- **Comparativo de lojas** (`GET /api/relatorio/comparativo`): preços por loja, média de mercado, divergências e detalhamento das compras; limite configurável.
- **Impacto financeiro** (`GET /api/relatorio/impacto`): compara a **última** com a **penúltima** compra; `impacto_mensal = (preço atual − preço anterior) × consumo médio mensal` (consumo da janela de `meses_consumo` ÷ meses).
- Regras de integridade compartilhadas por todos: item pendente manual, bonificado/doação (CFOP 5910/6910 ou R$ 0,00) e produto sem preço válido ficam fora. Aberração ≥ 50% não conta como aumento/economia, é marcada e vai para o fim da lista; ela é registrada automaticamente na Auditoria como `aberrante`.
- A **média de mercado** usa todas as empresas da base para o mesmo item/período, mas devolve apenas o número; as compras detalhadas vêm somente das lojas do usuário.
- Tooltip de mercado mostra marcas substitutas (mesmo NCM + descrição parecida) com preço médio próprio, sem alterar a média principal.
- Exportações Excel: raio-x de grupos com 2+ descrições e catálogo completo por Produto + Marca, ambas com admin + 2FA.
- O aviso de integridade (`integridade.pendentes_auditoria`/`bonificados`) informa quantos itens ficaram fora do cálculo.

### 4.5 Integração ACBr API

- `backend/acbr_client.py` autentica com OAuth2 `client_credentials`, mantém o token em memória e renova antes do vencimento; credenciais só vêm do `.env`.
- Operações implementadas: consultar CEP, consultar/cadastrar/atualizar empresa, enviar certificado (base64), consultar/configurar Distribuição NF-e, pedir `dist-nsu`, listar documentos paginados e baixar XML completo.
- `POST /api/acbr/empresas` (multipart): valida o CNPJ da loja e o vínculo com a conta, valida o `.pfx/.p12` + senha, completa o endereço via BrasilAPI quando necessário, cadastra/atualiza a empresa, envia o certificado e configura a Distribuição NF-e.
- **Fluxo “zero cliques”**: depois do certificado aceito, a rota agenda em `BackgroundTasks` o download da Distribuição NF-e e a importação dos XMLs. A resposta volta na hora com “Certificado enviado! Iniciando download e importação em background...”. A tarefa usa sessão própria, respeita a regra de NSU (cursor `ultimo_nsu_acbr`; loja nova começa em 0), aplica o período só localmente e importa só `xmls/acbr/<CNPJ>/`, de forma incremental.
- Em “Minhas Lojas”, o cadastro é um `st.form` (`form_vincular_loja`: certificado `.pfx/.p12` + senha + expander **📍 Endereço fiscal**) com o **CNPJ fora do formulário** (`nova_loja_cnpj`, consulta automática da Receita) e o único envio é o submit **Salvar e vincular loja**, que encadeia `/api/empresas/cadastrar` (vínculo + barreira do certificado) e `/api/acbr/empresas` (certificado na ACBr + automação), usando o mesmo `.pfx/.p12` e senha. O CNPJ ficar fora do formulário é o que permite consultar `GET /api/empresas/consultar-cnpj` ao completar os 14 dígitos (`on_change` + botão **🔎 Buscar dados do CNPJ**) e preencher razão social/endereço **antes** de os widgets do formulário existirem no run — dentro do `st.form` nada dispara até o submit, então os campos nasciam vazios e o cadastro chegava à ACBr sem endereço (400 “Complete o cadastro da empresa”). **A URL é somente leitura:** `?pagina=` é consultado por `pagina_da_url()` quando a sessão perde a página (`restaurar_pagina_da_url()` roda depois do `exigir_login()` e antes do rádio do menu), mas o app **nunca escreve** em `st.query_params` — escrever dispara um rerun extra com o estado de widgets do run anterior (`UPDATE_FROM_QUERY_PARAMS` → `sendRerunBackMsg`), e era isso que devolvia o menu para “Relatórios” no primeiro submit. O console do Streamlit imprime `[menu] run=… pagina=… sessao_pagina=… widget=… url=…` e `[sessao] login/saida …` por run, para separar “login silencioso” de “estado de widget descartado”. Depois do sucesso, CNPJ, upload, senha e endereço são limpos e a confirmação aparece como `st.success`; o uploader usa uma chave versionada (`uploader_certificado_<versao>`) porque o `st.file_uploader` retém o arquivo em memória, e os checkboxes “Confirmar exclusão” também usam versão (`confirmar_excluir_cert_<id>_<versao>`) para nascerem desmarcados ao cadastrar e depois de excluir um certificado. O botão **não tem validação no front**: CNPJ vazio/curto, certificado ausente ou senha em branco são recusados pelo backend (HTTP 400 com `detail`), não pela tela, e a tela mostra a mensagem amigável (o JSON cru fica no expander **🛠 Diagnóstico do envio do cadastro**, fechado — antes era impresso solto na tela). O endereço do expander vai no `data` de `POST /api/acbr/empresas` e tem prioridade sobre o endereço já gravado na loja e sobre a BrasilAPI — é o fallback quando a Receita responde 429. O resultado aparece como `st.success("Loja cadastrada com sucesso!")` ou `st.error("Erro: <código> - <detail>")`; erro de endereço é acionável (o expander **📍 Endereço fiscal** abre, aparece o botão **📍 Abrir endereço fiscal agora** e o formulário não é limpo). No sucesso, o resultado fica em `st.session_state`, `st.cache_data.clear()` invalida a lista e a tela faz **um único `st.rerun()`** — que mostra o `st.success` + o cartão novo (antes a lista só recarregava no botão **🔄 Atualizar Lista**).
- Consulta à Receita (BrasilAPI) em `/api/empresas/cadastrar`: 3 tentativas com espera crescente em 429/5xx/timeout (respeitando `Retry-After`, teto de 10s; `BRASILAPI_ESPERA=0` deixa os testes instantâneos) e cada falha entra no log da API com o status real — o antigo `except: pass` escondia o 429 e o cadastro seguia sem endereço. A resposta traz `fonte_dados` (`brasilapi`/`indisponivel`) e `aviso` quando a Receita não respondeu; a tela mostra esse aviso em `st.warning` para o cliente preencher o endereço fiscal antes de a ACBr recusar com “Complete o cadastro da empresa para a ACBr”.
- A automação pode ser desligada com `ACBR_AUTO_IMPORTAR_APOS_CERTIFICADO=0` (usado no teste das rotas manuais).
- O arquivo do certificado e a senha **não são persistidos**: seguem apenas para a ACBr; o banco guarda somente titular, validade e data de envio.
- `POST /api/acbr/baixar-xmls`: garante a configuração, faz o pedido com `dist-nsu` (a ACBr **não aceita campos de data** no schema v3.1.18: o payload leva somente `cpf_cnpj`, `ambiente`, `tipo_consulta`, `dist_nsu`, `uf_autor` e `ignorar_tempo_espera`; o período é aplicado localmente depois), pagina a listagem com `$inlinecount=true` (booleano exigido pela ACBr; a string OData `allpages` é recusada), baixa somente NF-e completa, compara `dhEmi` com o período e salva em `xmls/acbr/<CNPJ>/<chave>.xml`.
- A Distribuição NF-e avança por NSU: `empresas.ultimo_nsu_acbr` guarda o último NSU devolvido pela ACBr. Loja com `0`/vazio é primeiro download e envia `dist_nsu=0`; depois disso a rota envia o NSU salvo. Tanto `consultar_ultimo_nsu` quanto `distribuir_nfe` releem esse valor no banco imediatamente antes da requisição; o argumento `dist_nsu` existe só por compatibilidade e não sobrescreve o cursor persistido. Se a ACBr recusar o valor e informar `dist_nsu deve ser igual a N`, o cursor é sincronizado e a consulta é repetida uma vez. O cartão da loja mostra “🔄 Último NSU”.
- `python atualizar_nsu_manual.py [--simular]` varre `xmls/` e grava o maior NSU encontrado por CNPJ destinatário. XMLs completos de NF-e normalmente não trazem NSU; nesse caso o script lista as lojas e mostra o `UPDATE` para informar o valor manualmente.
- `import_xmls.py` continua sendo a importação manual (limpa as notas e varre `xmls/**` por padrão), mas agora aceita `pastas`, `limpar=False`, `cnpj_padrao` e `apenas_novos=True`; é essa forma incremental que a automação usa.
- Avisos para `resNFe` (só resumo), status `processando` e erro de distribuição são devolvidos na resposta. Falhas viram HTTP com mensagem amigável (`_erro_acbr_para_http`).
- Cada cartão de loja tem **🔄 Forçar Download de XMLs** (chama `POST /api/acbr/baixar-xmls` para aquele CNPJ e mostra o erro real da ACBr) e **🔍 Consultar Status ACBr** apenas para contas com `is_admin`; o usuário comum continua vendo os dados da loja e o botão de excluir certificado.
- `GET /api/acbr/status/{cnpj}` delega ao cliente a leitura do NSU persistido em `empresas.ultimo_nsu_acbr` (0 no primeiro acesso), imediatamente antes do POST — a ACBr exige exatamente o `ultNSU` da chamada anterior — e persiste o `ultNSU` retornado. Se o banco estiver atrasado, o cliente grava o cursor exigido na mensagem `dist_nsu deve ser igual a N` e repete a consulta uma única vez. A resposta devolve `ultimo_nsu_local`, `ultimo_nsu_anterior`, `ultimo_nsu_acbr`, `pendentes`, o certificado e a última automação. Como o download do cadastro roda em background, o resultado/erro dessa tarefa fica em memória e no log da API; a rota de status expõe o diagnóstico para a tela.
- `tests/test_acbr_rotas.py` sobe uma ACBr fake local e uma instância real do FastAPI; não usa internet nem credenciais reais e cobre também a rota de status.

### 4.6 Autenticação, multitenancy e certificado

- Senha com passlib/bcrypt; token JWT HS256 com validade padrão de 12h; segredo em `NFE_SECRET_KEY` ou `.secret_key` gerado no primeiro start.
- Cadastro/login por e-mail e senha; cada empresa tem `usuario_id` e as rotas de lojas conferem o dono. Lojas novas são criadas com o `id` do usuário autenticado (nunca com um ID fixo do admin) e `GET /api/empresas/vinculadas` filtra por esse `usuario_id`. **`GET /api/lojas` também exige token** e devolve somente as empresas de `usuario_id == usuário do token` — antes não tinha autenticação e fazia `db.query(Empresa).all()`, vazando as lojas de todas as contas (o front ainda imprimia o JSON cru na tela). `GET /api/empresas/consultar-cnpj` (autopreenchimento do cadastro) só devolve endereço de loja da própria conta: CNPJ de outra conta volta como `de_outra_conta`, sem consultar a Receita e sem expor dados.
- **2FA TOTP** para admin: `/api/auth/2fa/setup` devolve segredo + QR Code; `/api/auth/2fa/verify` confirma o código de 6 dígitos e emite token curto de 2FA (padrão 2h), enviado em `X-2FA-Token`.
- Login do admin é **fail-closed**: a senha correta não devolve sessão; devolve um desafio de 2FA (10 min). A sessão só nasce após o código do autenticador. Admin sem 2FA configurado recebe 403 e configura o QR Code no próprio login.
- Promoção/remoção de admin e reset de 2FA apenas por `python criar_admin.py`.
- **Barreira do certificado**: o vínculo de loja exige `.pfx/.p12` + senha; valida extensão, tamanho (máx. 10 MB), senha, validade e CNPJ do certificado contra o CNPJ informado. Arquivo e senha nunca são gravados.
- **Confirmação de e-mail no cadastro:** `usuarios.email_verificado`/`email_verificado_em` + tabela `codigos_email` (hash SHA-256 com o segredo da aplicação, validade de 15 min, 5 tentativas, códigos anteriores queimados a cada reenvio). `/api/auth/registrar` não abre sessão; `/api/auth/validar-email` confirma e emite o token; `/api/auth/reenviar-codigo` respeita 1/minuto e 10/hora; falha de SMTP não apaga a conta (resposta traz `email_enviado=false` + `aviso`). Contas antigas foram migradas como verificadas (`ALTER ... DEFAULT 1`).
- Token da API do WhatsApp é global, fica em `configuracoes_sistema` e nunca volta inteiro (só os 4 últimos caracteres); somente admin + 2FA altera.

### 4.7 Alertas e relatórios por e-mail

- **Regra central (definida com o cliente):** o relatório lista **toda variação de preço** em relação à **última compra do mesmo produto na mesma loja** (diário) ou à **média simples das compras do período** contra a média do período anterior de mesma duração (semanal/mensal) — não existe mais "variação mínima". A comparação é por `(CNPJ, chave do grupo dos relatórios)` na **unidade-base** (`quantidade_base`/`valor_unitario_base`, CX24 → unidade) e usa a mesma base dos relatórios (bonificação/doação e descrições pendentes na Auditoria ficam de fora).
- **Conteúdo do e-mail** (`backend/relatorios_alerta.py`): preço anterior → atual, variação (%), **impacto mensal em R$** (`diferença de preço × consumo médio mensal dos últimos 90 dias`), resumo por loja, **produtos novos** (primeira compra), **recomprados** (voltaram depois de 30+ dias), notas **“⚠️ recebida com atraso”** e o **índice de inflação**. No tooltip das colunas Preço Anterior/Preço Atual, o diário mostra data + fornecedor de cada compra; semanal/mensal mostram **“Média de DD/MM a DD/MM - Compras: Fornecedor A (R$ X), …”** com **todas as compras que compuseram a média** (até 10 no `title`, depois “… e mais N compras”). Variação ≥ 50 % **não é escondida**: recebe o selo “⚠️ conferir”.
- **Três relatórios** com lote por **ordem de importação** (`notas_fiscais.id`), não por `data_emissao` — uma nota emitida ontem e importada hoje entra no relatório de hoje:
  - **diário** (06:00): notas novas desde o cursor;
  - **semanal** (segunda 06:00): notas da semana anterior (seg–dom) + atrasadas ainda não relatadas;
  - **mensal** (dia 1º 06:00): notas do mês anterior fechado + atrasadas.
  Na primeira execução (cursor 0) o relatório cobre só a janela do período/último dia com notas — nunca o histórico inteiro.
- **Idempotência:** `configuracoes_alerta.ultima_nota_*_id` (cursor por tipo), `ultimo_envio_diario/semanal/mensal` e a tabela `alertas_notas` (uma linha por nota/conta/tipo). O cursor e os marcadores só são gravados **depois** de o SMTP confirmar (`_registrar_envio_relatorio`); falha de envio devolve 502 e mantém as notas para a próxima tentativa.
- **Inflação** (`backend/inflacao.py`): **Laspeyres encadeado** — a cesta do mês anterior define os pesos (gasto), os produtos são comparados pelo preço médio por unidade-base e o resultado é renormalizado pelo peso comparado. Exibe **cobertura** da amostra e marca “amostra pequena” abaixo de 60 %. Série de 6 meses (índice fechado) + **prévia de 30 dias** por janelas móveis (4 semanas × 4 semanas) + top-5 maiores responsáveis.
- **E-mail** (`backend/email_service.py`): `multipart/alternative` com texto puro + HTML (tabelas com CSS inline, compatíveis com Gmail/Outlook). O destinatário é sempre `configuracoes_alerta.email`; o secundário vai como `Cc`.
- **Rotas novas:** `POST /api/alertas/relatorio/previa` (monta e devolve o relatório, sem envio) e `POST /api/alertas/relatorio/enviar` (envia e marca a cobertura). `GET/POST /api/alertas/configurar` passou a expor/salvar `alerta_diario`, `alerta_semanal` e `alerta_mensal`.
- **Agendador** (`backend/agendador.py`): thread daemon ligada no `lifespan` do FastAPI, fuso `America/Sao_Paulo`, verificação a cada minuto e disparo **apenas dentro da hora configurada** (`NFE_ALERTAS_HORA`, padrão 6) — reiniciar a API à tarde não dispara relatório fora de hora. `NFE_ALERTAS_AGENDADOR=0` desliga.
- **Rotas legadas** (`/api/alertas/gerar-mensagens` e `/api/alertas/enviar-email`) continuam no backend por compatibilidade: o simulador agora também inclui toda variação (a de 50 %+ com o selo “conferir”) e a tela não as usa mais.

### 4.8 Front-end Streamlit

- Menu lateral com quatro páginas: **📊 Relatórios (Cliente)**, **🔍 Auditoria (Admin)**, **🏬 Minhas Lojas** e **🔔 Configurar Alertas**.
- **Relatórios:** indicadores gerais e abas Evolução de Preços, Comparativo de Lojas e Impacto Financeiro; filtros por loja, período, periodicidade e produto; tabela com tooltips, destaque de aberrações, rolagem e exportação.
- **Auditoria:** seis abas descritas acima, incluindo busca, modo “apenas pendentes/todos”, aprovação, agrupamento, aprendizado de marcas, memória da IA, configurações e exportações. Exige 2FA e esconde o menu para não-admin (defesa em profundidade; o backend também bloqueia).
- **Minhas Lojas:** cadastro por CNPJ com **autopreenchimento da Receita** (`GET /api/empresas/consultar-cnpj` → razão social + endereço fiscal nos widgets, disparado pelo `on_change` do CNPJ e pelo botão **🔎 Buscar dados do CNPJ**), **endereço pelo CEP** (`GET /api/empresas/consultar-cep` → ViaCEP/BrasilAPI preenche rua/bairro/município/UF; o número/complemento continuam manuais e o que o cliente digitou à mão não é sobrescrito), upload do certificado + senha, cartões das lojas e exclusão do certificado. O **Código IBGE** saiu da tela: vem da Receita/CEP para `session_state["nova_loja_codigo_municipio"]` e segue no payload da ACBr. O endereço fica **fora do `st.form`** (o `on_change` do CEP precisa rodar antes dos widgets do formulário). Os botões **🔌 Testar Conexão**, **🔄 Atualizar Lista** e a legenda `API:` foram removidos (decisão do cliente); depois de salvar, a lista recarrega com `st.cache_data.clear()` + `st.rerun()`. O cadastro imprime `[Streamlit→API] POST <url> -> HTTP <status>` (sem token/senha) e, em caso de erro, o expander **🛠 Diagnóstico do envio do cadastro** (fechado) traz URL, status e trecho do corpo.
- **Alertas:** tela com **Alertas ativos** + toggles **Relatório diário / Resumo semanal / Fechamento mensal**, e-mail principal (pré-preenchido com o e-mail da conta) e secundário opcional (Cc). **🧪 Gerar prévia** mostra o relatório real (resumo, variações com R$/mês, resumo por loja, produtos novos, recomprados, índice de inflação com série e maiores responsáveis e o texto do e-mail); **📧 Enviar agora** envia a prévia por SMTP com confirmação, sempre para o e-mail salvo na conta. Variação mínima, janela, canal e seleção de lojas continuam fora da tela.
- **Login/Cadastro:** tela própria com abas; login 2FA; **cadastro com confirmação de e-mail** — `POST /api/auth/registrar` cria a conta com `email_verificado = False`, envia por SMTP um código de 6 dígitos (tabela `codigos_email`, hash + validade de 15 min) e **não abre sessão**. A tela mostra o campo do código, **📨 Reenviar código** (1/minuto, 10/hora) e **↩️ Voltar**; `POST /api/auth/validar-email` confirma, cria a configuração de alerta padrão (ativo, canal e-mail, destinatário = e-mail da conta) e devolve o token. Login de conta pendente responde **403** com `email_nao_verificado` e reabre a tela do código; contas antigas foram migradas como verificadas (`DEFAULT 1`). Sessão em `st.session_state`; tratamento central de sessão expirada.
- O front usa `requests` contra a API e `st.cache_data` no carregamento inicial. **As URLs estão fixas em `http://127.0.0.1:8000`** (ver pendências).

### 4.9 Consulta cadastral (CNPJ e CEP)

- `backend/consulta_cadastral.py` troca de **provedor** em vez de repetir no mesmo serviço quando a Receita limita as requisições (HTTP 429): **BrasilAPI → minhareceita.org → publica.cnpj.ws**; no CEP, **ViaCEP → BrasilAPI CEP**. 404 encerra na hora (CNPJ/CEP inexistente), 429/5xx/timeout passam para o próximo com pausa curta (respeita `Retry-After`). Nenhum provedor exige chave.
- Respostas são normalizadas para os campos do cadastro/ACBr (`razao_social`, `nome_loja`, `municipio`, `cep`, `uf`, `codigo_municipio`, `logradouro` com tipo da via, `numero`, `complemento`, `email`, `telefone`) — a `publica.cnpj.ws` aninha os dados em `estabelecimento` e é traduzida. `fonte` continua `brasilapi`/`indisponivel` e o provedor real fica em `provedor` (log/diagnóstico).
- Cache em memória (por processo) de resposta boa por `NFE_CACHE_CADASTRAL_HORAS` (padrão 720 h = 30 dias); falha nunca é cacheada. `NFE_ESPERA_ENTRE_PROVEDORES=0` deixa os testes instantâneos.
- O cadastro (`_consultar_cnpj_brasilapi`) mantém o cache de processo e o contrato antigo (`fonte="brasilapi"`, `None` quando nenhum provedor responde), então a ACBr continua recebendo o endereço mesmo com a BrasilAPI fora do ar.

### 4.10 Integração Omie (notas de entrada)

**Fase 1 — cliente HTTP (`backend/omie_client.py`).** A API do Omie não usa OAuth: toda chamada é um `POST` JSON `{"call", "app_key", "app_secret", "param"}`. Dois métodos implementados:

- `ListarRecebimentos` (`/api/v1/produtos/recebimentonfe/`): notas de entrada já lançadas, com `cExibirDetalhes="S"` (traz itens/parcelas/`infoCadastro`), filtro por **data de emissão** (`dtEmissaoDe`/`dtEmissaoAte`, opcionais) e `cEtapa` do kanban (40 Faturado, 60 Recebido, 80 Conferido).
- `ListarDocumentos` (`/api/v1/contador/xml/`): XML completo em `cXml`; `cModelo="55"` e `cOperacao="0"` são obrigatórios para vir **entrada** (o default do Omie é `1` = saída).

Erro em dois níveis (HTTP 2xx não basta): status fora de 2xx vira `ErroOmie` com o corpo preservado e, com HTTP 200, `omie_fail`/`cCodStatus != "0"` também viram `ErroOmie` (sem isso uma recusa apareceria como “nenhuma nota”). A Omie responde **HTTP 425** em rajadas de chamadas, por isso `INTERVALO_ENTRE_PAGINAS = 1.0` entre páginas. `OMIE_TIMEOUT` (padrão 60 s) e `testar_omie.py` permitem smoke sem banco.

**Credenciais por loja (`backend/cripto_credenciais.py`).** `empresas.omie_app_key`/`omie_app_secret` guardam o texto **cifrado** com Fernet (AES-128-CBC + HMAC, via PBKDF2-HMAC-SHA256 sobre `OMIE_ENC_KEY` > `NFE_SECRET_KEY` > `.secret_key`), no formato `fernet:v1:<token>`; valor antigo em texto puro é devolvido como está e a migração é idempotente. Chave trocada levanta `InvalidToken` → `ErroCredenciais` com instrução de re-gravação (nunca devolve segredo pela metade). Leitura: credencial da **loja** tem prioridade; sem ela cai em `OMIE_APP_KEY`/`OMIE_APP_SECRET` do `.env`, e o resultado informa `origem_credenciais` (`empresa`/`ambiente`).

**Fase 2 — importação (`backend/omie_sync.py`).** `sincronizar_recebimentos(db, empresa, *, data_inicio, data_fim, dias, etapas, cliente, confirmar, recalcular_marcas_itens, atualizar_sync_em)` percorre as etapas e grava fornecedor/nota/itens na base do sistema, devolvendo contadores (`novas`, `duplicadas`, `canceladas`, `ignoradas`, `bonificados`, `notas_sem_itens`, `mapeamentos_*`, `itens`, `erros`, `etapas`, `origem_credenciais`, `simulado`, `sincronizado_em`).

| Campo do Omie | Destino |
|---|---|
| `cabec.cChaveNFe` (44 dígitos) | `notas_fiscais.chave_acesso` — identidade da nota (dedupe); sem 44 dígitos o recebimento é ignorado com aviso |
| `cabec.cNumeroNFe` / `cSerieNFe` | `numero_nota` / `serie` (sem zeros à esquerda; série vazia → `"1"`) |
| `cabec.dEmissaoNFe` (fallback `infoCadastro.dFat`) | `data_emissao` |
| `cabec.nValorNFe` (fallback `totais.vTotalNFe`) | `valor_total` |
| `cabec.cCNPJ_CPF` / `cRazaoSocial` (fallback `cNome`) | `fornecedores.cnpj` / `nome` (criado/reaproveitado por CNPJ, com cache na execução) |
| `infoCadastro.cCancelada = "S"` | nota **não** é gravada (contador `canceladas`) |
| `itensCabec.cCodigoProduto` | `itens_nota.c_prod` |
| `itensCabec.cDescricaoProduto` | `descricao_produto` (item sem descrição é descartado) |
| `itensCabec.cEAN` | `ean` (mesma regra do importador de XML: vazio/“SEM GTIN” → `SEM_EAN`, zeros à esquerda removidos) |
| `itensCabec.cNCM` / `cCFOP` | `ncm` / `cfop` (só dígitos, como no XML) |
| `cUnidadeNfe` / `itensAjustes.cUnidade` | `unidade_compra` |
| `nQtdeNFe` / `nPrecoUnit` / `vTotalItem` / `vDesconto` | `quantidade` / `valor_unitario` / `valor_total` — **valores crus**, para auditoria |
| embalagem (`interpretar_embalagem`) + `normalizar_quantidades` | `qtd_por_embalagem`, `unidade_base`, `quantidade_base`, `valor_unitario_base` |

Regras de negócio aplicadas:

- **Dedupe por chave**: a mesma nota vista em etapas diferentes (40/60/80) ou já existente no banco conta como `duplicadas` e não cria linha nova; o cabeçalho é commitado antes dos itens (uma falha de item não faz a nota “desaparecer” da próxima execução).
- **Bonificação/doação/brinde** (CFOP 5910/6910, preço 0, desconto integral ou termo BONIFIC/BRINDE/DOACAO na descrição) não entra em preço: vai para `bonificados`; nota só com bonificação é mantida (dedupe) e contada em `notas_sem_itens`.
- **De-para**: usa `mapeamento_fornecedor_produtos` (CNPJ do fornecedor + `cProd`) carregado uma vez por execução, aplica `aplicar_identidade`/`aplicar_embalagem_aprendida` e registra o aprendizado dos itens novos.
- **Marcas**: depois da importação chama `recalcular_marcas(db, apenas_cache=True)` (nenhuma chamada de IA), controlado por `recalcular_marcas_itens`; no fim o resumo traz `itens_sem_marca` e, quando > 0, um `aviso` mandando rodar `backfill_marcas.py` (marca `NULL` abre linha duplicada “(2)” no relatório, porque a marca entra na chave do grupo).
- **`omie_sync_em`**: gravado em ISO 8601 UTC ao fim de uma execução real.
- **Simulação (`confirmar=False`)**: faz `flush` a cada nota (validando constraints) e `rollback()` no fim — nenhuma linha permanece no banco.

**Scripts de operação.**

```powershell
# grava as credenciais da loja (cifradas) e encerra
python testar_omie_sync.py --gravar-credenciais --cnpj 12345678000199

# valida tudo sem gravar (rollback no fim; nada de rede além da API do Omie)
python testar_omie_sync.py --simular --dias 7

# importa de fato (padrão: hoje - 30 dias, etapas 40/60/80) — sempre recalcula marcas
python testar_omie_sync.py
python testar_omie_sync.py --tudo --etapas 40,60,80

# --sem-marcas só existe na simulação (importação real sem marca quebra o agrupamento)
python testar_omie_sync.py --simular --sem-marcas
```

`--cnpj` escolhe a loja (sem ele, só roda se houver **uma** loja cadastrada; com várias, lista as opções e falha), `--data-inicio`/`--data-fim` (dd/mm/aaaa) substituem `--dias` e `--tudo` importa o histórico inteiro. `--sem-marcas` sem `--simular` é recusado (uso inválido: nada é gravado). Códigos de saída: `0` sucesso, `1` falha de configuração/credencial/loja, `2` concluiu com erros por nota/etapa ou uso inválido de `--sem-marcas` (nestes casos `--simular` também sai com `2` e avisa que a simulação teve erros).

**Fase 3a — API e agendamento (24/11/2026).** O Omie agora é operado pela API, não só por script:

- `POST /api/omie/credenciais` grava `app_key`/`app_secret` da loja **cifrados** (`fernet:v1:`) e devolve apenas a máscara com os 4 últimos caracteres — o segredo não volta;
- `POST /api/omie/sincronizar` roda a fase 2 com a credencial da loja (ou o `.env` como fallback) e devolve o resumo (novas/duplicadas/itens/erros/etapas). Aceita `dias` (padrão 30; `null` = histórico), `data_inicio`/`data_fim` (as duas juntas), `etapas` (40/60/80) e `simular` (rollback, nada gravado);
- `GET /api/omie/status/{cnpj}` devolve `omie_sync_em`, as contagens da última sincronização real (resumo persistido em `configuracoes_sistema`, chave `omie_ultima_sync:<cnpj>`), se há credencial (loja/ambiente) e a chave mascarada — **nunca** o `app_secret`;
- o agendador (`backend/agendador.py`) roda **diariamente** na hora `NFE_OMIE_HORA` (padrão 3), uma vez por loja com credencial gravada, com pausa entre lojas (`NFE_OMIE_PAUSA_SEGUNDOS`, padrão 2 s, por causa do HTTP 425). A loja já sincronizada no dia é pulada (`omie_sync_em` convertido para o fuso local) e a falha não repete a cada minuto. O HTTP 500 `ERROR: Não existem registros para a página [1]!` é tratado como **sem dados** (`OmieSemRegistros` → lista vazia), não como erro fatal.

As três rotas usam a mesma checagem de dono das rotas ACBr (`_pode_gerenciar_loja`: admin vê todas). Erros: credencial ausente/ilegível e período inválido = 400; recusa da API do Omie = 502. O XML da nota só aparece no Omie depois que ela chega em **Recebido** (`cEtapa` 60); etapas 40 trazem apenas cabeçalho + `itensRecebimento`, e o `cXml` chega HTML-escapado (`html.unescape` antes de usar).

**Fase 3a parte 2 — tela "Fonte das notas" (Minhas Lojas, 24/11/2026).** Cada loja escolhe **de onde** baixa as notas, com apenas uma fonte ativa:

- **Omie:** painel com `app_key`/`app_secret` (formulário; segredo cifrado, nunca reexibido), status da última sincronização + contagens (`GET /api/omie/status/{cnpj}`) e botão **🔄 Sincronizar agora** com a opção **simular** marcada por padrão (testa sem gravar).
- **Certificado digital A1 (baixar direto da SEFAZ):** mantém o upload `.pfx/.p12` já existente, agora com **aviso destacado** e **checkbox de concordância obrigatório** ("Confirmo que as notas fiscais desta empresa NÃO são baixadas em nenhum outro sistema..."). Sem o aceite o certificado não é salvo/ativado; a concordância fica gravada com data/hora UTC e e-mail da conta (`empresas.cert_concordancia_em`/`cert_concordancia_por`) e aparece no cartão e no painel.
- A escolha fica em `empresas.fonte_notas` (`omie`/`certificado`; NULL nas lojas antigas é derivado do que a loja já tem). A rota nova `POST /api/empresas/{id}/fonte-notas` ativa a fonte, exige credencial para o Omie e a concordância para o certificado; `POST /api/empresas/cadastrar` aceita `fonte_notas="omie"` (vínculo **sem** certificado) e `concordancia_sefaz=true`. O seletor usa a mesma lista de lojas da tela: o admin vê/gerencia todas (com o e-mail do dono no rótulo) e a conta comum só as próprias.
- O formulário "Vincular novo CNPJ" ganhou o seletor de fonte **fora** do `st.form` (troca upload x credenciais no mesmo run). No certificado, o submit valida o checkbox antes de chamar a API e envia a concordância; no Omie, o vínculo e a gravação da credencial acontecem no mesmo clique.

---

## 5. Estado atual dos dados (banco real em 17/11/2026)

| Item | Quantidade |
|---|---:|
| Notas fiscais | 1.520 |
| Itens de nota | 5.734 |
| Empresas (lojas) | 3 |
| Usuários | 2 |
| Aprovações na Auditoria (`auditado=1`) | 29 |
| Pendências `aberrante` (`auditado=0`) | 7 chaves |
| Sugestões IA `pendente` (bruto) | 45 |
| Sugestões IA `aprovada` / `ignorada` | 6 / 1 |
| Memória `marcas_ia` | 742 descrições |
| Catálogo `marcas_master` | 498 marcas |

- As **7 chaves legadas** de aberração correspondem a **5 descrições reais**: 3 chaves de “ORIGINAL 600ML” (2 descrições distintas, uma repetida), 1 de “ARROZ BRANCO FOOD SERVICE CAMIL TP1 6X5K”, 1 de “ANCHO FILE DE COSTELA” e 2 de “CHOPP MADALENA LAGER 20” (mesma descrição). É exatamente o caso corrigido: aviso e lista agora contam 5.
- As **45 sugestões pendentes brutas** são reduzidas a **4 cards válidos** pelo filtro de duplicadas/desatualizadas de `GET /api/ia/sugestoes`. As linhas antigas continuam no banco; `_consolidar_sugestoes_pendentes` as marca como `ignorada` na próxima rodada de auditoria.
- O banco de 0 bytes na raiz não contém dados e não é usado pela aplicação.

---

## 6. Testes e validação

As suítes são **scripts Python**, não pytest (pytest não está instalado no ambiente). A suíte principal registra **74 testes** via decorador `@teste` e exige a API rodando em `http://127.0.0.1:8000`.

### Comandos

```powershell
# API (necessária para a suíte principal)
python -m uvicorn backend.main:app --port 8000

# Front
python -m streamlit run frontend/app.py

# Suíte principal (74 testes, usa o banco real e cria contas/notas de teste)
python tests/test_integridade_relatorios.py

# Relatório por e-mail + inflação + agendador (sem API)
python tests/test_alertas_relatorio.py
python tests/test_inflacao.py

# ACBr fake end-to-end (não precisa da API já ligada)
python tests/test_acbr_rotas.py
python tests/test_acbr_endereco.py

# Download + importação automática (SQLite temporário, sem ACBr)
python tests/test_acbr_auto_import.py

# Multitenancy das lojas e consulta de CNPJ (banco em memória)
python tests/test_multitenancy_lojas.py

# Inteligência de marcas (cópia do banco)
python tests/test_marcas.py
python tests/test_marcas_master.py

# SMTP (falso) e confirmação de e-mail
python tests/test_email_service.py
python tests/test_verificacao_email.py

# Sincronização Omie sem rede (cliente falso + SQLite temporário)
python tests/test_omie_sync.py

# Rotas /api/omie/* e agendador diário (mock, SQLite temporário)
python tests/test_omie_rotas.py

# Migração SQLite -> PostgreSQL (destino temporário; só lê a base real)
python tests/test_migracao_postgres.py
```

### Último resultado registrado

- `python -m py_compile` de todos os módulos (backend, front, testes): **OK**.
- `tests/test_omie_sync.py`: **OK** — migração aditiva idempotente das colunas Omie, credenciais Fernet (cifra, lê de volta, origem loja/ambiente, erro de chave trocada), sincronização real contra `ClienteFalso` (mapeamento de chave/número/série/data/valor/fornecedor/EAN/NCM/CFOP, valores crus vs `*_base`, bonificação ignorada, cancelada não gravada, dedupe na segunda execução, `--simular` = rollback), tudo em SQLite temporário e sem rede.
- `tests/test_omie_rotas.py`: **12/12** — credencial cifrada + máscara (sem segredo na resposta), 403/400/404, status com `omie_sync_em`/contagens e credencial ilegível, sincronização com janela padrão/datas/etapas/simulação e tradução de erros (400/502), agendamento diário (uma vez por loja, pausa, não repete no dia) e HTTP 500 "Não existem registros" = sem dados. `py_compile` de `backend/main.py`, `backend/agendador.py`, `backend/omie_client.py` e do teste: **OK**; `tests/test_acbr_rotas.py` continua passando (app sobe com os dois agendadores).
- `tests/test_migracao_postgres.py`: **passou** (cópia com as colunas novas, IDs/contagens preservados).
- `tests/test_multitenancy_lojas.py`: **14/14** (inclui as colunas novas de `empresas`, a fonte das notas Omie x certificado, a concordância obrigatória com data/usuário, o bloqueio de loja de outra conta e o cadastro com `fonte_notas="omie"` sem certificado).
- Tela da fonte das notas validada por AppTest com a API mockada: painel Omie (status/contagens), troca para certificado (aviso + checkbox + botão desabilitado), POST `/fonte-notas` com `concordancia_sefaz=true`, cadastro de CNPJ novo sem aceite (nenhuma chamada) e com aceite (concordância nos dois POSTs), além do fluxo Omie (`cadastrar` com `fonte_notas=omie` + `credenciais`). Testes de integração afetados rodaram contra API temporária: **8/8** AppTests/barreira do certificado (inclui `test_apptest_alertas_e_botao_excluir_certificado`, `test_apptest_cadastro_loja_formulario_e_endereco_fiscal` e `test_cadastro_avisa_quando_receita_indisponivel`); `tests/test_acbr_rotas.py` continua passando.
- `tests/test_marcas.py` e `tests/test_integridade_relatorios.py` agora chamam `init_db(engine)` na **cópia** do banco, então a cópia nasce com as migrações aditivas (antes a suíte quebrava em `no such column: empresas.omie_app_key`); a base real continua intocada. A suíte principal continua exigindo a API ligada.
- `testar_omie_sync.py` validado por harness fora do repositório (base temporária): `--gravar-credenciais` cifra e relê, `--simular` não grava, execução real importa/deduplica, CNPJ inexistente sai com `1`, parsers de `--etapas`/data/`--dias` e `--help` OK.
- Suíte principal: **74/74 testes passaram** (inclui os AppTests de Minhas Lojas/alertas, que exigem a API ligada).
- `tests/test_alertas_relatorio.py`: **16/16** (inclui o relatório de período com média e o tooltip listando todas as compras); `tests/test_inflacao.py`: **9/9**; `tests/test_email_service.py`: **8/8** (inclui `multipart/alternative` texto + HTML); `tests/test_acbr_endereco.py`: **11/11** (429 troca de provedor); `tests/test_multitenancy_lojas.py`: **6/6**; `tests/test_marcas*.py`, `test_acbr_rotas.py`, `test_acbr_auto_import.py` e `test_verificacao_email.py`: passaram.
- Relatório real (banco de produção, conta do admin): diário → 12 itens/1 nota, 9 variações, impacto **+R$ 15,35/mês**; semanal → 38 itens/14 notas, 17 variações (+R$ 219,69/mês); mensal → 591 itens/183 notas, 86 variações (+R$ 1.122,89/mês). Índice de inflação do mês fechado (out/26): **+2,92 %**, cobertura **76 %**; prévia de 30 dias **+0,10 %**, cobertura **62,8 %** — números coerentes entre si e com as compras do período.
- Base real: loja 4 (“Loja Exemplo”, da conta `sac@portucho.com.br`) removida com **backup em `C:\sistema_nfe\backup_sistema_nfe_pro_20261117_123854.db`**; `PRAGMA integrity_check` = `ok`; as 3 lojas do Rodrigo e as duas contas permanecem.

### Correções desta sessão (já implementadas)

1. **Aviso x lista**: correspondência por descrição normalizada (sufixo), não pela chave completa do grupo; contagem calculada no mesmo recorte da lista, inclusive busca.
2. **Marca como critério da IA**: prompt reforçado, exemplo Queijo Minas Camanducaia x Ipanema e regra explícita para nunca classificar marca como termo genérico.
3. **Números de controle**: `ia_consenso` remove “- 1”, “- 2” para consulta/equivalência, sem alterar o texto original de `itens_nota`.
4. **Sugestões duplicadas**: deduplicação por descrição equivalente, reaproveitamento de pendente existente, respeito a decisões anteriores e consolidação de duplicatas antigas como `ignorada`.
5. **Aplicar sugestão**: move todas as variações equivalentes (grupo novo ou existente) e não deixa a sugestão reaparecer.
6. **Aprovar descrição**: valida também as chaves legadas da mesma descrição, limpando o aviso de variação brusca.
7. **Testes**: novos casos para equivalência/deduplicação, listagem, aplicação e aprovação de chaves antigas.

Arquivos alterados nesta frente: `backend/ia_consenso.py`, `backend/main.py`, `frontend/app.py` e `tests/test_integridade_relatorios.py`.

### Frente de alertas por e-mail + Minhas Lojas (17/11/2026)

1. **Relatório de alertas por e-mail** (diário/semanal/mensal) com toda variação contra a última compra, impacto em R$/mês, novos/recomprados, atrasadas, resumo por loja e inflação (Laspeyres encadeado + prévia de 30 dias).
2. **Agendador** em thread (06:00, segunda e dia 1º), idempotente por cursor e marcadores; cursor só avança após o SMTP confirmar.
3. **E-mail HTML** (`multipart/alternative`) com texto puro como alternativa.
4. **Minhas Lojas:** CNPJ com fallback de provedor (fim do bloqueio por 429), CEP ViaCEP/BrasilAPI, IBGE fora da tela e remoção dos botões “Testar Conexão”/“Atualizar Lista” + legenda `API:`.
5. **Testes:** atualizados os AppTests/alertas e criados `test_alertas_relatorio.py` e `test_inflacao.py`.

Arquivos alterados nesta frente: `backend/consulta_cadastral.py` (novo), `backend/relatorios_alerta.py` (novo), `backend/inflacao.py` (novo), `backend/agendador.py` (novo), `backend/main.py`, `backend/models.py`, `backend/database.py`, `backend/alertas.py`, `backend/email_service.py`, `frontend/app.py`, `tests/*` e este resumo.

### Frente Omie — fases 1 e 2 (24/11/2026)

1. **Cliente HTTP** (`backend/omie_client.py`): `ListarRecebimentos` (detalhado, filtro por emissão e etapa) e `ListarDocumentos` (XML completo de entrada), com erro em dois níveis e pausa entre páginas por causa do HTTP 425.
2. **Credenciais cifradas por loja** (`backend/cripto_credenciais.py` + colunas em `empresas`): Fernet/PBKDF2, prefixo `fernet:v1:`, migração aditiva idempotente em `init_db`, fallback para o `.env` e erro claro quando a chave de criptografia muda.
3. **Sincronização** (`backend/omie_sync.py`): recebimentos do Omie → `fornecedores`/`notas_fiscais`/`itens_nota`, com dedupe por chave, descarte de canceladas e bonificações, de-para por fornecedor+`cProd`, valores crus + `*_base`, `omie_sync_em`, modo simulação e recálculo de marcas só por cache.
4. **Scripts**: `testar_omie.py` (cliente, sem banco), `testar_omie_sync.py` (`--simular`, `--gravar-credenciais`, `--cnpj`, `--dias`/`--data-inicio`/`--data-fim`/`--tudo`, `--etapas`, `--sem-marcas` só junto de `--simular`) e os diagnósticos `diagnostico_omie_cetapa.py`/`diagnostico_omie_xml.py`.
5. **Testes**: `tests/test_omie_sync.py` (offline, cliente falso) e `init_db(engine)` na cópia usada por `test_marcas.py`/`test_integridade_relatorios.py`.

Arquivos novos/alterados nesta frente: `backend/omie_client.py`, `backend/omie_sync.py`, `backend/cripto_credenciais.py`, `backend/models.py`, `backend/database.py`, `testar_omie.py`, `testar_omie_sync.py`, `tests/test_omie_sync.py`, `tests/test_marcas.py`, `tests/test_integridade_relatorios.py` e este resumo.

### Frente Omie — fase 3a: rotas e agendador (24/11/2026)

1. **Rotas** (`backend/main.py`): `POST /api/omie/credenciais` (grava cifrado e devolve só a máscara), `POST /api/omie/sincronizar` (janela/datas/etapas/`simular`, resumo completo) e `GET /api/omie/status/{cnpj}` (`omie_sync_em`, contagens da última execução, origem da credencial, sem segredo). Todas passam por `_loja_para_omie`/`_pode_gerenciar_loja`.
2. **Resumo persistido**: `_registrar_sincronizacao_omie` grava contadores/período/etapas/erros em `configuracoes_sistema` (`omie_ultima_sync:<cnpj>`); simulação não atualiza o resumo nem `omie_sync_em`.
3. **Agendador** (`backend/agendador.py`): segundo laço diário (`iniciar_omie`) na hora `NFE_OMIE_HORA` (padrão 3), só lojas com credencial, pausa `NFE_OMIE_PAUSA_SEGUNDOS` (padrão 2 s) entre lojas, marcação por dia (local) e `tentativas` em memória; `parar()` encerra os dois laços.
4. **HTTP 500 "Não existem registros"** (`backend/omie_client.py`): nova `OmieSemRegistros` + `sem_registros()`; `_chamar` classifica o erro e as quatro listagens (`recebimentos`/`documentos`, página e todas) devolvem `[]` em vez de exceção.
5. **Testes**: `tests/test_omie_rotas.py` (12 testes, mock e SQLite temporário) cobrindo rotas, permissões, status sem segredo, agendador e classificação do 500.

Arquivos novos/alterados nesta frente: `backend/main.py`, `backend/agendador.py`, `backend/omie_client.py`, `tests/test_omie_rotas.py` e este resumo. A tela em Minhas Lojas ficou para a parte 2 (abaixo).

### Frente Omie — fase 3a parte 2: tela "Fonte das notas" (24/11/2026)

1. **Seletor por loja (`frontend/app.py`)**: em **Minhas Lojas**, seção "⚙️ Fonte das notas por loja" com `selectbox` das lojas da conta (o admin vê todas, com o e-mail do dono) e `radio` **Omie** x **Certificado A1**. Cada painel mostra status e ações da fonte escolhida.
2. **Painel Omie**: `app_key`/`app_secret` em formulário (segredo cifrado, nunca reexibido), status da última sincronização + contagens via `GET /api/omie/status/{cnpj}` e botão **🔄 Sincronizar agora** com **simular** ligado por padrão; o resumo (novas/duplicadas/itens/avisos) aparece na própria tela.
3. **Painel Certificado**: aviso destacado de que a SEFAZ entrega cada nota a um único sistema, checkbox de concordância obrigatório, data da concordância já registrada (Brasília) e botão de ativação desabilitado até o aceite. O upload `.pfx/.p12` existente foi mantido no formulário "Vincular novo CNPJ".
4. **Backend**: colunas aditivas `empresas.fonte_notas`, `cert_concordancia_em` e `cert_concordancia_por` (migração idempotente em `init_db`); rota `POST /api/empresas/{id}/fonte-notas` (exige credencial para Omie e concordância para certificado); `POST /api/empresas/cadastrar` aceita `fonte_notas="omie"` (vínculo sem `.pfx`) e grava a concordância quando `concordancia_sefaz=true`; `/api/acbr/empresas` também grava/recusa a concordância. Excluir o certificado apaga a concordância; gravar credencial Omie ativa a fonte Omie. `_empresa_json` devolve `fonte_notas`, `cert_concordancia`, `omie_sync_em` e `omie_configurado` (sem segredo).
5. **Testes**: `tests/test_multitenancy_lojas.py` ganhou 5 casos (derivação da fonte e JSON sem segredo, concordância obrigatória + data/usuário, credencial Omie/403, exclusão limpando a concordância e cadastro com fonte Omie sem certificado); `tests/test_integridade_relatorios.py` marca o checkbox da concordância nos AppTests do cadastro. Fluxos de tela (painel Omie, troca para certificado, submit sem/com aceite e cadastro Omie) validados por AppTest temporário com a API mockada.

Arquivos novos/alterados nesta frente: `frontend/app.py`, `backend/main.py`, `backend/models.py`, `backend/database.py`, `backend/certificados.py`, `tests/test_multitenancy_lojas.py`, `tests/test_omie_rotas.py`, `tests/test_integridade_relatorios.py` e este resumo.

---

## 7. Pendências e riscos

### P0 — corrigir antes de considerar a entrega fechada

1. **Teste dependente do estado do banco** (`test_api_relatorios_sem_zerados_nem_pendentes`).
   - Hoje ele exige que o banco real tenha descrição pendente de divergência. Se a base estiver toda aprovada, falha sem haver bug no produto.
   - Correção recomendada: criar uma nota/descrição pendente dentro de um banco temporário ou marcar `skip` quando não houver pendências, em vez de assumir o estado da base de produção.

2. **Limpeza das sugestões antigas da IA no banco real.**
   - Há 45 linhas `pendente` brutas, mas só 4 cards válidos. Rodar uma auditoria completa (`POST /api/ia/auditar-base-completa`) para `_consolidar_sugestoes_pendentes` marcar as duplicatas como `ignorada` e conferir os 4 cards na tela.

3. **Autenticação/multitenancy nas rotas de relatórios.**
   - `GET /api/resumo-precos`, `GET /api/relatorios/exportar-*`, `GET /api/relatorio/evolucao`, `/comparativo` e `/impacto` não recebem `usuario`/token e não recortam pelos CNPJs da conta. Hoje o isolamento efetivo está no front e no filtro de loja, não no backend.
   - Antes de produção, exigir `auth.usuario_atual` e limitar as consultas às empresas do usuário (admin pode ter visão global, se for regra de negócio).

### P1 — funcionalidades incompletas

4. **Envio real dos alertas por WhatsApp.**
   - O e-mail já sai por SMTP (`POST /api/alertas/enviar-email`). Falta a integração HTTP com WhatsApp Business/Cloud API, além de agendador, retentativas, idempotência, opt-out e limite de frequência para os dois canais.

5. **Cofre do certificado digital A1.**
   - O `.pfx/.p12` e a senha não são persistidos por decisão de segurança. Para download recorrente sem reenvio manual, será necessário um cofre criptografado (KMS/secret manager), rotação, auditoria de acesso e política de retenção.

6. **Importação automática dos XMLs da ACBr — implementada no cadastro.**
   - `POST /api/acbr/empresas` agenda download + importação em background; a importação é incremental e deduplica por chave (`tests/test_acbr_auto_import.py`).
   - Continua faltando um agendador periódico para atualizar o NSU sem novo cadastro de certificado; hoje o fluxo é disparado pelo cadastro/reenvio do certificado ou pela rota manual.

7. **Integração Omie — fase 3 (rotas e tela).**
   - A fase 2 entrega o backend e os scripts; falta expor na API (`POST /api/omie/credenciais`, `POST /api/omie/sincronizar`, `GET /api/omie/status/{cnpj}`) e uma tela em **Minhas Lojas** (campos `app_key`/`app_secret` que nunca voltam inteiros, botão sincronizar com resumo e `omie_sync_em`).
   - Definir também se a sincronização será agendada (como o NSU da ACBr) e qual o recorte padrão por loja.

### P2 — operação, qualidade e infraestrutura

8. **Configuração de ambiente no front.**
   - `frontend/app.py` fixa `http://127.0.0.1:8000`. Parametrizar por variável de ambiente/`.streamlit/secrets.toml`, com validação e mensagem amigável quando a API estiver indisponível.

9. **Versionamento e CI.**
   - Não há repositório Git inicializado, pipeline, lint ou testes automatizados em CI. Também não há Alembic: as migrações são SQL aditivo em `init_db()`.
   - Recomendado: `git init`, branch protection, CI rodando `py_compile`, suítes de marcas/ACBr e a suíte principal contra um banco temporário.

10. **Infraestrutura e confiabilidade.**
   - SQLite é adequado ao volume atual, mas tem limitações de concorrência/backup. Definir rotina de backup, restauração testada, retenção de logs e monitoramento (erros 5xx, tempo de resposta, falhas de IA/ACBr).
   - Não há Docker/systemd/HTTPS documentado; a API sobe em HTTP local.

11. **Limpeza de código legado.**
    - `backend/processar_lote.py` **apaga** `../sistema_nfe_pro.db` antes de reprocessar. É perigoso e não deve ser executado.
    - `backend/xml_parser.py`, `backend/testar_parser.py`, `tests/_verificar_auditoria_tmp.py` e `sistema_nfe_pro.db` (0 bytes) são resíduos de desenvolvimento: mover para uma pasta `legacy/` ou remover, com registro no README.
    - `api.log`, `api.err.log`, `uvicorn_teste*.log` e `.uvicorn_teste.pid` são artefatos locais; não versionar.

12. **Custos/observabilidade da IA.**
    - Já existe cache e limite por lote, mas falta registrar custo/tokens por execução, aplicar orçamento máximo, retentativa com backoff e trilha de auditoria das decisões da IA.

13. **Qualidade dos dados.**
    - As 7 chaves legadas de aberração devem ser revisadas/aprovadas na tela após o deploy da correção; depois, conferir se o aviso some e se o Impacto volta a somar os itens validados.
    - Rodar os backfills pendentes (`backfill_relatorios.py`, `backfill_aprendizado.py`, `backfill_marcas.py`) quando houver importação antiga fora do fluxo.

---

## 8. Próximas tarefas sugeridas (ordem de execução)

### Sprint 0 — fechar a frente atual (curto prazo)

- [ ] Corrigir `test_api_relatorios_sem_zerados_nem_pendentes` para não depender do estado do banco real (banco temporário ou `skip` explícito).
- [ ] Rodar a suíte principal novamente e buscar **62/62**.
- [ ] Executar uma auditoria completa no banco real para consolidar as sugestões duplicadas e confirmar os **4 cards válidos**.
- [ ] Revisar/aprovar as 5 descrições das 7 chaves legadas e confirmar que o aviso de variação brusca some.
- [ ] Smoke test manual no Streamlit: aviso ↔ lista, Aplicar movendo todas as variações e Aprovar limpando chaves antigas.

### Sprint 1 — segurança e produção

- [ ] Exigir autenticação e recorte por empresas do usuário em `/api/resumo-precos`, `/api/relatorio/*` e exportações.
- [ ] Parametrizar a URL da API no front; remover `127.0.0.1` fixo.
- [ ] Definir CORS/HTTPS, rate limiting no login/2FA e política de bloqueio por tentativas.
- [ ] Revisar exposição de logs, segredos e mensagens de erro.

### Sprint 1.5 — Omie fase 3 (rotas + tela)

- [x] `POST /api/omie/credenciais` (grava cifrado, devolve só os 4 últimos caracteres), `POST /api/omie/sincronizar` (dispara o sync da loja e devolve o resumo) e `GET /api/omie/status/{cnpj}` (`omie_sync_em`, origem da credencial, última execução).
- [x] Tela em **Minhas Lojas**: seletor de **fonte das notas** (Omie x certificado A1) por loja, campos de `app_key`/`app_secret`, status da última sync + contagens, botão **🔄 Sincronizar agora** com resumo e simulação padrão, aviso + **concordância obrigatória** do certificado (data/hora + usuário) e cadastro de CNPJ novo já escolhendo a fonte.
- [x] Definir agendamento (mesma lógica do NSU da ACBr) e recorte padrão (dias/etapas) por loja — diário às `NFE_OMIE_HORA` (padrão 3), 30 dias/etapas 40/60/80, pausa entre lojas.

### Sprint 2 — alertas reais

- [ ] Implementar um `Notificador` com providers WhatsApp e e-mail, interface comum e testes com servidor fake.
- [ ] Agendador (cron/worker) para disparo periódico, com idempotência por conta/período.
- [ ] Registrar tentativas, falhas, custos e opt-out; retentativa com backoff.
- [ ] Tela para histórico de envios e status.

### Sprint 3 — ACBr ponta a ponta

- [ ] Cofre criptografado do certificado A1 (arquivo + senha), com rotação e auditoria.
- [ ] Rota/botão “Importar XMLs baixados”, com deduplicação, resumo e vínculo à loja.
- [ ] Status por documento (baixado/importado/ignorado/erro) e reprocessamento.
- [ ] Teste end-to-end cobrindo download + importação.

### Sprint 4 — operação e sustentação

- [ ] `git init`, `.gitignore` revisado, README e CI (py_compile + suítes).
- [ ] Migrações versionadas (Alembic ou equivalente) e backup/restauração automatizados.
- [ ] Monitoramento, logs estruturados e alertas de falha.
- [ ] Remover/quarentenar scripts legados perigosos e o banco de 0 bytes.

---

## 9. Invariantes de negócio (não quebrar)

1. **A IA nunca altera a base sozinha**: só grava sugestão; a mudança exige Aplicar.
2. **Valores crus do XML são preservados**; embalagem é identidade, não conversão de preço/quantidade.
3. **A marca não entra na média**: ela separa grupos e alimenta o tooltip; cada grupo usa o preço das suas próprias compras.
4. **Pendência manual esconde** o produto dos relatórios; **aberração permanece visível**, mas fora dos totais até validação.
5. **Variação ≥ 50% é aberração/divergência**, nunca aumento/economia.
6. **Números de controle não são identidade**: “- 1” e “- 2” são a mesma descrição para IA/equivalência.
7. **Aprovar uma descrição vale para as chaves legadas** da mesma descrição.
8. **2FA do admin é fail-closed**; conta comum não acessa Auditoria nem por API.
9. **Certificado e senha não vão para o banco** (apenas dados públicos).
10. **Uma única fonte de itens** para relatórios e alertas (`_carregar_itens_relatorio`), garantindo os mesmos excluídos.
11. **Credencial Omie é segredo**: no banco só entra cifrada (`fernet:v1:`); nenhuma rota/script imprime `app_secret` e a leitura prefere a loja ao `.env`.
12. **A mesma nota nunca entra duas vezes**: a chave de 44 dígitos é a identidade — vale para XML, ACBr e Omie, inclusive entre etapas do kanban.

---

## 10. Roteiro de smoke test manual

Com a API em `:8000` e o Streamlit rodando:

1. **Login admin**: entrar com e-mail/senha e confirmar o código de 6 dígitos. Sem 2FA, o QR Code deve aparecer; conta comum não deve ver a Auditoria.
2. **Auditoria → aba Divergências**: conferir se o aviso amarelo de variação brusca mostra **5** e se a lista mostra os mesmos **5 grupos**. Testar a busca e confirmar que o aviso acompanha o recorte.
3. **Sugestões da IA**: confirmar **4 cards válidos**, sem “Coca-Cola”/“Queijo”/“Carne” repetidos. Aplicar um card e verificar se todas as variações equivalentes saíram do grupo de origem.
4. **Rodar auditoria de novo** e confirmar que a sugestão aplicada não volta e que duplicatas antigas não reaparecem.
5. **Aprovar uma descrição** com chave legada e confirmar que o aviso correspondente desaparece e que o item volta a somar no Impacto.
6. **Relatórios**: conferir Evolução, Comparativo e Impacto; aberrações no fim, totais sem aberrações e sem pendências manuais; tooltip de marcas substitutas.
7. **Minhas Lojas**: cadastrar/consultar CNPJ, testar certificado inválido (deve bloquear) e exclusão do certificado.
8. **Alertas**: salvar configuração, simular mensagem e conferir aumentos/quedas/divergências e divisão em partes.
9. **ACBr**: rodar `python tests/test_acbr_rotas.py`; opcionalmente `python testar_acbr.py` para autenticação real no sandbox.
10. **Omie**: `python tests/test_omie_sync.py` (offline) e depois `python testar_omie_sync.py --simular --dias 3 --cnpj <CNPJ>` — conferir o resumo e se a base não mudou (`omie_sync_em` só avança sem `--simular`).

---

## 11. Limitações conhecidas

- A extração de marca é heurística e depende de haver variação de descrições no mesmo NCM; não é um catálogo universal.
- A IA (DeepSeek) é não determinística: prompts, cache e validação local reduzem, mas não eliminam respostas ruins. Por isso a aplicação nunca aplica sozinha.
- A similaridade da Auditoria é textual/medidas; casos ambíguos exigem decisão humana (ou segunda opinião da IA).
- SQLite e o volume atual atendem ao uso local; não há teste de carga nem concorrência de escrita em escala.
- O envio de alertas por e-mail é real (SMTP), mas o disparo por WhatsApp continua simulado; não há agendamento automático, retentativa nem histórico de envios.
- O certificado digital é efêmero (não persiste), então automação recorrente da ACBr ainda depende de reenvio.
- As rotas de relatório não têm isolamento por conta no backend (item P0).
- A suíte principal usa o banco real e a API no ar; alguns testes são sensíveis ao estado dos dados.
- O front fala com um único endereço local fixo.
- A integração Omie tem API e agendador (fase 3a), mas ainda **não tem tela**: a credencial pode ser gravada por `POST /api/omie/credenciais` ou pelo script, e o recorte padrão do agendador é 30 dias/etapas 40/60/80 (a rota aceita outros recortes).
- Não há Git, CI, container, HTTPS ou migrações versionadas.

---

## 12. Decisões em aberto (precisam de definição de produto)

1. Relatórios serão **por conta** (cada cliente vê só suas lojas) ou o admin pode ter visão global? Isso define a correção de multitenancy.
2. O canal de alerta oficial será WhatsApp Cloud API, outro provedor ou apenas e-mail no primeiro momento?
3. O certificado A1 será armazenado em cofre gerenciado (AWS/GCP/Azure) ou em banco criptografado com chave local?
4. A importação ACBr deve ser automática após o download ou continuar como ação explícita do usuário?
5. Qual o SLA/orçamento mensal de IA e qual o comportamento ao estourar (parar, usar só cache, avisar admin)?
6. O que fazer com as 45 sugestões antigas: consolidar automaticamente ou manter histórico completo para auditoria?
7. Omie: a sincronização oficial é só por `recebimentos` (etapas 40/60/80) ou também traz notas ainda não lançadas? E cada loja tem uma conta Omie própria (credencial por `empresas`) ou uma conta do grupo?
8. Omie: o que fazer quando a mesma nota já existe pelo XML da ACBr (mantém, atualiza preço/fornecedor ou só marca como conferida)?

---

## 13. Resumo executivo

O núcleo do produto está **implementado e utilizável localmente**: importa NF-e, aprende produtos, separa marcas, audita divergências com 2FA, gera relatórios confiáveis e exporta a base. A frente recente de Auditoria/Sugestões-IA foi corrigida e validada: aviso e lista agora coincidem, a marca é tratada como critério, números de controle não duplicam descrições, sugestões duplicadas são consolidadas e aplicar uma sugestão move todas as variações.

O que separa o sistema de uma operação de produção é principalmente **segurança/multitenancy nas rotas de relatório, envio real de alertas por WhatsApp, cofre do certificado, importação automática da ACBr, infraestrutura (Git/CI/backup) e a correção do teste dependente do banco**. A ordem recomendada está na seção 8.

---

*Documento gerado a partir da leitura do código, do banco real e dos resultados de teste em 13/11/2026. Atualizado na mesma data para registrar o envio real de e-mail dos alertas. Atualize este arquivo sempre que uma pendência da seção 7/8 for concluída.*











