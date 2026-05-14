"""
=============================================================================
LABORATÓRIO DE PROGRAMAÇÃO – 1º ANO | ENGENHARIA INFORMÁTICA
Normalização de Texto com Pipeline de Pré-Processamento e SLMs
=============================================================================
ESTRUTURA DO FICHEIRO:
  - ETAPA 1: Extração de Texto Multi-formato (linha ~50)
  - ETAPA 2: Pipeline de Limpeza e Pré-Processamento (linha ~130)
  - ETAPA 3: Preparação do Input para Normalização (linha ~230)
  - INTERFACE WEB (Flask) (linha ~310)
=============================================================================
DEPENDÊNCIAS:
  pip install flask pymupdf python-docx langdetect
=============================================================================
"""

import os
import re
import io
import json
import unicodedata
import datetime
import difflib
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, render_template_string, send_file

# Dependências para extração de texto
import fitz          # PyMuPDF  →  pip install pymupdf
import docx          # python-docx → pip install python-docx
from langdetect import detect  # langdetect → pip install langdetect

app = Flask(__name__)

# =============================================================================
# ETAPA 1 – EXTRAÇÃO DE TEXTO MULTI-FORMATO
# =============================================================================

def extrair_texto_pdf(file_bytes: bytes) -> str:
    """
    Extrai texto bruto de um ficheiro PDF preservando quebras de linha,
    artefactos e inconsistências originais.
    """
    texto_bruto = []
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for num_pagina, pagina in enumerate(doc, start=1):
            texto_pagina = pagina.get_text("text")  # mantém layout original
            texto_bruto.append(f"[PÁGINA {num_pagina}]\n{texto_pagina}")
    return "\n".join(texto_bruto)


def extrair_texto_docx(file_bytes: bytes) -> str:
    """
    Extrai texto bruto de um ficheiro DOCX, parágrafo a parágrafo,
    preservando quebras de linha originais.
    """
    documento = docx.Document(io.BytesIO(file_bytes))
    linhas = []
    for paragrafo in documento.paragraphs:
        linhas.append(paragrafo.text)  # inclui linhas vazias (estrutura original)
    return "\n".join(linhas)


def extrair_texto_txt(file_bytes: bytes) -> str:
    """
    Extrai texto bruto de um ficheiro TXT.
    Tenta UTF-8; se falhar, usa latin-1 como fallback.
    """
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1", errors="replace")


def extrair_texto(file_bytes: bytes, nome_ficheiro: str) -> dict:
    """
    Ponto de entrada da Etapa 1.
    Deteta o formato pelo nome do ficheiro e delega para a função correta.

    Retorna:
        dict com 'texto' (str) e 'formato' (str)
    """
    extensao = nome_ficheiro.rsplit(".", 1)[-1].lower()

    if extensao == "pdf":
        texto = extrair_texto_pdf(file_bytes)
        formato = "PDF"
    elif extensao == "docx":
        texto = extrair_texto_docx(file_bytes)
        formato = "DOCX"
    elif extensao == "txt":
        texto = extrair_texto_txt(file_bytes)
        formato = "TXT"
    else:
        raise ValueError(f"Formato não suportado: .{extensao}. Use PDF, DOCX ou TXT.")

    return {"texto": texto, "formato": formato}


# =============================================================================
# ETAPA 2 – PIPELINE DE LIMPEZA E PRÉ-PROCESSAMENTO
# =============================================================================

def remover_artefactos(texto: str) -> str:
    """
    Remove caracteres inválidos, símbolos de controlo e artefactos de encoding
    (ex: caracteres de substituição \ufffd, caracteres nulos, etc.).
    Mantém letras, números, pontuação comum e espaços.
    """
    # Remove caracteres de controlo (exceto \n e \t)
    texto = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', texto)
    # Remove caractere de substituição unicode
    texto = texto.replace('\ufffd', '')
    # Normaliza caracteres unicode para a forma NFC (resolve encoding misto)
    texto = unicodedata.normalize('NFC', texto)
    return texto


def reconstruir_paragrafos(texto: str) -> str:
    """
    Junta linhas que foram incorretamente quebradas a meio de uma frase
    (típico de PDFs). Uma quebra de linha é considerada incorreta se:
    - a linha anterior NÃO termina com pontuação final (. ! ? : ;)
    - a linha seguinte começa com letra minúscula ou não começa nova frase
    Linhas em branco são preservadas como separadores de parágrafo.
    """
    linhas = texto.split('\n')
    resultado = []
    i = 0
    while i < len(linhas):
        linha_atual = linhas[i].rstrip()
        if not linha_atual:
            resultado.append('')
            i += 1
            continue
        # Tenta juntar com a próxima linha se for quebra incorreta
        while (i + 1 < len(linhas)
               and linhas[i + 1].strip()
               and not re.search(r'[.!?:;]\s*$', linha_atual)
               and re.match(r'^[a-záàãâéêíóôõúüça-z]', linhas[i + 1].strip())):
            i += 1
            linha_atual = linha_atual.rstrip() + ' ' + linhas[i].strip()
        resultado.append(linha_atual)
        i += 1
    return '\n'.join(resultado)


def remover_cabecalhos_rodapes(texto: str, min_repeticoes: int = 2) -> str:
    """
    Deteta e remove linhas que se repetem frequentemente no documento
    (cabeçalhos/rodapés típicos de PDFs multipágina).
    Uma linha é considerada repetida se aparece >= min_repeticoes vezes.
    """
    linhas = texto.split('\n')
    from collections import Counter
    contagem = Counter(l.strip() for l in linhas if l.strip())
    linhas_repetidas = {linha for linha, count in contagem.items()
                        if count >= min_repeticoes and len(linha) > 3}
    resultado = [l for l in linhas if l.strip() not in linhas_repetidas]
    return '\n'.join(resultado)


def normalizar_espacos_pontuacao(texto: str) -> str:
    """
    - Colapsa múltiplos espaços em branco num único espaço
    - Remove espaços antes de pontuação (ex: "olá ." → "olá.")
    - Garante espaço após pontuação final
    - Colapsa mais de 2 linhas em branco consecutivas em 2
    """
    # Colapsa espaços múltiplos (exceto quebras de linha)
    texto = re.sub(r'[ \t]+', ' ', texto)
    # Remove espaço antes de pontuação
    texto = re.sub(r'\s+([.!?,;:])', r'\1', texto)
    # Garante espaço após pontuação se seguida de letra
    texto = re.sub(r'([.!?])([A-ZÁÀÃÂÉÊÍÓÔÕÚA-Z])', r'\1 \2', texto)
    # Colapsa linhas em branco excessivas
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


