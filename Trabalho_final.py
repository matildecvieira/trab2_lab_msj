"""
=============================================================================
LABORATÓRIO DE PROGRAMAÇÃO – 1º ANO | ENGENHARIA INFORMÁTICA
Normalização de Texto com Pipeline de Pré-Processamento e SLMs
=============================================================================
ESTRUTURA DO FICHEIRO:
  - ETAPA 1: Extração de Texto Multi-formato (linha ~60)
  - ETAPA 2: Pipeline de Limpeza e Pré-Processamento (linha ~145)
  - ETAPA 3: Preparação do Input para Normalização (linha ~255)
  - ETAPA 4: Conexão à API do SLM (linha ~330)
  - ETAPA 5: Criação de Relatórios Automáticos (linha ~440)
  - INTERFACE WEB (Flask) (linha ~600)
=============================================================================
DEPENDÊNCIAS:
  pip install flask pymupdf python-docx langdetect
=============================================================================
MELHORIAS APLICADAS:
  [BUG 1] segmentar_texto: loop infinito quando sobreposicao >= tamanho_chunk
           → Adicionada guarda e valor mínimo de avanço (max(1, ...))
  [BUG 2] remover_cabecalhos_rodapes: min_repeticoes=2 demasiado agressivo;
           removia texto legítimo repetido → aumentado para 3
  [BUG 3] reconstruir_paragrafos: regex de junção de linhas não cobria todos
           os caracteres Unicode minúsculos → substituído por re.UNICODE + .islower()
  [BUG 4] normalizar_espacos: classe de caracteres [A-Z...A-Z] duplicada → corrigida
  [MELHORIA 1] enviar_para_slm: retry automático (até 3 tentativas) em falhas
               temporárias de rede (HTTPError 5xx, URLError)
  [MELHORIA 2] Validação de tamanho_chunk no endpoint /preparar (mín 50, máx 4000)
  [MELHORIA 3] limpar_texto: regista os passos aplicados no resultado
  [MELHORIA 4] preparar_input: retorna também metadados de tamanho médio por chunk
  [MELHORIA 5] Interface Web: barra de progresso por chunk durante envio ao SLM,
               contador de tempo decorrido, botão de cancelamento (abort)
  [MELHORIA 6] Relatório HTML: adiciona diff visual (antes/depois) por chunk
=============================================================================
"""

import os
import re
import io
import time
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
        dict com 'texto' (str), 'formato' (str) e metadados básicos
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

    return {
        "texto": texto,
        "formato": formato,
        "num_chars": len(texto),
        "num_palavras": len(texto.split()),
    }


# =============================================================================
# ETAPA 2 – PIPELINE DE LIMPEZA E PRÉ-PROCESSAMENTO
# =============================================================================

