"""Leitor de `.env` — sem dependência externa.

O projeto lê credencial de variável de ambiente, o que no Windows significa
`$env:X = "..."` a cada sessão do PowerShell. Um `.env` na raiz resolve isso e
mantém o token fora do código versionado.

Não usa `python-dotenv` de propósito: o formato é simples e o projeto vive com
duas dependências: `curl_cffi` e `rapidfuzz`.

Regras suportadas:

    CHAVE=valor
    CHAVE="valor com espaço"      # aspas são removidas
    export CHAVE=valor            # o prefixo export é ignorado
    # comentário                  # linha inteira, ou depois do valor
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# CHAVE=VALOR, com "export " opcional na frente.
LINHA = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _limpar(valor: str) -> str:
    valor = valor.strip()
    # Aspas casadas preservam espaços e o '#' literal; sem elas, '#' abre comentário.
    if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
        return valor[1:-1]
    return valor.split("#", 1)[0].strip()


def parse(texto: str) -> dict[str, str]:
    """Converte o conteúdo de um .env num dict. Linha inválida é ignorada."""
    saida: dict[str, str] = {}
    for linha in texto.splitlines():
        if not linha.strip() or linha.lstrip().startswith("#"):
            continue
        m = LINHA.match(linha)
        if m:
            saida[m.group(1)] = _limpar(m.group(2))
    return saida


def carregar(caminho: Path | str | None = None, *, sobrescrever: bool = False) -> dict[str, str]:
    """Carrega o `.env` no ambiente do processo.

    Por padrão NÃO sobrescreve variável já definida: quem exportou na mão (ou o
    systemd/Docker) mandou mais que o arquivo.
    """
    caminho = Path(caminho) if caminho else Path(__file__).resolve().parent.parent / ".env"
    if not caminho.is_file():
        return {}

    try:
        conteudo = caminho.read_text(encoding="utf-8")
    except OSError:
        return {}

    valores = parse(conteudo)
    for chave, valor in valores.items():
        if sobrescrever or chave not in os.environ:
            os.environ[chave] = valor
    return valores


def escrever(valores: dict[str, str], caminho: Path | str | None = None) -> Path:
    """Grava/atualiza chaves no `.env`, preservando o resto do arquivo."""
    caminho = Path(caminho) if caminho else Path(__file__).resolve().parent.parent / ".env"
    linhas: list[str] = []
    if caminho.is_file():
        linhas = caminho.read_text(encoding="utf-8").splitlines()

    pendentes = dict(valores)
    saida: list[str] = []
    for linha in linhas:
        m = LINHA.match(linha)
        if m and m.group(1) in pendentes:
            chave = m.group(1)
            saida.append(f'{chave}="{pendentes.pop(chave)}"')
        else:
            saida.append(linha)

    for chave, valor in pendentes.items():
        saida.append(f'{chave}="{valor}"')

    caminho.write_text("\n".join(saida).rstrip("\n") + "\n", encoding="utf-8")
    return caminho