def limpar_texto(texto: str, opcoes: dict = None) -> str:
    """
    Ponto de entrada da Etapa 2.
    Aplica a pipeline de limpeza de forma configurável.

    opcoes (dict) – permite ativar/desativar cada passo:
        {
            "remover_artefactos": True,
            "reconstruir_paragrafos": True,
            "remover_cabecalhos_rodapes": True,
            "normalizar_espacos": True
        }
    Todos os passos estão ativos por omissão.
    """
    if opcoes is None:
        opcoes = {}

    passos_activos = {
        "remover_artefactos": opcoes.get("remover_artefactos", True),
        "reconstruir_paragrafos": opcoes.get("reconstruir_paragrafos", True),
        "remover_cabecalhos_rodapes": opcoes.get("remover_cabecalhos_rodapes", True),
        "normalizar_espacos": opcoes.get("normalizar_espacos", True),
    }

    texto_processado = texto

    if passos_activos["remover_artefactos"]:
        texto_processado = remover_artefactos(texto_processado)

    if passos_activos["remover_cabecalhos_rodapes"]:
        texto_processado = remover_cabecalhos_rodapes(texto_processado)

    if passos_activos["reconstruir_paragrafos"]:
        texto_processado = reconstruir_paragrafos(texto_processado)

    if passos_activos["normalizar_espacos"]:
        texto_processado = normalizar_espacos_pontuacao(texto_processado)

    return texto_processado


# =============================================================================
# ETAPA 3 – PREPARAÇÃO DO INPUT PARA NORMALIZAÇÃO
# =============================================================================

PROMPTS_POR_IDIOMA = {
    "pt": (
        "Normaliza o seguinte texto em português, corrigindo erros ortográficos, "
        "melhorando a pontuação e tornando o texto mais claro e coerente. "
        "Devolve apenas o texto corrigido, sem comentários adicionais:\n\n"
    ),
    "en": (
        "Normalize the following English text by fixing spelling errors, "
        "improving punctuation and making it clearer and more coherent. "
        "Return only the corrected text, without additional comments:\n\n"
    ),
    "es": (
        "Normaliza el siguiente texto en español, corrigiendo errores ortográficos, "
        "mejorando la puntuación y haciéndolo más claro y coherente. "
        "Devuelve solo el texto corregido, sin comentarios adicionales:\n\n"
    ),
    "fr": (
        "Normalise le texte français suivant en corrigeant les fautes d'orthographe, "
        "en améliorant la ponctuation et en le rendant plus clair et cohérent. "
        "Retourne uniquement le texte corrigé, sans commentaires supplémentaires:\n\n"
    ),
}
PROMPT_PADRAO = (
    "Normalize the following text by fixing errors, improving punctuation "
    "and making it clearer. Return only the corrected text:\n\n"
)


def detectar_idioma(texto: str) -> str:
    """
    Deteta automaticamente o idioma do texto usando langdetect.
    Retorna o código ISO 639-1 (ex: 'pt', 'en', 'es').
    Em caso de falha, retorna 'en' por omissão.
    """
    try:
        # Usa apenas os primeiros 500 caracteres para eficiência
        amostra = texto[:500].strip()
        if not amostra:
            return "en"
        return detect(amostra)
    except Exception:
        return "en"


def segmentar_texto(texto: str, tamanho_chunk: int = 800, sobreposicao: int = 100) -> list[str]:
    """
    Divide o texto em blocos (chunks) de tamanho controlado, com sobreposição
    para preservar contexto entre blocos.

    Parâmetros:
        tamanho_chunk  – número máximo de palavras por bloco (default: 800)
        sobreposicao   – número de palavras sobrepostas entre blocos (default: 100)

    Retorna:
        Lista de strings (blocos de texto)
    """
    palavras = texto.split()
    if not palavras:
        return []

    chunks = []
    inicio = 0
    while inicio < len(palavras):
        fim = inicio + tamanho_chunk
        chunk = ' '.join(palavras[inicio:fim])
        chunks.append(chunk)
        # Avança considerando a sobreposição
        inicio += tamanho_chunk - sobreposicao

    return chunks


def criar_prompt(chunk: str, idioma: str) -> str:
    """
    Cria o prompt adequado para o chunk, adaptado ao idioma detetado.
    """
    template = PROMPTS_POR_IDIOMA.get(idioma, PROMPT_PADRAO)
    return template + chunk


def preparar_input(texto_limpo: str, tamanho_chunk: int = 800) -> dict:
    """
    Ponto de entrada da Etapa 3.
    Segmenta o texto limpo e prepara os prompts para envio ao SLM.

    Retorna:
        dict com:
            'idioma'    – idioma detetado
            'num_chunks'– número de blocos criados
            'chunks'    – lista de blocos de texto
            'prompts'   – lista de prompts prontos a enviar à API
    """
    idioma = detectar_idioma(texto_limpo)
    chunks = segmentar_texto(texto_limpo, tamanho_chunk=tamanho_chunk)
    prompts = [criar_prompt(chunk, idioma) for chunk in chunks]

    return {
        "idioma": idioma,
        "num_chunks": len(chunks),
        "chunks": chunks,
        "prompts": prompts,
    }


# =============================================================================
# ETAPA 4 – CONEXÃO À API DO SLM
# =============================================================================

SLM_API_URL = "https://reality.utad.net/slm"
SLM_MODEL   = "llama-3.2-1b-instruct"