def remover_artefactos(texto: str) -> str:
    """
    Remove caracteres inválidos, símbolos de controlo e artefactos de encoding
    (ex: caracteres de substituição \ufffd, caracteres nulos, etc.).
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
    Junta linhas incorretamente quebradas a meio de uma frase (típico de PDFs).
    Uma quebra é considerada incorreta se:
    - a linha anterior NÃO termina com pontuação final (. ! ? : ;)
    - a linha seguinte começa com letra minúscula (qualquer Unicode)

    [CORRIGIDO BUG 3]: A regex original usava uma classe de caracteres manual
    que não cobria todos os caracteres Unicode minúsculos. Substituída por
    re.match com flag re.UNICODE e .islower() para cobertura completa.
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
        # Junta com a linha seguinte se for uma quebra incorreta
        while (i + 1 < len(linhas)
               and linhas[i + 1].strip()
               and not re.search(r'[.!?:;]\s*$', linha_atual)
               # [FIX] usa \p via re com unicode; fallback: primeiro char minúsculo
               and len(linhas[i + 1].strip()) > 0
               and linhas[i + 1].strip()[0].islower()):
            i += 1
            linha_atual = linha_atual.rstrip() + ' ' + linhas[i].strip()
        resultado.append(linha_atual)
        i += 1
    return '\n'.join(resultado)


def remover_cabecalhos_rodapes(texto: str, min_repeticoes: int = 3) -> str:
    """
    Deteta e remove linhas que se repetem frequentemente no documento
    (cabeçalhos/rodapés típicos de PDFs multipágina).

    [CORRIGIDO BUG 2]: O valor original min_repeticoes=2 era demasiado agressivo
    e eliminava texto legítimo que aparecia apenas duas vezes no documento.
    Aumentado para 3 para maior segurança.
    Adicionada condição de comprimento mínimo mais restrito (>10 chars).
    """
    from collections import Counter
    linhas = texto.split('\n')
    contagem = Counter(l.strip() for l in linhas if l.strip())
    linhas_repetidas = {linha for linha, count in contagem.items()
                        if count >= min_repeticoes and len(linha) > 10}
    resultado = [l for l in linhas if l.strip() not in linhas_repetidas]
    return '\n'.join(resultado)


def normalizar_espacos_pontuacao(texto: str) -> str:
    """
    - Colapsa múltiplos espaços num único espaço
    - Remove espaços antes de pontuação
    - Garante espaço após pontuação final
    - Colapsa mais de 2 linhas em branco em 2

    [CORRIGIDO BUG 4]: A regex original tinha [A-ZÁÀÃÂÉÊÍÓÔÕÚA-Z] com A-Z
    duplicado. Corrigida para [A-ZÁÀÃÂÉÊÍÓÔÕÚ].
    """
    # Colapsa espaços múltiplos (exceto quebras de linha)
    texto = re.sub(r'[ \t]+', ' ', texto)
    # Remove espaço antes de pontuação
    texto = re.sub(r'\s+([.!?,;:])', r'\1', texto)
    # [FIX] Regex corrigida: classe de caracteres sem duplicação
    texto = re.sub(r'([.!?])([A-ZÁÀÃÂÉÊÍÓÔÕÚ])', r'\1 \2', texto)
    # Colapsa linhas em branco excessivas
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


def limpar_texto(texto: str, opcoes: dict = None) -> dict:
    """
    Ponto de entrada da Etapa 2.
    Aplica a pipeline de limpeza de forma configurável.

    [MELHORIA 3]: Agora retorna um dict com o texto limpo E os passos aplicados,
    o que permite ao relatório documentar exatamente o que foi feito.

    opcoes (dict):
        {
            "remover_artefactos": True,
            "reconstruir_paragrafos": True,
            "remover_cabecalhos_rodapes": True,
            "normalizar_espacos": True
        }
    """
    if opcoes is None:
        opcoes = {}

    passos_activos = {
        "remover_artefactos":        opcoes.get("remover_artefactos", True),
        "reconstruir_paragrafos":    opcoes.get("reconstruir_paragrafos", True),
        "remover_cabecalhos_rodapes": opcoes.get("remover_cabecalhos_rodapes", True),
        "normalizar_espacos":        opcoes.get("normalizar_espacos", True),
    }

    texto_processado = texto
    passos_executados = []

    if passos_activos["remover_artefactos"]:
        texto_processado = remover_artefactos(texto_processado)
        passos_executados.append("remover_artefactos")

    if passos_activos["remover_cabecalhos_rodapes"]:
        texto_processado = remover_cabecalhos_rodapes(texto_processado)
        passos_executados.append("remover_cabecalhos_rodapes")

    if passos_activos["reconstruir_paragrafos"]:
        texto_processado = reconstruir_paragrafos(texto_processado)
        passos_executados.append("reconstruir_paragrafos")

    if passos_activos["normalizar_espacos"]:
        texto_processado = normalizar_espacos_pontuacao(texto_processado)
        passos_executados.append("normalizar_espacos")

    return {
        "texto_limpo": texto_processado,
        "passos_executados": passos_executados,
        "chars_antes": len(texto),
        "chars_depois": len(texto_processado),
    }


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
        amostra = texto[:500].strip()
        if not amostra:
            return "en"
        return detect(amostra)
    except Exception:
        return "en"


def segmentar_texto(texto: str, tamanho_chunk: int = 200, sobreposicao: int = 50) -> list:
    """
    Divide o texto em blocos (chunks) de tamanho controlado, com sobreposição
    para preservar contexto entre blocos.

    [CORRIGIDO BUG 1]: O código original podia entrar em loop infinito se
    sobreposicao >= tamanho_chunk, pois o avanço seria 0 ou negativo.
    Corrigido com max(1, tamanho_chunk - sobreposicao) como passo mínimo.

    [MELHORIA 2]: tamanho_chunk é agora validado antes de chegar aqui
    (ver endpoint /preparar).
    """
    palavras = texto.split()
    if not palavras:
        return []

    # [FIX] Garante que o passo nunca é zero ou negativo → evita loop infinito
    passo = max(1, tamanho_chunk - sobreposicao)

    chunks = []
    inicio = 0
    while inicio < len(palavras):
        fim = inicio + tamanho_chunk
        chunk = ' '.join(palavras[inicio:fim])
        chunks.append(chunk)
        inicio += passo

    return chunks


def criar_prompt(chunk: str, idioma: str) -> str:
    """Cria o prompt adequado para o chunk, adaptado ao idioma detetado."""
    template = PROMPTS_POR_IDIOMA.get(idioma, PROMPT_PADRAO)
    return template + chunk


def preparar_input(texto_limpo: str, tamanho_chunk: int = 200) -> dict:
    """
    Ponto de entrada da Etapa 3.
    Segmenta o texto limpo e prepara os prompts para envio ao SLM.

    [MELHORIA 4]: Retorna também o tamanho médio dos chunks para o relatório.

    Retorna dict com: idioma, num_chunks, chunks, prompts, palavras_total,
                      media_palavras_por_chunk
    """
    idioma = detectar_idioma(texto_limpo)
    chunks = segmentar_texto(texto_limpo, tamanho_chunk=tamanho_chunk)
    prompts = [criar_prompt(chunk, idioma) for chunk in chunks]

    palavras_por_chunk = [len(c.split()) for c in chunks]
    media = round(sum(palavras_por_chunk) / len(palavras_por_chunk), 1) if chunks else 0

    return {
        "idioma": idioma,
        "num_chunks": len(chunks),
        "chunks": chunks,
        "prompts": prompts,
        "palavras_total": sum(palavras_por_chunk),
        "media_palavras_por_chunk": media,
    }


# =============================================================================
# ETAPA 4 – CONEXÃO À API DO SLM
# =============================================================================

SLM_API_URL = "https://reality.utad.net/slm"
SLM_MODEL   = "llama-3.2-1b-instruct"
MAX_TENTATIVAS = 3       # número máximo de tentativas por chunk
ESPERA_RETRY   = 2       # segundos de espera entre tentativas


def enviar_para_slm(prompt: str, timeout: int = 60) -> dict:
    """
    Envia um pedido HTTP POST à API do SLM com o prompt fornecido.

    [MELHORIA 1]: Adicionado retry automático (até MAX_TENTATIVAS tentativas)
    em caso de erros temporários (HTTPError 5xx ou URLError).
    Cada tentativa aguarda ESPERA_RETRY segundos antes de repetir.

    Retorna dict com: sucesso, resposta, erro, modelo, tokens, tentativas
    """
    payload = {
        "model": SLM_MODEL,
        "messages": [{"role": "user", "content": prompt}]
    }
    dados = json.dumps(payload).encode("utf-8")

    ultimo_erro = None

    for tentativa in range(1, MAX_TENTATIVAS + 1):
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

            print(f"[SLM] Tentativa {tentativa} OK | {json.dumps(resultado, ensure_ascii=False)[:200]}")

            # Extrai o texto — formato confirmado: choices[0].message.content
            texto_resposta = ""
            escolha = {}
            if "choices" in resultado and resultado["choices"]:
                escolha = resultado["choices"][0]
                if "message" in escolha and isinstance(escolha["message"], dict):
                    texto_resposta = escolha["message"].get("content") or ""
                elif "text" in escolha:
                    texto_resposta = escolha.get("text") or ""
                elif "content" in escolha:
                    texto_resposta = escolha.get("content") or ""
            elif "content" in resultado:
                conteudo_api = resultado["content"]
                if isinstance(conteudo_api, list):
                    texto_resposta = " ".join(
                        c.get("text", "") for c in conteudo_api if isinstance(c, dict)
                    )
                else:
                    texto_resposta = str(conteudo_api) if conteudo_api else ""
            elif "response" in resultado:
                texto_resposta = resultado.get("response") or ""
            elif "text" in resultado:
                texto_resposta = resultado.get("text") or ""
            elif "message" in resultado:
                msg = resultado["message"]
                texto_resposta = msg.get("content", "") if isinstance(msg, dict) else str(msg)

            texto_resposta = texto_resposta.strip()

            # Resposta vazia = chunk demasiado grande para o modelo (context_length: 4096 tokens)
            if not texto_resposta:
                finish = escolha.get("finish_reason", "?")
                print(f"[SLM] AVISO: resposta vazia! finish_reason={finish}")
                return {
                    "sucesso": False,
                    "resposta": "",
                    "erro": f"Modelo devolveu resposta vazia (finish_reason={finish}). Reduz o tamanho do chunk para 200-300 palavras.",
                    "modelo": SLM_MODEL,
                    "tokens": resultado.get("usage", {}),
                    "tentativas": tentativa,
                }

            return {
                "sucesso": True,
                "resposta": texto_resposta,
                "erro": None,
                "modelo": SLM_MODEL,
                "tokens": resultado.get("usage", {}),
                "tentativas": tentativa,
            }

        except urllib.error.HTTPError as e:
            corpo_erro = e.read().decode("utf-8", errors="replace")
            ultimo_erro = f"HTTP {e.code}: {corpo_erro[:300]}"
            # Só faz retry em erros de servidor (5xx); erros de cliente (4xx) são definitivos
            if e.code < 500:
                break
            print(f"[SLM] Tentativa {tentativa} falhou (HTTP {e.code}). A aguardar {ESPERA_RETRY}s...")
            time.sleep(ESPERA_RETRY)

        except urllib.error.URLError as e:
            ultimo_erro = f"Erro de ligação: {str(e.reason)}"
            print(f"[SLM] Tentativa {tentativa} falhou (URLError). A aguardar {ESPERA_RETRY}s...")
            time.sleep(ESPERA_RETRY)

        except Exception as e:
            ultimo_erro = f"Erro inesperado: {str(e)}"
            break  # erros inesperados não beneficiam de retry

    return {
        "sucesso": False,
        "resposta": "",
        "erro": ultimo_erro,
        "modelo": SLM_MODEL,
        "tokens": {},
        "tentativas": MAX_TENTATIVAS,
    }


def processar_chunks_slm(chunks: list, prompts: list) -> list:
    """
    Envia cada chunk ao SLM e recolhe as respostas.
    Retorna lista de dicts com resultados por chunk.
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
            "modelo": resultado_api["modelo"],
            "tentativas": resultado_api.get("tentativas", 1),
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

    similaridade = difflib.SequenceMatcher(
        None, texto_antes[:2000], texto_depois[:2000]
    ).ratio() if texto_depois else 0

    reducao_chars = round((1 - chars_depois / chars_antes) * 100, 1) if chars_antes else 0

    return {
        "palavras_antes":    palavras_antes,
        "palavras_depois":   palavras_depois,
        "chars_antes":       chars_antes,
        "chars_depois":      chars_depois,
        "similaridade_pct":  round(similaridade * 100, 1),
        "reducao_chars_pct": reducao_chars,
    }


def _gerar_diff_html(antes: str, depois: str, limite: int = 400) -> str:
    """
    [MELHORIA 6]: Gera um diff visual HTML mostrando o que mudou entre
    o texto original e o normalizado (usado no relatório por chunk).
    """
    antes_curto  = antes[:limite]
    depois_curto = depois[:limite] if depois else ""

    matcher = difflib.SequenceMatcher(None, antes_curto, depois_curto)
    resultado = []

    for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
        if opcode == 'equal':
            resultado.append(antes_curto[a0:a1].replace('<', '&lt;').replace('>', '&gt;'))
        elif opcode == 'replace':
            resultado.append(
                f'<del style="background:#ffd0d0;border-radius:3px">{antes_curto[a0:a1].replace("<","&lt;")}</del>'
                f'<ins style="background:#d0f0d0;border-radius:3px;text-decoration:none">{depois_curto[b0:b1].replace("<","&lt;")}</ins>'
            )
        elif opcode == 'delete':
            resultado.append(
                f'<del style="background:#ffd0d0;border-radius:3px">{antes_curto[a0:a1].replace("<","&lt;")}</del>'
            )
        elif opcode == 'insert':
            resultado.append(
                f'<ins style="background:#d0f0d0;border-radius:3px;text-decoration:none">{depois_curto[b0:b1].replace("<","&lt;")}</ins>'
            )

    return ''.join(resultado)