def enviar_para_slm(prompt: str, timeout: int = 60) -> dict:
    """
    Envia um pedido HTTP POST à API do SLM com o prompt fornecido.

    Parâmetros:
        prompt  – texto completo a enviar (prompt + chunk)
        timeout – tempo máximo de espera em segundos (default: 60)

    Retorna:
        dict com:
            'sucesso'    – bool
            'resposta'   – texto normalizado devolvido pelo modelo (str)
            'erro'       – mensagem de erro, se houver (str ou None)
            'modelo'     – modelo utilizado
            'tokens'     – informação de tokens usados (se disponível)
    """
    payload = {
        "model": SLM_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    dados = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLM_API_URL,
        data=dados,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            corpo = resp.read().decode("utf-8")
            resultado = json.loads(corpo)

        # Debug
        print("RESPOSTA API SLM:", json.dumps(resultado, ensure_ascii=False)[:800])

        # Extrai o texto da resposta (testa vários formatos)
        texto_resposta = ""
        if "choices" in resultado and resultado["choices"]:
            escolha = resultado["choices"][0]
            if "message" in escolha:
                texto_resposta = escolha["message"].get("content", "")
            elif "text" in escolha:
                texto_resposta = escolha["text"]
            elif "content" in escolha:
                texto_resposta = escolha["content"]
        elif "content" in resultado:
            conteudo_api = resultado["content"]
            if isinstance(conteudo_api, list):
                texto_resposta = " ".join(c.get("text", "") for c in conteudo_api if isinstance(c, dict))
            else:
                texto_resposta = str(conteudo_api)
        elif "response" in resultado:
            texto_resposta = resultado["response"]
        elif "text" in resultado:
            texto_resposta = resultado["text"]
        elif "message" in resultado:
            msg = resultado["message"]
            texto_resposta = msg.get("content", "") if isinstance(msg, dict) else str(msg)

        tokens = resultado.get("usage", {})

        return {
            "sucesso": True,
            "resposta": texto_resposta.strip(),
            "erro": None,
            "modelo": SLM_MODEL,
            "tokens": tokens
        }

    except urllib.error.HTTPError as e:
        corpo_erro = e.read().decode("utf-8", errors="replace")
        return {
            "sucesso": False,
            "resposta": "",
            "erro": f"HTTP {e.code}: {corpo_erro[:300]}",
            "modelo": SLM_MODEL,
            "tokens": {}
        }
    except urllib.error.URLError as e:
        return {
            "sucesso": False,
            "resposta": "",
            "erro": f"Erro de ligação: {str(e.reason)}",
            "modelo": SLM_MODEL,
            "tokens": {}
        }
    except Exception as e:
        return {
            "sucesso": False,
            "resposta": "",
            "erro": f"Erro inesperado: {str(e)}",
            "modelo": SLM_MODEL,
            "tokens": {}
        }


def processar_chunks_slm(chunks: list, prompts: list) -> list:
    """
    Envia cada chunk ao SLM e recolhe as respostas.

    Retorna lista de dicts com:
        'chunk_id', 'texto_original', 'prompt', 'texto_normalizado',
        'sucesso', 'erro', 'tokens'
    """
    resultados = []
    for i, (chunk, prompt) in enumerate(zip(chunks, prompts)):
        resultado_api = enviar_para_slm(prompt)
        resultados.append({
            "chunk_id": i + 1,
            "texto_original": chunk,
            "prompt": prompt,
            "texto_normalizado": resultado_api["resposta"],
            "sucesso": resultado_api["sucesso"],
            "erro": resultado_api["erro"],
            "tokens": resultado_api["tokens"],
            "modelo": resultado_api["modelo"]
        })
    return resultados


# =============================================================================
# ETAPA 5 – CRIAÇÃO DE RELATÓRIOS AUTOMÁTICOS
# =============================================================================

def calcular_metricas_normalizacao(texto_antes: str, texto_depois: str) -> dict:
    """
    Calcula métricas de avaliação da normalização comparando
    o texto antes e depois do processamento.
    """
    palavras_antes  = len(texto_antes.split())
    palavras_depois = len(texto_depois.split()) if texto_depois else 0
    chars_antes     = len(texto_antes)
    chars_depois    = len(texto_depois) if texto_depois else 0

    # Similaridade de sequências (0 a 1)
    similaridade = difflib.SequenceMatcher(
        None, texto_antes[:2000], texto_depois[:2000]
    ).ratio() if texto_depois else 0

    reducao_chars = round((1 - chars_depois / chars_antes) * 100, 1) if chars_antes else 0

    return {
        "palavras_antes":   palavras_antes,
        "palavras_depois":  palavras_depois,
        "chars_antes":      chars_antes,
        "chars_depois":     chars_depois,
        "similaridade_pct": round(similaridade * 100, 1),
        "reducao_chars_pct": reducao_chars,
    }


def gerar_relatorio_html(dados: dict) -> str:
    """
    Gera um relatório completo em HTML com:
    - Parâmetros da pipeline
    - Texto antes/depois
    - Avaliação da normalização
    """
    agora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    params = dados.get("parametros_pipeline", {})
    metricas = dados.get("metricas", {})
    resultados_slm = dados.get("resultados_slm", [])
    texto_bruto = dados.get("texto_bruto", "")
    texto_limpo = dados.get("texto_limpo", "")
    idioma = dados.get("idioma", "desconhecido")

    # Constrói o texto final normalizado (todos os chunks juntos)
    texto_normalizado = "\n\n".join(
        r["texto_normalizado"] for r in resultados_slm if r.get("sucesso")
    )

    chunks_html = ""
    for r in resultados_slm:
        estado_cor = "#27ae60" if r["sucesso"] else "#e74c3c"
        estado_txt = "✓ Sucesso" if r["sucesso"] else f"✗ Erro: {r.get('erro','')}"
        tokens_info = ""
        if r.get("tokens"):
            tk = r["tokens"]
            tokens_info = f"<small>Tokens: prompt={tk.get('prompt_tokens','?')} | completion={tk.get('completion_tokens','?')}</small>"
        chunks_html += f"""
        <div class="chunk-block">
            <div class="chunk-header">
                <span>Chunk {r['chunk_id']}</span>
                <span style="color:{estado_cor}">{estado_txt}</span>
                {tokens_info}
            </div>
            <div class="two-col">
                <div>
                    <label>Texto Original</label>
                    <pre>{r['texto_original'][:600]}{'...' if len(r['texto_original'])>600 else ''}</pre>
                </div>
                <div>
                    <label>Texto Normalizado</label>
                    <pre>{r['texto_normalizado'][:600] if r['texto_normalizado'] else '(sem resposta)'}{'...' if len(r.get('texto_normalizado',''))>600 else ''}</pre>
                </div>
            </div>
        </div>"""

    html_output = f"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<title>Relatório NormText</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #fdf2f8; color: #4a235a; padding: 2rem; }}
  h1 {{ color: #d63384; font-size: 1.8rem; margin-bottom: .3rem; }}
  h2 {{ color: #a0527a; font-size: 1.1rem; margin: 2rem 0 .8rem; border-bottom: 2px solid #f4a7c3; padding-bottom: .4rem; }}
  .meta {{ color: #888; font-size: .85rem; margin-bottom: 2rem; }}
  .section {{ background: #fff; border: 1.5px solid #f4a7c3; border-radius: 14px; padding: 1.2rem; margin-bottom: 1.5rem; }}
  .kv-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: .6rem; }}
  .kv {{ background: #fce4ec; border-radius: 10px; padding: .5rem .8rem; }}
  .kv strong {{ display: block; font-size: .72rem; color: #a0527a; text-transform: uppercase; }}
  .kv span {{ font-size: 1rem; font-weight: 700; color: #d63384; }}
  pre {{ white-space: pre-wrap; word-break: break-word; font-size: .75rem;
         background: #fff0f6; border: 1px solid #f4a7c3; border-radius: 8px;
         padding: .7rem; max-height: 220px; overflow-y: auto; margin-top: .4rem; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: .5rem; }}
  label {{ font-size: .78rem; color: #a0527a; font-weight: 600; }}
  .chunk-block {{ border: 1px solid #f4a7c3; border-radius: 10px; padding: .8rem; margin: .6rem 0; background: #fffafe; }}
  .chunk-header {{ display: flex; justify-content: space-between; align-items: center;
                   font-weight: 700; font-size: .82rem; margin-bottom: .4rem; }}
  .tag {{ display: inline-block; background: #fce4ec; border: 1px solid #f4a7c3;
          border-radius: 20px; padding: .15rem .7rem; font-size: .72rem;
          color: #a0527a; margin: .1rem; font-weight: 600; }}
  .ok {{ color: #27ae60 !important; }} .fail {{ color: #e74c3c !important; }}
  @media print {{ body {{ background: white; }} .section {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<h1>🌸 Relatório de Normalização – NormText</h1>
<p class="meta">Gerado em {agora} &nbsp;|&nbsp; Modelo: {SLM_MODEL}</p>

<h2>1. Parâmetros da Pipeline</h2>
<div class="section">
  <div class="kv-grid">
    <div class="kv"><strong>Idioma detetado</strong><span>{idioma.upper()}</span></div>
    <div class="kv"><strong>Nº de chunks</strong><span>{dados.get('num_chunks', 0)}</span></div>
    <div class="kv"><strong>Tamanho chunk (palavras)</strong><span>{params.get('tamanho_chunk', '—')}</span></div>
    <div class="kv"><strong>Remover artefactos</strong><span>{'✓' if params.get('remover_artefactos') else '✗'}</span></div>
    <div class="kv"><strong>Reconstruir parágrafos</strong><span>{'✓' if params.get('reconstruir_paragrafos') else '✗'}</span></div>
    <div class="kv"><strong>Remover cabeçalhos</strong><span>{'✓' if params.get('remover_cabecalhos_rodapes') else '✗'}</span></div>
    <div class="kv"><strong>Normalizar espaços</strong><span>{'✓' if params.get('normalizar_espacos') else '✗'}</span></div>
    <div class="kv"><strong>Modelo SLM</strong><span style="font-size:.75rem">{SLM_MODEL}</span></div>
  </div>
</div>

<h2>2. Texto Antes e Depois da Limpeza</h2>
<div class="section">
  <div class="two-col">
    <div>
      <label>Texto Bruto (extraído)</label>
      <pre>{texto_bruto[:1200]}{'...' if len(texto_bruto)>1200 else ''}</pre>
    </div>
    <div>
      <label>Texto Limpo (após pipeline)</label>
      <pre>{texto_limpo[:1200]}{'...' if len(texto_limpo)>1200 else ''}</pre>
    </div>
  </div>
</div>

<h2>3. Avaliação da Normalização</h2>
<div class="section">
  <div class="kv-grid">
    <div class="kv"><strong>Palavras (antes)</strong><span>{metricas.get('palavras_antes','—')}</span></div>
    <div class="kv"><strong>Palavras (depois)</strong><span>{metricas.get('palavras_depois','—')}</span></div>
    <div class="kv"><strong>Caracteres (antes)</strong><span>{metricas.get('chars_antes','—')}</span></div>
    <div class="kv"><strong>Caracteres (depois)</strong><span>{metricas.get('chars_depois','—')}</span></div>
    <div class="kv"><strong>Similaridade</strong><span>{metricas.get('similaridade_pct','—')}%</span></div>
    <div class="kv"><strong>Redução de caracteres</strong><span>{metricas.get('reducao_chars_pct','—')}%</span></div>
  </div>
  <div style="margin-top:1rem">
    <label>Texto Final Normalizado (pelo SLM)</label>
    <pre>{texto_normalizado[:1500] if texto_normalizado else '(sem resposta do SLM)'}{'...' if len(texto_normalizado)>1500 else ''}</pre>
  </div>
</div>

<h2>4. Detalhe por Chunk</h2>
<div class="section">
  {chunks_html if chunks_html else '<p style="color:#aaa">Nenhum chunk processado.</p>'}
</div>

</body>
</html>"""
    return html_output


def gerar_relatorio_pdf(dados: dict) -> bytes:
    """
    Gera o relatório em PDF usando WeasyPrint (se disponível)
    ou fallback para reportlab.
    Retorna bytes do PDF.
    """
    html_content = gerar_relatorio_html(dados)

    # Tenta WeasyPrint primeiro
    try:
        from weasyprint import HTML as WeasyprintHTML
        pdf_bytes = WeasyprintHTML(string=html_content).write_pdf()
        return pdf_bytes
    except ImportError:
        pass

    # Fallback: reportlab (relatório simplificado em texto)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.units import cm

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4,
                                topMargin=2*cm, bottomMargin=2*cm,
                                leftMargin=2*cm, rightMargin=2*cm)
        styles = getSampleStyleSheet()
        story = []

        agora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        params = dados.get("parametros_pipeline", {})
        metricas = dados.get("metricas", {})
        resultados_slm = dados.get("resultados_slm", [])

        titulo_style = ParagraphStyle('titulo', parent=styles['Title'],
                                      fontSize=18, textColor=colors.HexColor('#d63384'))
        h2_style = ParagraphStyle('h2', parent=styles['Heading2'],
                                  fontSize=12, textColor=colors.HexColor('#a0527a'))
        normal = styles['Normal']

        story.append(Paragraph("Relatório de Normalização – NormText", titulo_style))
        story.append(Paragraph(f"Gerado em {agora} | Modelo: {SLM_MODEL}", normal))
        story.append(Spacer(1, 0.5*cm))

        # Parâmetros
        story.append(Paragraph("1. Parâmetros da Pipeline", h2_style))
        p_data = [
            ["Parâmetro", "Valor"],
            ["Idioma detetado", dados.get("idioma", "—").upper()],
            ["Nº de chunks", str(dados.get("num_chunks", 0))],
            ["Tamanho chunk (palavras)", str(params.get("tamanho_chunk", "—"))],
            ["Remover artefactos", "Sim" if params.get("remover_artefactos") else "Não"],
            ["Reconstruir parágrafos", "Sim" if params.get("reconstruir_paragrafos") else "Não"],
            ["Remover cabeçalhos/rodapés", "Sim" if params.get("remover_cabecalhos_rodapes") else "Não"],
            ["Normalizar espaços", "Sim" if params.get("normalizar_espacos") else "Não"],
            ["Modelo SLM", SLM_MODEL],
        ]
        t = Table(p_data, colWidths=[9*cm, 8*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f4a7c3')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#6b3a52')),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#fff0f6'), colors.white]),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#f4a7c3')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('PADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4*cm))

        # Métricas
        story.append(Paragraph("2. Avaliação da Normalização", h2_style))
        m_data = [
            ["Métrica", "Valor"],
            ["Palavras antes", str(metricas.get("palavras_antes", "—"))],
            ["Palavras depois (normalizado)", str(metricas.get("palavras_depois", "—"))],
            ["Caracteres antes", str(metricas.get("chars_antes", "—"))],
            ["Caracteres depois", str(metricas.get("chars_depois", "—"))],
            ["Similaridade (%)", str(metricas.get("similaridade_pct", "—")) + "%"],
            ["Redução de caracteres (%)", str(metricas.get("reducao_chars_pct", "—")) + "%"],
        ]
        t2 = Table(m_data, colWidths=[9*cm, 8*cm])
        t2.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f4a7c3')),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#fff0f6'), colors.white]),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#f4a7c3')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('PADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(t2)
        story.append(Spacer(1, 0.4*cm))

        # Chunks
        story.append(Paragraph("3. Detalhe por Chunk", h2_style))
        for r in resultados_slm:
            estado = "Sucesso" if r["sucesso"] else f"Erro: {r.get('erro','')}"
            story.append(Paragraph(f"<b>Chunk {r['chunk_id']}</b> – {estado}", normal))
            orig = r['texto_original'][:400].replace('&','&amp;').replace('<','&lt;')
            norm = (r['texto_normalizado'] or '(sem resposta)')[:400].replace('&','&amp;').replace('<','&lt;')
            story.append(Paragraph(f"<i>Original:</i> {orig}", normal))
            story.append(Paragraph(f"<i>Normalizado:</i> {norm}", normal))
            story.append(Spacer(1, 0.3*cm))

        doc.build(story)
        return buffer.getvalue()

    except ImportError:
        raise RuntimeError(
            "Nenhuma biblioteca de PDF disponível. "
            "Instala weasyprint ou reportlab: pip install reportlab"
        )


# =============================================================================
# INTERFACE WEB (Flask)
# Rota principal + endpoints para cada etapa
# =============================================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NormText – Pipeline de Pré-Processamento</title>
    <link href="https://fonts.googleapis.com/css2?family=Quicksand:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Quicksand', sans-serif;
            background: linear-gradient(135deg, #ffe4f0 0%, #ffd6e8 50%, #f9c6e0 100%);
            background-attachment: fixed;
            color: #6b3a52;
            min-height: 100vh;
            padding: 2rem;
        }
        body::before {
            content: '🌸 ✨ 💕 🌷 ✨ 🌸 💕 🌷 ✨ 🌸 💕 🌷 ✨ 🌸 💕 🌷 ✨ 🌸';
            display: block;
            font-size: 0.8rem;
            text-align: center;
            margin-bottom: 1rem;
            opacity: 0.5;
            letter-spacing: 4px;
        }
        header {
            border-bottom: 2px dashed #f4a7c3;
            padding-bottom: 1rem;
            margin-bottom: 2rem;
            text-align: center;
        }
        header h1 {
            font-size: 2rem;
            color: #d63384;
            letter-spacing: 3px;
            font-weight: 700;
            text-shadow: 2px 2px 0px #f9c6e0;
        }
        header h1::before { content: '🌸 '; }
        header h1::after  { content: ' 🌸'; }
        header p  { font-size: 0.85rem; color: #c06090; margin-top: 0.4rem; font-weight: 500; }

        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
        @media(max-width:900px){ .grid { grid-template-columns: 1fr; } }

        .card {
            background: rgba(255, 255, 255, 0.75);
            border: 2px solid #f4a7c3;
            border-radius: 20px;
            padding: 1.4rem;
            box-shadow: 0 4px 20px rgba(214, 51, 132, 0.1);
            backdrop-filter: blur(6px);
        }
        .card h2 {
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: #d63384;
            margin-bottom: 1rem;
            border-bottom: 1.5px dashed #f4a7c3;
            padding-bottom: 0.5rem;
            font-weight: 700;
        }
        label { font-size: 0.82rem; color: #a0527a; display: block; margin: 0.6rem 0 0.2rem; font-weight: 600; }
        input[type="file"], input[type="number"] {
            width: 100%;
            background: #fff0f6;
            border: 1.5px solid #f4a7c3;
            color: #6b3a52;
            padding: 0.5rem 0.8rem;
            border-radius: 12px;
            font-family: inherit;
            font-size: 0.82rem;
        }
        input[type="file"]:focus, input[type="number"]:focus {
            outline: none;
            border-color: #d63384;
        }
        .checkbox-group label {
            display: flex; align-items: center; gap: 0.5rem;
            color: #6b3a52; cursor: pointer; margin: 0.35rem 0; font-weight: 500;
        }
        input[type="checkbox"] { accent-color: #d63384; width: 15px; height: 15px; }

        button {
            margin-top: 1rem;
            width: 100%;
            padding: 0.65rem;
            background: linear-gradient(135deg, #f06292, #d63384);
            color: #fff;
            border: none;
            border-radius: 14px;
            font-family: inherit;
            font-size: 0.88rem;
            font-weight: 700;
            cursor: pointer;
            transition: all .2s;
            letter-spacing: 0.5px;
            box-shadow: 0 3px 10px rgba(214, 51, 132, 0.3);
        }
        button:hover {
            background: linear-gradient(135deg, #e91e8c, #c2185b);
            transform: translateY(-1px);
            box-shadow: 0 5px 15px rgba(214, 51, 132, 0.4);
        }
        button:disabled {
            background: #f0d0de;
            color: #c9a0b8;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }

        textarea {
            width: 100%;
            min-height: 180px;
            background: #fff0f6;
            border: 1.5px solid #f4a7c3;
            color: #6b3a52;
            padding: 0.7rem;
            border-radius: 12px;
            font-family: inherit;
            font-size: 0.75rem;
            resize: vertical;
            margin-top: 0.5rem;
            line-height: 1.5;
        }
        textarea:focus { outline: none; border-color: #d63384; }

        .tag {
            display: inline-block;
            background: #fce4ec;
            border: 1px solid #f4a7c3;
            border-radius: 20px;
            padding: 0.15rem 0.7rem;
            font-size: 0.72rem;
            color: #a0527a;
            margin: 0.1rem;
            font-weight: 600;
        }
        .tag.green { background: #fce4ec; border-color: #d63384; color: #d63384; }
        .tag.blue  { background: #f8e8f0; border-color: #c06090; color: #c06090; }

        #status {
            margin-top: 1rem;
            padding: 0.6rem 1rem;
            border-radius: 12px;
            font-size: 0.82rem;
            font-weight: 600;
            display: none;
        }
        .status-ok    { background: #fce4ec; border: 1.5px solid #d63384; color: #c2185b; display:block!important; }
        .status-error { background: #fdecea; border: 1.5px solid #e57373; color: #c62828; display:block!important; }
        .status-info  { background: #fdf0f6; border: 1.5px solid #f4a7c3; color: #a0527a; display:block!important; }

        .chunk-list {
            max-height: 300px;
            overflow-y: auto;
            margin-top: 0.5rem;
        }
        .chunk-list::-webkit-scrollbar { width: 6px; }
        .chunk-list::-webkit-scrollbar-thumb { background: #f4a7c3; border-radius: 10px; }
        .chunk-item {
            background: #fff0f6;
            border: 1px solid #f4a7c3;
            border-radius: 12px;
            padding: 0.6rem 0.8rem;
            margin: 0.4rem 0;
            font-size: 0.72rem;
            line-height: 1.5;
        }
        .chunk-item strong { color: #d63384; }

        .full-width { grid-column: 1 / -1; }
    </style>
</head>
<body>
<header>
    <h1>⚙ NormText</h1>
    <p>Pipeline de Pré-Processamento de Texto | Laboratório de Programação – 1º Ano</p>
</header>

<div class="grid">

    <!-- ETAPA 1 -->
    <div class="card">
        <h2>Etapa 1 – Extração de Texto</h2>
        <label>Seleciona um ficheiro (PDF, DOCX, TXT)</label>
        <input type="file" id="ficheiro" accept=".pdf,.docx,.txt">
        <button id="btn-extrair" onclick="extrairTexto()">Extrair Texto</button>
        <label>Texto extraído (bruto):</label>
        <textarea id="texto-bruto" readonly placeholder="O texto extraído aparecerá aqui..."></textarea>
        <div id="info-extracao" style="margin-top:.5rem"></div>
    </div>

    <!-- ETAPA 2 -->
    <div class="card">
        <h2>Etapa 2 – Limpeza e Pré-Processamento</h2>
        <label>Opções da pipeline:</label>
        <div class="checkbox-group">
            <label><input type="checkbox" id="opt-artefactos" checked> Remover artefactos</label>
            <label><input type="checkbox" id="opt-cabecalhos" checked> Remover cabeçalhos/rodapés repetidos</label>
            <label><input type="checkbox" id="opt-paragrafos" checked> Reconstruir parágrafos</label>
            <label><input type="checkbox" id="opt-espacos" checked> Normalizar espaços e pontuação</label>
        </div>
        <button id="btn-limpar" onclick="limparTexto()" disabled>Limpar Texto</button>
        <label>Texto depois da limpeza:</label>
        <textarea id="texto-limpo" readonly placeholder="O texto limpo aparecerá aqui..."></textarea>
    </div>

    <!-- ETAPA 3 -->
    <div class="card full-width">
        <h2>Etapa 3 – Preparação do Input para Normalização</h2>
        <label>Tamanho do chunk (palavras):</label>
        <input type="number" id="chunk-size" value="800" min="100" max="3000">
        <button id="btn-preparar" onclick="prepararInput()" disabled>Preparar Input</button>

        <div id="info-preparacao" style="margin-top:1rem"></div>
        <div class="chunk-list" id="lista-chunks"></div>
    </div>

    <!-- ETAPA 4 -->
    <div class="card full-width">
        <h2>Etapa 4 – Envio ao SLM (llama-3.2-1b-instruct)</h2>
        <p style="font-size:.82rem;color:#a0527a;margin-bottom:.8rem">
            Envia cada chunk à API do SLM e recebe o texto normalizado.
            Este passo pode demorar consoante o número de chunks.
        </p>
        <button id="btn-slm" onclick="enviarSLM()" disabled>🚀 Enviar ao SLM</button>
        <div id="slm-progresso" style="margin-top:.8rem"></div>
        <div class="chunk-list" id="slm-resultados"></div>
    </div>

    <!-- ETAPA 5 -->
    <div class="card full-width">
        <h2>Etapa 5 – Relatório Automático</h2>
        <p style="font-size:.82rem;color:#a0527a;margin-bottom:.8rem">
            Gera um relatório com os parâmetros da pipeline, texto antes/depois e avaliação da normalização.
        </p>
        <div style="display:flex;gap:1rem;">
            <button id="btn-html" onclick="exportarRelatorio('html')" disabled style="flex:1">📄 Exportar HTML</button>
            <button id="btn-pdf" onclick="exportarRelatorio('pdf')" disabled style="flex:1">📑 Exportar PDF</button>
        </div>
        <div id="relatorio-info" style="margin-top:.8rem"></div>
    </div>

</div>

<div id="status"></div>

<script>
    let textoExtraido = "";
    let textoLimpo    = "";
    let dadosInput    = null;
    let resultadosSLM = null;

    function mostrarStatus(msg, tipo = "info") {
        const el = document.getElementById("status");
        el.textContent = msg;
        el.className = "status-" + tipo;
    }

    async function extrairTexto() {
        const ficheiro = document.getElementById("ficheiro").files[0];
        if (!ficheiro) { mostrarStatus("Seleciona um ficheiro primeiro.", "error"); return; }

        mostrarStatus("A extrair texto...", "info");
        const form = new FormData();
        form.append("ficheiro", ficheiro);

        try {
            const resp = await fetch("/extrair", { method: "POST", body: form });
            const data = await resp.json();
            if (data.erro) { mostrarStatus(data.erro, "error"); return; }

            textoExtraido = data.texto;
            document.getElementById("texto-bruto").value = textoExtraido;
            document.getElementById("btn-limpar").disabled = false;

            const info = document.getElementById("info-extracao");
            info.innerHTML = `
                <span class="tag blue">Formato: ${data.formato}</span>
                <span class="tag">Caracteres: ${textoExtraido.length}</span>
                <span class="tag">Palavras: ${textoExtraido.split(/\\s+/).length}</span>`;

            mostrarStatus("✓ Texto extraído com sucesso!", "ok");
        } catch(e) { mostrarStatus("Erro na extração: " + e, "error"); }
    }

    async function limparTexto() {
        if (!textoExtraido) { mostrarStatus("Extrai o texto primeiro.", "error"); return; }

        const opcoes = {
            remover_artefactos:       document.getElementById("opt-artefactos").checked,
            remover_cabecalhos_rodapes: document.getElementById("opt-cabecalhos").checked,
            reconstruir_paragrafos:   document.getElementById("opt-paragrafos").checked,
            normalizar_espacos:       document.getElementById("opt-espacos").checked,
        };

        mostrarStatus("A limpar texto...", "info");
        try {
            const resp = await fetch("/limpar", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ texto: textoExtraido, opcoes })
            });
            const data = await resp.json();
            if (data.erro) { mostrarStatus(data.erro, "error"); return; }

            textoLimpo = data.texto_limpo;
            document.getElementById("texto-limpo").value = textoLimpo;
            document.getElementById("btn-preparar").disabled = false;
            mostrarStatus("✓ Limpeza concluída!", "ok");
        } catch(e) { mostrarStatus("Erro na limpeza: " + e, "error"); }
    }

    async function prepararInput() {
        if (!textoLimpo) { mostrarStatus("Limpa o texto primeiro.", "error"); return; }

        const tamanho = parseInt(document.getElementById("chunk-size").value) || 800;
        mostrarStatus("A preparar input...", "info");

        try {
            const resp = await fetch("/preparar", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ texto: textoLimpo, tamanho_chunk: tamanho })
            });
            const data = await resp.json();
            if (data.erro) { mostrarStatus(data.erro, "error"); return; }

            dadosInput = data;

            document.getElementById("info-preparacao").innerHTML = `
                <span class="tag blue">Idioma: ${data.idioma}</span>
                <span class="tag green">Chunks: ${data.num_chunks}</span>`;

            const lista = document.getElementById("lista-chunks");
            lista.innerHTML = "";
            data.chunks.forEach((chunk, i) => {
                const div = document.createElement("div");
                div.className = "chunk-item";
                div.innerHTML = `<strong>Chunk ${i+1}/${data.num_chunks}</strong> (${chunk.split(" ").length} palavras)<br><br>${chunk.substring(0, 200)}${chunk.length > 200 ? "..." : ""}`;
                lista.appendChild(div);
            });

            document.getElementById("btn-slm").disabled = false;
            mostrarStatus("✓ Input preparado! Pronto para enviar ao SLM.", "ok");
        } catch(e) { mostrarStatus("Erro na preparação: " + e, "error"); }
    }

    async function enviarSLM() {
        if (!dadosInput) { mostrarStatus("Prepara o input primeiro.", "error"); return; }

        document.getElementById("btn-slm").disabled = true;
        const progresso = document.getElementById("slm-progresso");
        const resultadosDiv = document.getElementById("slm-resultados");
        resultadosDiv.innerHTML = "";
        progresso.innerHTML = `<span class="tag">A enviar ${dadosInput.num_chunks} chunk(s) ao SLM...</span>`;
        mostrarStatus("A comunicar com o SLM... (pode demorar)", "info");

        try {
            const resp = await fetch("/slm", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    chunks: dadosInput.chunks,
                    prompts: dadosInput.prompts
                })
            });
            const data = await resp.json();
            if (data.erro) { mostrarStatus(data.erro, "error"); return; }

            resultadosSLM = data.resultados;
            progresso.innerHTML = `
                <span class="tag green">✓ ${data.chunks_ok} chunk(s) normalizados</span>
                ${data.chunks_erro > 0 ? `<span class="tag" style="color:#e74c3c">✗ ${data.chunks_erro} erro(s)</span>` : ''}`;

            resultadosSLM.forEach(r => {
                const div = document.createElement("div");
                div.className = "chunk-item";
                const cor = r.sucesso ? "#27ae60" : "#e74c3c";
                const estado = r.sucesso ? "✓ OK" : `✗ Erro: ${r.erro || ''}`;
                div.innerHTML = `
                    <strong style="color:${cor}">Chunk ${r.chunk_id} – ${estado}</strong><br><br>
                    <small><b>Original:</b></small><br>${(r.texto_original||'').substring(0,250)}...<br><br>
                    <small><b>Normalizado:</b></small><br>${(r.texto_normalizado||'(sem resposta)').substring(0,250)}`;
                resultadosDiv.appendChild(div);
            });

            document.getElementById("btn-html").disabled = false;
            document.getElementById("btn-pdf").disabled = false;
            mostrarStatus("✓ Normalização concluída! Podes exportar o relatório.", "ok");
        } catch(e) {
            mostrarStatus("Erro na comunicação com o SLM: " + e, "error");
            document.getElementById("btn-slm").disabled = false;
        }
    }

    async function exportarRelatorio(formato) {
        if (!resultadosSLM) { mostrarStatus("Processa o texto no SLM primeiro.", "error"); return; }

        const tamanho = parseInt(document.getElementById("chunk-size").value) || 800;
        const opcoes = {
            remover_artefactos:         document.getElementById("opt-artefactos").checked,
            remover_cabecalhos_rodapes:  document.getElementById("opt-cabecalhos").checked,
            reconstruir_paragrafos:     document.getElementById("opt-paragrafos").checked,
            normalizar_espacos:         document.getElementById("opt-espacos").checked,
        };

        const infoDiv = document.getElementById("relatorio-info");
        infoDiv.innerHTML = `<span class="tag">A gerar relatório ${formato.toUpperCase()}...</span>`;

        try {
            const resp = await fetch("/relatorio/" + formato, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    texto_bruto:      textoExtraido,
                    texto_limpo:      textoLimpo,
                    resultados_slm:   resultadosSLM,
                    idioma:           dadosInput.idioma,
                    num_chunks:       dadosInput.num_chunks,
                    parametros_pipeline: { tamanho_chunk: tamanho, ...opcoes }
                })
            });

            if (!resp.ok) {
                const err = await resp.json();
                infoDiv.innerHTML = `<span style="color:#e74c3c">Erro: ${err.erro}</span>`;
                return;
            }

            const blob = await resp.blob();
            const url  = URL.createObjectURL(blob);
            const a    = document.createElement("a");
            a.href     = url;
            a.download = `relatorio_normtext.${formato}`;
            a.click();
            URL.revokeObjectURL(url);

            infoDiv.innerHTML = `<span class="tag green">✓ Relatório ${formato.toUpperCase()} exportado!</span>`;
            mostrarStatus(`✓ Relatório ${formato.toUpperCase()} descarregado com sucesso!`, "ok");
        } catch(e) {
            infoDiv.innerHTML = `<span style="color:#e74c3c">Erro: ${e}</span>`;
        }
    }
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/extrair", methods=["POST"])
def rota_extrair():
    """Endpoint da Etapa 1 – recebe ficheiro, devolve texto bruto."""
    if "ficheiro" not in request.files:
        return jsonify({"erro": "Nenhum ficheiro enviado."}), 400

    ficheiro = request.files["ficheiro"]
    nome = ficheiro.filename
    dados = ficheiro.read()

    try:
        resultado = extrair_texto(dados, nome)
        return jsonify(resultado)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        return jsonify({"erro": f"Erro inesperado: {str(e)}"}), 500


@app.route("/limpar", methods=["POST"])
def rota_limpar():
    """Endpoint da Etapa 2 – recebe texto bruto + opções, devolve texto limpo."""
    body = request.get_json()
    if not body or "texto" not in body:
        return jsonify({"erro": "Campo 'texto' em falta."}), 400

    texto_limpo = limpar_texto(body["texto"], body.get("opcoes", {}))
    return jsonify({"texto_limpo": texto_limpo})


@app.route("/preparar", methods=["POST"])
def rota_preparar():
    """Endpoint da Etapa 3 – recebe texto limpo, devolve chunks + prompts."""
    body = request.get_json()
    if not body or "texto" not in body:
        return jsonify({"erro": "Campo 'texto' em falta."}), 400

    tamanho = body.get("tamanho_chunk", 800)
    resultado = preparar_input(body["texto"], tamanho_chunk=tamanho)
    return jsonify(resultado)


@app.route("/slm", methods=["POST"])
def rota_slm():
    """
    Endpoint da Etapa 4 – recebe chunks e prompts, envia ao SLM e devolve resultados.
    Body: { "chunks": [...], "prompts": [...] }
    """
    body = request.get_json()
    if not body or "chunks" not in body or "prompts" not in body:
        return jsonify({"erro": "Campos 'chunks' e 'prompts' em falta."}), 400

    chunks  = body["chunks"]
    prompts = body["prompts"]

    if len(chunks) != len(prompts):
        return jsonify({"erro": "Número de chunks e prompts não coincide."}), 400

    resultados = processar_chunks_slm(chunks, prompts)
    chunks_ok   = sum(1 for r in resultados if r["sucesso"])
    chunks_erro = sum(1 for r in resultados if not r["sucesso"])

    return jsonify({
        "resultados":  resultados,
        "chunks_ok":   chunks_ok,
        "chunks_erro": chunks_erro,
        "total":       len(resultados)
    })


@app.route("/relatorio/html", methods=["POST"])
def rota_relatorio_html():
    """Endpoint da Etapa 5 – gera e devolve relatório em HTML."""
    body = request.get_json()
    if not body:
        return jsonify({"erro": "Body em falta."}), 400

    # Calcula métricas de normalização
    texto_bruto = body.get("texto_bruto", "")
    texto_normalizado = "\n\n".join(
        r["texto_normalizado"] for r in body.get("resultados_slm", [])
        if r.get("sucesso") and r.get("texto_normalizado")
    )
    body["metricas"] = calcular_metricas_normalizacao(texto_bruto, texto_normalizado)

    html_output = gerar_relatorio_html(body)
    return send_file(
        io.BytesIO(html_output.encode("utf-8")),
        mimetype="text/html",
        as_attachment=True,
        download_name="relatorio_normtext.html"
    )


@app.route("/relatorio/pdf", methods=["POST"])
def rota_relatorio_pdf():
    """Endpoint da Etapa 5 – gera e devolve relatório em PDF."""
    body = request.get_json()
    if not body:
        return jsonify({"erro": "Body em falta."}), 400

    texto_bruto = body.get("texto_bruto", "")
    texto_normalizado = "\n\n".join(
        r["texto_normalizado"] for r in body.get("resultados_slm", [])
        if r.get("sucesso") and r.get("texto_normalizado")
    )
    body["metricas"] = calcular_metricas_normalizacao(texto_bruto, texto_normalizado)

    try:
        pdf_bytes = gerar_relatorio_pdf(body)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="relatorio_normtext.pdf"
        )
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    print("=" * 60)
    print("  NormText – Pipeline de Pré-Processamento")
    print("  Acede em: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)