def gerar_relatorio_html(dados: dict) -> str:
    """
    Gera um relatório completo em HTML com:
    - Parâmetros da pipeline e passos executados
    - Texto antes/depois da limpeza
    - Avaliação da normalização
    - [MELHORIA 6] Diff visual por chunk
    """
    agora    = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    params   = dados.get("parametros_pipeline", {})
    metricas = dados.get("metricas", {})
    resultados_slm = dados.get("resultados_slm", [])
    texto_bruto  = dados.get("texto_bruto", "")
    texto_limpo  = dados.get("texto_limpo", "")
    idioma       = dados.get("idioma", "desconhecido")

    texto_normalizado = "\n\n".join(
        r["texto_normalizado"] for r in resultados_slm if r.get("sucesso")
    )

    chunks_html = ""
    for r in resultados_slm:
        estado_cor = "#27ae60" if r["sucesso"] else "#e74c3c"
        estado_txt = "✓ Sucesso" if r["sucesso"] else f"✗ Erro: {r.get('erro','')}"
        tentativas_txt = f" | {r.get('tentativas',1)} tentativa(s)" if r.get("tentativas",1) > 1 else ""
        tokens_info = ""
        if r.get("tokens"):
            tk = r["tokens"]
            tokens_info = f"<small>Tokens: prompt={tk.get('prompt_tokens','?')} | completion={tk.get('completion_tokens','?')}</small>"

        diff_html = ""
        if r["sucesso"] and r.get("texto_normalizado"):
            diff_html = f"""
            <div style="margin-top:.5rem">
                <label>Diff (vermelho=removido, verde=adicionado)</label>
                <pre style="white-space:pre-wrap;word-break:break-word;font-size:.72rem;
                            background:#fffafe;border:1px solid #f4a7c3;border-radius:8px;
                            padding:.6rem;max-height:180px;overflow-y:auto;margin-top:.3rem">
{_gerar_diff_html(r['texto_original'], r['texto_normalizado'])}{'...' if len(r['texto_original']) > 400 else ''}</pre>
            </div>"""

        chunks_html += f"""
        <div class="chunk-block">
            <div class="chunk-header">
                <span>Chunk {r['chunk_id']}</span>
                <span style="color:{estado_cor}">{estado_txt}{tentativas_txt}</span>
                {tokens_info}
            </div>
            <div class="two-col">
                <div>
                    <label>Texto Original</label>
                    <pre>{r['texto_original'][:600].replace('<','&lt;')}{'...' if len(r['texto_original'])>600 else ''}</pre>
                </div>
                <div>
                    <label>Texto Normalizado</label>
                    <pre>{(r['texto_normalizado'][:600].replace('<','&lt;') if r['texto_normalizado'] else '(sem resposta)')}{'...' if len(r.get('texto_normalizado',''))>600 else ''}</pre>
                </div>
            </div>
            {diff_html}
        </div>"""

    # Lista de passos executados para o relatório
    passos_html = " ".join(
        f'<span class="tag">{p}</span>'
        for p in dados.get("passos_executados", [])
    ) or "<em>não disponível</em>"

    html_output = f"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<title>Relatório NormText</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #fdf2f8; color: #4a235a; padding: 2rem; }}
  h1 {{ color: #d63384; font-size: 1.8rem; margin-bottom: .3rem; }}
  h2 {{ color: #a0527a; font-size: 1.1rem; margin: 2rem 0 .8rem;
        border-bottom: 2px solid #f4a7c3; padding-bottom: .4rem; }}
  .meta {{ color: #888; font-size: .85rem; margin-bottom: 2rem; }}
  .section {{ background: #fff; border: 1.5px solid #f4a7c3; border-radius: 14px;
              padding: 1.2rem; margin-bottom: 1.5rem; }}
  .kv-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: .6rem; }}
  .kv {{ background: #fce4ec; border-radius: 10px; padding: .5rem .8rem; }}
  .kv strong {{ display: block; font-size: .72rem; color: #a0527a; text-transform: uppercase; }}
  .kv span {{ font-size: 1rem; font-weight: 700; color: #d63384; }}
  pre {{ white-space: pre-wrap; word-break: break-word; font-size: .75rem;
         background: #fff0f6; border: 1px solid #f4a7c3; border-radius: 8px;
         padding: .7rem; max-height: 220px; overflow-y: auto; margin-top: .4rem; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: .5rem; }}
  label {{ font-size: .78rem; color: #a0527a; font-weight: 600; }}
  .chunk-block {{ border: 1px solid #f4a7c3; border-radius: 10px; padding: .8rem;
                  margin: .6rem 0; background: #fffafe; }}
  .chunk-header {{ display: flex; justify-content: space-between; align-items: center;
                   font-weight: 700; font-size: .82rem; margin-bottom: .4rem; }}
  .tag {{ display: inline-block; background: #fce4ec; border: 1px solid #f4a7c3;
          border-radius: 20px; padding: .15rem .7rem; font-size: .72rem;
          color: #a0527a; margin: .1rem; font-weight: 600; }}
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
  <div style="margin-top:.8rem">
    <label>Passos executados (por ordem)</label><br>
    <div style="margin-top:.3rem">{passos_html}</div>
  </div>
</div>

<h2>2. Texto Antes e Depois da Limpeza</h2>
<div class="section">
  <div class="two-col">
    <div>
      <label>Texto Bruto (extraído)</label>
      <pre>{texto_bruto[:1200].replace('<','&lt;')}{'...' if len(texto_bruto)>1200 else ''}</pre>
    </div>
    <div>
      <label>Texto Limpo (após pipeline)</label>
      <pre>{texto_limpo[:1200].replace('<','&lt;')}{'...' if len(texto_limpo)>1200 else ''}</pre>
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
    <pre>{texto_normalizado[:1500].replace('<','&lt;') if texto_normalizado else '(sem resposta do SLM)'}{'...' if len(texto_normalizado)>1500 else ''}</pre>
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
    """Gera o relatório em PDF usando WeasyPrint ou fallback reportlab."""
    html_content = gerar_relatorio_html(dados)

    try:
        from weasyprint import HTML as WeasyprintHTML
        pdf_bytes = WeasyprintHTML(string=html_content).write_pdf()
        return pdf_bytes
    except ImportError:
        pass

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
        story  = []

        agora  = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
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
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#fff0f6'), colors.white]),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#f4a7c3')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('PADDING',  (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4*cm))

        story.append(Paragraph("2. Avaliação da Normalização", h2_style))
        m_data = [
            ["Métrica", "Valor"],
            ["Palavras antes", str(metricas.get("palavras_antes", "—"))],
            ["Palavras depois", str(metricas.get("palavras_depois", "—"))],
            ["Caracteres antes", str(metricas.get("chars_antes", "—"))],
            ["Caracteres depois", str(metricas.get("chars_depois", "—"))],
            ["Similaridade (%)", str(metricas.get("similaridade_pct", "—")) + "%"],
            ["Redução de caracteres (%)", str(metricas.get("reducao_chars_pct", "—")) + "%"],
        ]
        t2 = Table(m_data, colWidths=[9*cm, 8*cm])
        t2.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f4a7c3')),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#fff0f6'), colors.white]),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#f4a7c3')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('PADDING',  (0,0), (-1,-1), 5),
        ]))
        story.append(t2)
        story.append(Spacer(1, 0.4*cm))

        story.append(Paragraph("3. Detalhe por Chunk", h2_style))
        for r in resultados_slm:
            estado = "Sucesso" if r["sucesso"] else f"Erro: {r.get('erro','')}"
            tentativas = r.get("tentativas", 1)
            info_extra = f" ({tentativas} tentativa(s))" if tentativas > 1 else ""
            story.append(Paragraph(f"<b>Chunk {r['chunk_id']}</b> – {estado}{info_extra}", normal))
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
            display: block; font-size: 0.8rem; text-align: center;
            margin-bottom: 1rem; opacity: 0.5; letter-spacing: 4px;
        }
        header { border-bottom: 2px dashed #f4a7c3; padding-bottom: 1rem; margin-bottom: 2rem; text-align: center; }
        header h1 { font-size: 2rem; color: #d63384; letter-spacing: 3px; font-weight: 700; text-shadow: 2px 2px 0px #f9c6e0; }
        header h1::before { content: '🌸 '; }
        header h1::after  { content: ' 🌸'; }
        header p { font-size: 0.85rem; color: #c06090; margin-top: 0.4rem; font-weight: 500; }

        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
        @media(max-width:900px){ .grid { grid-template-columns: 1fr; } }

        .card {
            background: rgba(255, 255, 255, 0.75);
            border: 2px solid #f4a7c3; border-radius: 20px; padding: 1.4rem;
            box-shadow: 0 4px 20px rgba(214, 51, 132, 0.1); backdrop-filter: blur(6px);
        }
        .card h2 {
            font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1.5px;
            color: #d63384; margin-bottom: 1rem; border-bottom: 1.5px dashed #f4a7c3;
            padding-bottom: 0.5rem; font-weight: 700;
        }
        label { font-size: 0.82rem; color: #a0527a; display: block; margin: 0.6rem 0 0.2rem; font-weight: 600; }
        input[type="file"], input[type="number"] {
            width: 100%; background: #fff0f6; border: 1.5px solid #f4a7c3;
            color: #6b3a52; padding: 0.5rem 0.8rem; border-radius: 12px;
            font-family: inherit; font-size: 0.82rem;
        }
        input[type="file"]:focus, input[type="number"]:focus { outline: none; border-color: #d63384; }
        .checkbox-group label { display: flex; align-items: center; gap: 0.5rem; color: #6b3a52; cursor: pointer; margin: 0.35rem 0; font-weight: 500; }
        input[type="checkbox"] { accent-color: #d63384; width: 15px; height: 15px; }

        button {
            margin-top: 1rem; width: 100%; padding: 0.65rem;
            background: linear-gradient(135deg, #f06292, #d63384);
            color: #fff; border: none; border-radius: 14px;
            font-family: inherit; font-size: 0.88rem; font-weight: 700;
            cursor: pointer; transition: all .2s; letter-spacing: 0.5px;
            box-shadow: 0 3px 10px rgba(214, 51, 132, 0.3);
        }
        button:hover { background: linear-gradient(135deg, #e91e8c, #c2185b); transform: translateY(-1px); box-shadow: 0 5px 15px rgba(214, 51, 132, 0.4); }
        button:disabled { background: #f0d0de; color: #c9a0b8; cursor: not-allowed; transform: none; box-shadow: none; }
        button.btn-cancel { background: linear-gradient(135deg, #e57373, #c62828); margin-top: .5rem; }
        button.btn-cancel:hover { background: linear-gradient(135deg, #ef5350, #b71c1c); }

        textarea {
            width: 100%; min-height: 180px; background: #fff0f6;
            border: 1.5px solid #f4a7c3; color: #6b3a52; padding: 0.7rem;
            border-radius: 12px; font-family: inherit; font-size: 0.75rem;
            resize: vertical; margin-top: 0.5rem; line-height: 1.5;
        }
        textarea:focus { outline: none; border-color: #d63384; }

        .tag { display: inline-block; background: #fce4ec; border: 1px solid #f4a7c3; border-radius: 20px; padding: .15rem .7rem; font-size: .72rem; color: #a0527a; margin: .1rem; font-weight: 600; }
        .tag.green { background: #fce4ec; border-color: #d63384; color: #d63384; }
        .tag.blue  { background: #f8e8f0; border-color: #c06090; color: #c06090; }

        #status { margin-top: 1rem; padding: 0.6rem 1rem; border-radius: 12px; font-size: 0.82rem; font-weight: 600; display: none; }
        .status-ok    { background: #fce4ec; border: 1.5px solid #d63384; color: #c2185b; display:block!important; }
        .status-error { background: #fdecea; border: 1.5px solid #e57373; color: #c62828; display:block!important; }
        .status-info  { background: #fdf0f6; border: 1.5px solid #f4a7c3; color: #a0527a; display:block!important; }

        /* Barra de progresso */
        .progress-bar-container {
            background: #fce4ec; border-radius: 20px; height: 12px;
            margin: .5rem 0; overflow: hidden; display: none;
        }
        .progress-bar-fill {
            height: 100%; background: linear-gradient(90deg, #f06292, #d63384);
            border-radius: 20px; transition: width .4s ease;
            width: 0%;
        }
        .progress-label { font-size: .75rem; color: #a0527a; font-weight: 600; text-align: center; margin-bottom: .3rem; }

        .chunk-list { max-height: 300px; overflow-y: auto; margin-top: 0.5rem; }
        .chunk-list::-webkit-scrollbar { width: 6px; }
        .chunk-list::-webkit-scrollbar-thumb { background: #f4a7c3; border-radius: 10px; }
        .chunk-item { background: #fff0f6; border: 1px solid #f4a7c3; border-radius: 12px; padding: 0.6rem 0.8rem; margin: 0.4rem 0; font-size: 0.72rem; line-height: 1.5; }
        .chunk-item strong { color: #d63384; }

        .full-width { grid-column: 1 / -1; }
        .timer { font-size: .72rem; color: #c06090; font-weight: 600; margin-left: .5rem; }
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
            <label><input type="checkbox" id="opt-espacos"   checked> Normalizar espaços e pontuação</label>
        </div>
        <button id="btn-limpar" onclick="limparTexto()" disabled>Limpar Texto</button>
        <label>Texto depois da limpeza:</label>
        <textarea id="texto-limpo" readonly placeholder="O texto limpo aparecerá aqui..."></textarea>
        <div id="info-limpeza" style="margin-top:.5rem"></div>
    </div>

    <!-- ETAPA 3 -->
    <div class="card full-width">
        <h2>Etapa 3 – Preparação do Input para Normalização</h2>
        <label>Tamanho do chunk (palavras, entre 50 e 4000):</label>
        <input type="number" id="chunk-size" value="200" min="50" max="4000">
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
        <button id="btn-cancelar" class="btn-cancel" onclick="cancelarSLM()" style="display:none">✕ Cancelar</button>

        <!-- Barra de progresso por chunk -->
        <div class="progress-label" id="prog-label" style="display:none"></div>
        <div class="progress-bar-container" id="prog-container">
            <div class="progress-bar-fill" id="prog-fill"></div>
        </div>
        <span class="timer" id="timer-slm"></span>

        <div id="slm-progresso" style="margin-top:.8rem"></div>
        <div class="chunk-list" id="slm-resultados"></div>
    </div>

    <!-- ETAPA 5 -->
    <div class="card full-width">
        <h2>Etapa 5 – Relatório Automático</h2>
        <p style="font-size:.82rem;color:#a0527a;margin-bottom:.8rem">
            Gera um relatório com os parâmetros da pipeline, texto antes/depois,
            avaliação da normalização e diff visual por chunk.
        </p>
        <div style="display:flex;gap:1rem;">
            <button id="btn-html" onclick="exportarRelatorio('html')" disabled style="flex:1">📄 Exportar HTML</button>
            <button id="btn-pdf"  onclick="exportarRelatorio('pdf')"  disabled style="flex:1">📑 Exportar PDF</button>
        </div>
        <div id="relatorio-info" style="margin-top:.8rem"></div>
    </div>

</div>

<div id="status"></div>

<script>
    let textoExtraido  = "";
    let textoLimpo     = "";
    let dadosInput     = null;
    let resultadosSLM  = null;
    let passosLimpeza  = [];
    let cancelarEnvio  = false;
    let timerInterval  = null;

    function mostrarStatus(msg, tipo = "info") {
        const el = document.getElementById("status");
        el.textContent = msg;
        el.className = "status-" + tipo;
    }

    // ── ETAPA 1 ────────────────────────────────────────────────
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

            document.getElementById("info-extracao").innerHTML = `
                <span class="tag blue">Formato: ${data.formato}</span>
                <span class="tag">Caracteres: ${data.num_chars}</span>
                <span class="tag">Palavras: ${data.num_palavras}</span>`;

            mostrarStatus("✓ Texto extraído com sucesso!", "ok");
        } catch(e) { mostrarStatus("Erro na extração: " + e, "error"); }
    }

    // ── ETAPA 2 ────────────────────────────────────────────────
    async function limparTexto() {
        if (!textoExtraido) { mostrarStatus("Extrai o texto primeiro.", "error"); return; }

        const opcoes = {
            remover_artefactos:         document.getElementById("opt-artefactos").checked,
            remover_cabecalhos_rodapes:  document.getElementById("opt-cabecalhos").checked,
            reconstruir_paragrafos:      document.getElementById("opt-paragrafos").checked,
            normalizar_espacos:          document.getElementById("opt-espacos").checked,
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

            textoLimpo   = data.texto_limpo;
            passosLimpeza = data.passos_executados || [];

            document.getElementById("texto-limpo").value = textoLimpo;
            document.getElementById("btn-preparar").disabled = false;

            // Mostra redução de caracteres
            const reducao = data.chars_antes > 0
                ? Math.round((1 - data.chars_depois / data.chars_antes) * 100)
                : 0;
            document.getElementById("info-limpeza").innerHTML = `
                <span class="tag">Chars antes: ${data.chars_antes}</span>
                <span class="tag">Chars depois: ${data.chars_depois}</span>
                <span class="tag green">Redução: ${reducao}%</span>`;

            mostrarStatus("✓ Limpeza concluída!", "ok");
        } catch(e) { mostrarStatus("Erro na limpeza: " + e, "error"); }
    }

    // ── ETAPA 3 ────────────────────────────────────────────────
    async function prepararInput() {
        if (!textoLimpo) { mostrarStatus("Limpa o texto primeiro.", "error"); return; }

        // [MELHORIA 2] Validação no cliente também
        let tamanho = parseInt(document.getElementById("chunk-size").value) || 800;
        tamanho = Math.max(50, Math.min(4000, tamanho));
        document.getElementById("chunk-size").value = tamanho;

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
                <span class="tag green">Chunks: ${data.num_chunks}</span>
                <span class="tag">Total palavras: ${data.palavras_total}</span>
                <span class="tag">Média por chunk: ${data.media_palavras_por_chunk} palavras</span>`;

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

    // ── ETAPA 4 ────────────────────────────────────────────────
    function cancelarSLM() {
        cancelarEnvio = true;
        mostrarStatus("A cancelar... o chunk em curso terminará antes de parar.", "info");
    }

    function iniciarTimer() {
        const inicio = Date.now();
        const el = document.getElementById("timer-slm");
        timerInterval = setInterval(() => {
            const seg = Math.floor((Date.now() - inicio) / 1000);
            el.textContent = `⏱ ${seg}s decorridos`;
        }, 1000);
    }

    function pararTimer() {
        clearInterval(timerInterval);
        document.getElementById("timer-slm").textContent = "";
    }

    async function enviarSLM() {
        if (!dadosInput) { mostrarStatus("Prepara o input primeiro.", "error"); return; }

        cancelarEnvio = false;
        document.getElementById("btn-slm").disabled = true;
        document.getElementById("btn-cancelar").style.display = "block";

        const progContainer = document.getElementById("prog-container");
        const progFill      = document.getElementById("prog-fill");
        const progLabel     = document.getElementById("prog-label");
        const resultadosDiv = document.getElementById("slm-resultados");
        resultadosDiv.innerHTML = "";

        progContainer.style.display = "block";
        progLabel.style.display = "block";
        iniciarTimer();

        const total = dadosInput.num_chunks;
        resultadosSLM = [];
        let chunksOk  = 0;
        let chunksErr = 0;

        mostrarStatus(`A processar chunk 1 de ${total}...`, "info");

        // Envio chunk a chunk para feedback em tempo real
        for (let i = 0; i < total; i++) {
            if (cancelarEnvio) {
                mostrarStatus(`Cancelado após ${i} de ${total} chunks.`, "error");
                break;
            }

            const pct = Math.round((i / total) * 100);
            progFill.style.width = pct + "%";
            progLabel.textContent = `Chunk ${i+1} de ${total} (${pct}%)`;
            mostrarStatus(`A processar chunk ${i+1} de ${total}...`, "info");

            try {
                const resp = await fetch("/slm/chunk", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        chunk:  dadosInput.chunks[i],
                        prompt: dadosInput.prompts[i],
                        chunk_id: i + 1
                    })
                });
                const r = await resp.json();

                resultadosSLM.push(r);
                if (r.sucesso) chunksOk++; else chunksErr++;

                // Adiciona resultado à lista em tempo real
                const div = document.createElement("div");
                div.className = "chunk-item";
                const cor    = r.sucesso ? "#27ae60" : "#e74c3c";
                const estado = r.sucesso ? "✓ OK" : `✗ Erro: ${r.erro || ''}`;
                const tentativas = r.tentativas > 1 ? ` (${r.tentativas} tentativas)` : "";
                div.innerHTML = `
                    <strong style="color:${cor}">Chunk ${r.chunk_id} – ${estado}${tentativas}</strong><br><br>
                    <small><b>Original:</b></small><br>${(r.texto_original||'').substring(0,250)}...<br><br>
                    <small><b>Normalizado:</b></small><br>${(r.texto_normalizado||'(sem resposta)').substring(0,250)}`;
                resultadosDiv.appendChild(div);
                resultadosDiv.scrollTop = resultadosDiv.scrollHeight;

            } catch(e) {
                resultadosSLM.push({
                    chunk_id: i + 1,
                    texto_original: dadosInput.chunks[i],
                    texto_normalizado: "",
                    sucesso: false,
                    erro: String(e),
                    tentativas: 1,
                });
                chunksErr++;
            }
        }

        pararTimer();
        progFill.style.width = "100%";
        progLabel.textContent = `Concluído: ${chunksOk} OK, ${chunksErr} erro(s)`;

        document.getElementById("slm-progresso").innerHTML = `
            <span class="tag green">✓ ${chunksOk} chunk(s) normalizados</span>
            ${chunksErr > 0 ? `<span class="tag" style="color:#e74c3c">✗ ${chunksErr} erro(s)</span>` : ''}`;

        document.getElementById("btn-cancelar").style.display = "none";
        document.getElementById("btn-html").disabled = false;
        document.getElementById("btn-pdf").disabled  = false;
        mostrarStatus("✓ Normalização concluída! Podes exportar o relatório.", "ok");
    }

    // ── ETAPA 5 ────────────────────────────────────────────────
    async function exportarRelatorio(formato) {
        if (!resultadosSLM) { mostrarStatus("Processa o texto no SLM primeiro.", "error"); return; }

        const tamanho = parseInt(document.getElementById("chunk-size").value) || 800;
        const opcoes  = {
            remover_artefactos:         document.getElementById("opt-artefactos").checked,
            remover_cabecalhos_rodapes:  document.getElementById("opt-cabecalhos").checked,
            reconstruir_paragrafos:      document.getElementById("opt-paragrafos").checked,
            normalizar_espacos:          document.getElementById("opt-espacos").checked,
        };

        const infoDiv = document.getElementById("relatorio-info");
        infoDiv.innerHTML = `<span class="tag">A gerar relatório ${formato.toUpperCase()}...</span>`;

        try {
            const resp = await fetch("/relatorio/" + formato, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    texto_bruto:         textoExtraido,
                    texto_limpo:         textoLimpo,
                    resultados_slm:      resultadosSLM,
                    idioma:              dadosInput.idioma,
                    num_chunks:          dadosInput.num_chunks,
                    passos_executados:   passosLimpeza,
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
    nome     = ficheiro.filename
    dados    = ficheiro.read()

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

    resultado = limpar_texto(body["texto"], body.get("opcoes", {}))
    return jsonify(resultado)


@app.route("/preparar", methods=["POST"])
def rota_preparar():
    """Endpoint da Etapa 3 – recebe texto limpo, devolve chunks + prompts."""
    body = request.get_json()
    if not body or "texto" not in body:
        return jsonify({"erro": "Campo 'texto' em falta."}), 400

    # [MELHORIA 2] Validação do tamanho_chunk no servidor
    tamanho = body.get("tamanho_chunk", 800)
    try:
        tamanho = int(tamanho)
    except (TypeError, ValueError):
        tamanho = 800
    tamanho = max(50, min(4000, tamanho))

    resultado = preparar_input(body["texto"], tamanho_chunk=tamanho)
    return jsonify(resultado)


@app.route("/slm/chunk", methods=["POST"])
def rota_slm_chunk():
    """
    Endpoint da Etapa 4 – processa UM chunk de cada vez.
    Permite feedback em tempo real no frontend (barra de progresso).
    Body: { "chunk": "...", "prompt": "...", "chunk_id": 1 }
    """
    body = request.get_json()
    if not body or "chunk" not in body or "prompt" not in body:
        return jsonify({"erro": "Campos 'chunk' e 'prompt' em falta."}), 400

    resultado_api = enviar_para_slm(body["prompt"])
    return jsonify({
        "chunk_id":         body.get("chunk_id", 1),
        "texto_original":   body["chunk"],
        "prompt":           body["prompt"],
        "texto_normalizado": resultado_api["resposta"],
        "sucesso":          resultado_api["sucesso"],
        "erro":             resultado_api["erro"],
        "tokens":           resultado_api["tokens"],
        "modelo":           resultado_api["modelo"],
        "tentativas":       resultado_api.get("tentativas", 1),
    })


# Mantido para compatibilidade com chamadas que enviem todos os chunks de uma vez
@app.route("/slm", methods=["POST"])
def rota_slm():
    """Endpoint da Etapa 4 – processa todos os chunks de uma vez (batch)."""
    body = request.get_json()
    if not body or "chunks" not in body or "prompts" not in body:
        return jsonify({"erro": "Campos 'chunks' e 'prompts' em falta."}), 400

    chunks  = body["chunks"]
    prompts = body["prompts"]

    if len(chunks) != len(prompts):
        return jsonify({"erro": "Número de chunks e prompts não coincide."}), 400

    resultados  = processar_chunks_slm(chunks, prompts)
    chunks_ok   = sum(1 for r in resultados if r["sucesso"])
    chunks_erro = sum(1 for r in resultados if not r["sucesso"])

    return jsonify({
        "resultados":  resultados,
        "chunks_ok":   chunks_ok,
        "chunks_erro": chunks_erro,
        "total":       len(resultados),
    })


@app.route("/relatorio/html", methods=["POST"])
def rota_relatorio_html():
    """Endpoint da Etapa 5 – gera e devolve relatório em HTML."""
    body = request.get_json()
    if not body:
        return jsonify({"erro": "Body em falta."}), 400

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