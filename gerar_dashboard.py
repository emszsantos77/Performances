# ==============================================================================
# SCRIPT DE CONSOLIDAÇÃO - JBS FRIOS (VERSÃO OTIMIZADA + PRODUTOS + DATA)
# ==============================================================================
#
# REGRAS:
#   - Coluna B: Emissão -> fonte principal do mês
#   - Coluna C: Grupo
#   - Coluna D: Operação
#   - Coluna N: fallback para vendedor
#   - Coluna W: Motivo da devolução
#
# NOVIDADES DESTA VERSÃO:
#   - CAMPO "data" (YYYY-MM-DD) em cada registro -> habilita o calendário
#     da tela Evolução Semanal e a comparação com os 90 dias anteriores
#     (gráficos, clientes novos e clientes que deixaram de comprar).
#   - VENDEDOR: entre colunas candidatas, escolhe a que contém NOMES
#     (análise de conteúdo), e não códigos numéricos.
#   - SUPERVISOR: aceita cabeçalhos "Supervisor", "Sup", "Supervisão".
#   - PRODUTO: nova coluna detectada (produto/item/material) -> campo "prod".
#   - QUANTIDADE: nova coluna detectada (qtd/qtde/quantidade) -> campo "qtd".
#   - AMOSTRAS de validação no terminal (vendedor, supervisor, produto).
#
# FATURAMENTO LÍQUIDO: Vendas - Devoluções efetivas
# DEVOLUÇÕES COM MOTIVOS 5, 8, 26 e 27: são TROCAS.
# TROCA: Bonificação de saída + devoluções dos motivos 5, 8, 26 e 27.
#
# SAÍDA: dashboard_data.json, dashboard_data_part2.json, ...
# Cada JSON fica abaixo de 25 MB.
# ==============================================================================

import csv
import datetime
import glob
import json
import os
import re
import time
import unicodedata
import urllib.request
from pathlib import Path

import pandas as pd


# ==============================================================================
# CONFIGURAÇÕES
# ==============================================================================

ROOT_PADRAO = r"C:\Users\User\Desktop\Workspace\PERFORMANCES"

MOTIVOS_TROCA = {5, 8, 26, 27}
MOTIVOS_TROCA_TEXTO = {str(motivo) for motivo in MOTIVOS_TROCA}

POP_PADRAO = 12500

MAX_JSON_BYTES = 24 * 1024 * 1024
MARGEM_SEGURANCA_BYTES = 64

ARQUIVO_POPULACOES_MANUAL = "populacoes.csv"
ARQUIVO_CACHE_IBGE = "populacoes_ibge.csv"

CONSULTAR_IBGE = True

OP_BONIF_TROCA = "01 saida bonificacao jbs frios"

MESES_PT = {
    1: "Jan",
    2: "Fev",
    3: "Mar",
    4: "Abr",
    5: "Mai",
    6: "Jun",
    7: "Jul",
    8: "Ago",
    9: "Set",
    10: "Out",
    11: "Nov",
    12: "Dez",
}

SIDRA_URLS = [
    (
        "Estimativa mais recente",
        "https://apisidra.ibge.gov.br/values/t/6579/n6/all/v/all/p/last%201?formato=json",
    ),
    (
        "Censo 2022",
        "https://apisidra.ibge.gov.br/values/t/4709/n6/all/v/93/p/2022?formato=json",
    ),
]


# ==============================================================================
# CRONÔMETRO
# ==============================================================================

INICIO_EXECUCAO = time.perf_counter()


def marcar(mensagem):
    print(f"[{time.perf_counter() - INICIO_EXECUCAO:7.1f}s] {mensagem}")


# ==============================================================================
# NORMALIZAÇÃO E CONVERSÃO
# ==============================================================================

def normalize_text(value):
    if value is None:
        return ""

    text = str(value).strip()

    if text.lower() in {"nan", "none", "null"}:
        return ""

    text = unicodedata.normalize("NFD", text)
    text = "".join(
        char
        for char in text
        if unicodedata.category(char) != "Mn"
    )

    text = re.sub(r"[^a-zA-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


def normalize_series(serie):
    s = serie.astype("string").fillna("")

    s = s.str.normalize("NFD")
    s = s.str.replace(r"[\u0300-\u036f]", "", regex=True)
    s = s.str.replace(r"[^a-zA-Z0-9 ]+", " ", regex=True)
    s = s.str.replace(r"\s+", " ", regex=True)
    s = s.str.strip().str.lower()

    return s.mask(
        s.isin({"nan", "none", "null", "nat"}),
        "",
    )


def limpar_texto(serie):
    s = serie.astype("string").fillna("").str.strip()

    return s.mask(
        s.str.lower().isin({"nan", "none", "null", "nat"}),
        "",
    )


def parse_number(value):
    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        if pd.isna(value):
            return 0.0
        return float(value)

    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "null"}:
        return 0.0

    text = text.replace("R$", "")
    text = text.replace(" ", "")
    text = re.sub(r"[^\d,.\-]", "", text)

    if not text:
        return 0.0

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    elif "," in text:
        text = text.replace(".", "")
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return 0.0


def serie_para_numero(serie):
    numeros = pd.to_numeric(serie, errors="coerce")

    faltantes = numeros.isna() & serie.notna()

    if faltantes.any():
        convertidos = serie[faltantes].apply(parse_number)
        numeros = numeros.fillna(convertidos)

    return numeros.fillna(0.0)


def safe_text(value):
    if value is None:
        return ""

    if pd.isna(value):
        return ""

    text = str(value).strip()

    if text.lower() in {"nan", "none", "null"}:
        return ""

    return text


def converter_serie_emissao(serie):
    """
    Converte a coluna B inteira de forma VETORIZADA.
    """

    if pd.api.types.is_datetime64_any_dtype(serie):
        return serie

    if pd.api.types.is_numeric_dtype(serie):
        return pd.to_datetime(
            serie,
            unit="D",
            origin="1899-12-30",
            errors="coerce",
        )

    amostra = serie.dropna().head(30)

    if amostra.size and all(
        isinstance(
            valor,
            (pd.Timestamp, datetime.datetime, datetime.date),
        )
        for valor in amostra
    ):
        return pd.to_datetime(serie, errors="coerce")

    texto = (
        serie.astype("string")
        .str.strip()
        .str.replace("T", " ", regex=False)
    )

    formatos = [
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%d/%m/%y",
    ]

    datas = pd.to_datetime(
        texto,
        format=formatos[0],
        errors="coerce",
    )

    for formato in formatos[1:]:
        faltantes = datas.isna()

        if not faltantes.any():
            break

        datas.loc[faltantes] = pd.to_datetime(
            texto[faltantes],
            format=formato,
            errors="coerce",
        )

    faltantes = datas.isna()

    if faltantes.any():
        numeros = pd.to_numeric(texto[faltantes], errors="coerce")
        validos = numeros.notna()

        if validos.any():
            datas.loc[faltantes] = pd.to_datetime(
                numeros[validos],
                unit="D",
                origin="1899-12-30",
                errors="coerce",
            )

    return datas


def remove_uf_at_end(value):
    text = normalize_text(value)
    return re.sub(r"\s+[a-z]{2}$", "", text).strip()


# ==============================================================================
# ANÁLISE DE CONTEÚDO DAS COLUNAS
# ==============================================================================

def column_numeric_share(df, column, sample_size=500):
    """
    Retorna a proporção de valores puramente numéricos na coluna.

    Retorna None quando a coluna está vazia.
    """

    serie = df[column].dropna().head(sample_size)
    serie = serie.astype("string").str.strip()
    serie = serie[serie != ""]

    if serie.empty:
        return None

    mask = serie.str.fullmatch(r"\d+([.,]\d+)?")
    return float(mask.mean())


def pick_text_column(df, candidates):
    """
    Entre as colunas candidatas, escolhe a que contém mais texto
    e menos números. Serve para preferir 'Nome Vendedor' em vez de
    'Vendedor' quando a última contém códigos.
    """

    best = None
    best_score = None

    for column in candidates:
        share = column_numeric_share(df, column)

        if share is None:
            continue

        score = 1.0 - share

        if best is None or score > best_score:
            best = column
            best_score = score

    return best


def is_supervisor_name(normalized_name):
    if "supervisor" in normalized_name:
        return True
    if "supervisao" in normalized_name:
        return True

    tokens = normalized_name.split(" ")
    return "sup" in tokens


def is_vendedor_name(normalized_name):
    if "vendedor" in normalized_name:
        return True

    tokens = normalized_name.split(" ")
    return "vend" in tokens


# ==============================================================================
# DETECÇÃO DE COLUNAS
# ==============================================================================

def resolve_columns(df):
    normalized_columns = {
        column: normalize_text(column)
        for column in df.columns
    }

    used_columns = set()
    resolved = {}

    def is_code_column(normalized_name):
        return (
            "codigo" in normalized_name
            or normalized_name.startswith("cod")
            or normalized_name == "id"
            or normalized_name.startswith("id ")
            or " id " in f" {normalized_name} "
        )

    def find_column(logical_name, predicates):
        for predicate in predicates:
            for column in df.columns:
                if column in used_columns:
                    continue

                normalized_name = normalized_columns[column]

                if predicate(normalized_name):
                    resolved[logical_name] = column
                    used_columns.add(column)
                    return column

        return None

    # --------------------------------------------------------------------------
    # EMISSÃO
    # --------------------------------------------------------------------------

    find_column(
        "emissao",
        [
            lambda name: name in {
                "emissao",
                "dt emissao",
                "data emissao",
                "data de emissao",
                "emissao data",
            },
            lambda name: "emiss" in name,
        ],
    )

    if "emissao" not in resolved and len(df.columns) > 1:
        resolved["emissao"] = df.columns[1]
        used_columns.add(df.columns[1])

    # --------------------------------------------------------------------------
    # GRUPO
    # --------------------------------------------------------------------------

    find_column(
        "grupo",
        [
            lambda name: name == "grupo",
            lambda name: "grupo" in name and "sub" not in name,
        ],
    )

    # --------------------------------------------------------------------------
    # OPERAÇÃO
    # --------------------------------------------------------------------------

    find_column(
        "operacao",
        [
            lambda name: name == "operacao",
            lambda name: "operac" in name,
        ],
    )

    # --------------------------------------------------------------------------
    # MOTIVO
    # --------------------------------------------------------------------------

    find_column(
        "motivo",
        [
            lambda name: "motivo" in name,
            lambda name: "devolucao" in name and "motivo" in name,
        ],
    )

    if "motivo" not in resolved and len(df.columns) > 22:
        resolved["motivo"] = df.columns[22]
        used_columns.add(df.columns[22])

    # --------------------------------------------------------------------------
    # VALOR
    # --------------------------------------------------------------------------

    find_column(
        "vlr",
        [
            lambda name: "vlr" in name and "total" in name,
            lambda name: "valor" in name and "total" in name,
            lambda name: name in {"vlr total", "valor total"},
            lambda name: "vlr" in name,
            lambda name: "valor" in name,
        ],
    )

    # --------------------------------------------------------------------------
    # QUANTIDADE (antes do peso, para não haver conflito)
    # --------------------------------------------------------------------------

    find_column(
        "quantidade",
        [
            lambda name: name in {
                "qtd",
                "qtde",
                "quantidade",
                "qt",
                "quantidade vendida",
                "qtd vendida",
                "qtd venda",
            },
            lambda name: ("qtd" in name or "quantidade" in name)
            and "peso" not in name,
        ],
    )

    # --------------------------------------------------------------------------
    # PESO
    # --------------------------------------------------------------------------

    find_column(
        "peso",
        [
            lambda name: "peso" in name,
            lambda name: "kg" in name,
        ],
    )

    # --------------------------------------------------------------------------
    # CIDADE
    # --------------------------------------------------------------------------

    find_column(
        "cidade",
        [
            lambda name: name in {"cidade", "municipio", "municipio venda"},
            lambda name: "cidade" in name,
            lambda name: "municipio" in name,
        ],
    )

    # --------------------------------------------------------------------------
    # CLIENTE
    # --------------------------------------------------------------------------

    find_column(
        "cliente",
        [
            lambda name: "cnpj" in name,
            lambda name: "cpf" in name,
            lambda name: "cliente" in name and not is_code_column(name),
            lambda name: name in {"cliente", "razao social", "razao"},
        ],
    )

    # --------------------------------------------------------------------------
    # GERENTE
    # --------------------------------------------------------------------------

    gerente_candidates = []

    for column in df.columns:
        if column in used_columns:
            continue

        normalized_name = normalized_columns[column]

        if "gerent" in normalized_name and not is_code_column(normalized_name):
            gerente_candidates.append(column)

    if gerente_candidates:
        resolved["gerente"] = gerente_candidates[0]
        used_columns.add(gerente_candidates[0])

    # --------------------------------------------------------------------------
    # SUPERVISOR (aceita "Supervisor", "Sup", "Supervisão")
    # --------------------------------------------------------------------------

    supervisor_candidates = []

    for column in df.columns:
        if column in used_columns:
            continue

        normalized_name = normalized_columns[column]

        if is_code_column(normalized_name):
            continue

        if is_supervisor_name(normalized_name):
            supervisor_candidates.append(column)

    if supervisor_candidates:
        chosen = pick_text_column(df, supervisor_candidates)
        chosen = chosen or supervisor_candidates[0]

        resolved["supervisor"] = chosen
        used_columns.add(chosen)

    # --------------------------------------------------------------------------
    # FORNECEDOR
    # --------------------------------------------------------------------------

    find_column(
        "fornecedor",
        [
            lambda name: name == "fornecedor",
            lambda name: "fornecedor" in name,
            lambda name: "fabricante" in name,
        ],
    )

    # --------------------------------------------------------------------------
    # SUBGRUPO
    # --------------------------------------------------------------------------

    find_column(
        "subgrupo",
        [
            lambda name: name == "subgrupo",
            lambda name: "subgrupo" in name,
            lambda name: name == "sub",
        ],
    )

    # --------------------------------------------------------------------------
    # PRODUTO
    # --------------------------------------------------------------------------

    produto_candidates = []

    for column in df.columns:
        if column in used_columns:
            continue

        normalized_name = normalized_columns[column]

        if is_code_column(normalized_name):
            continue

        if (
            "produto" in normalized_name
            or "item" in normalized_name
            or "material" in normalized_name
        ):
            produto_candidates.append(column)

    if produto_candidates:
        # Prefere cabeçalhos de descrição/nome.
        produto_candidates.sort(
            key=lambda c: (
                0
                if (
                    "desc" in normalized_columns[c]
                    or "nome" in normalized_columns[c]
                )
                else 1,
                df.columns.get_loc(c),
            )
        )

        chosen = pick_text_column(df, produto_candidates)
        chosen = chosen or produto_candidates[0]

        resolved["produto"] = chosen
        used_columns.add(chosen)

    # --------------------------------------------------------------------------
    # VENDEDOR (escolhe a coluna com NOMES, não códigos)
    # --------------------------------------------------------------------------

    vendedor_candidates = []

    for column in df.columns:
        if column in used_columns:
            continue

        normalized_name = normalized_columns[column]

        if is_code_column(normalized_name):
            continue

        if is_vendedor_name(normalized_name):
            vendedor_candidates.append(column)

    if vendedor_candidates:
        # Prefere cabeçalhos com "nome".
        vendedor_candidates.sort(
            key=lambda c: (
                0 if "nome" in normalized_columns[c] else 1,
                df.columns.get_loc(c),
            )
        )

        chosen = pick_text_column(df, vendedor_candidates)
        chosen = chosen or vendedor_candidates[0]

        resolved["vendedor"] = chosen
        used_columns.add(chosen)

    else:
        # Fallback: coluna N (índice 13). Se a N contiver apenas números,
        # procura uma coluna textual de vendedor/nome não utilizada.
        chosen = None

        if len(df.columns) > 13 and df.columns[13] not in used_columns:
            chosen = df.columns[13]

            share = column_numeric_share(df, chosen)

            if share is not None and share > 0.5:
                alternativas = []

                for column in df.columns:
                    if column in used_columns or column == chosen:
                        continue

                    normalized_name = normalized_columns[column]

                    if not (
                        is_vendedor_name(normalized_name)
                        or "nome" in normalized_name
                    ):
                        continue

                    alt_share = column_numeric_share(df, column)

                    if alt_share is not None and alt_share < 0.5:
                        alternativas.append(column)

                if alternativas:
                    chosen = alternativas[0]

        if chosen is not None:
            resolved["vendedor"] = chosen
            used_columns.add(chosen)

    return resolved


def print_column_mapping(mapping):
    print("\nMAPEAMENTO DE COLUNAS")
    print("-" * 70)

    for logical_name, real_column in mapping.items():
        print(f"{logical_name:15} <- {real_column}")

    print("-" * 70)


# ==============================================================================
# POPULAÇÕES
# ==============================================================================

def parse_population_number(value):
    number = parse_number(value)

    if number <= 0:
        return 0

    return int(round(number))


def load_manual_populations(root):
    path = os.path.join(root, ARQUIVO_POPULACOES_MANUAL)

    if not os.path.exists(path):
        print(f"\nArquivo manual de população não encontrado: {path}")
        return {}

    populations = {}

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as file:
            sample = file.read(4096)
            file.seek(0)

            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,")
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ";" if ";" in sample else ","

            reader = csv.DictReader(file, delimiter=delimiter)

            if not reader.fieldnames:
                print("populacoes.csv não possui cabeçalho.")
                return {}

            headers = {
                normalize_text(header): header
                for header in reader.fieldnames
                if header
            }

            city_column = None
            population_column = None

            for normalized, original in headers.items():
                if normalized in {
                    "cidade",
                    "municipio",
                    "nome",
                    "nome municipio",
                }:
                    city_column = original
                    break

            for normalized, original in headers.items():
                if normalized in {
                    "populacao",
                    "pop",
                    "habitantes",
                }:
                    population_column = original
                    break

            if not city_column or not population_column:
                print(
                    "populacoes.csv precisa conter as colunas "
                    "'cidade' e 'populacao'."
                )
                return {}

            for row in reader:
                city = safe_text(row.get(city_column))
                population = parse_population_number(
                    row.get(population_column)
                )

                if city and population > 0:
                    populations[remove_uf_at_end(city)] = population

    except Exception as error:
        print(f"Erro ao ler {ARQUIVO_POPULACOES_MANUAL}: {error}")
        return {}

    print(
        f"\nPopulações manuais carregadas de {ARQUIVO_POPULACOES_MANUAL}: "
        f"{len(populations)}"
    )

    return populations


def load_population_cache(root):
    path = os.path.join(root, ARQUIVO_CACHE_IBGE)

    if not os.path.exists(path):
        return {}

    populations = {}

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)

            for row in reader:
                city = (
                    row.get("cidade")
                    or row.get("municipio")
                    or row.get("nome")
                    or ""
                )

                population = (
                    row.get("populacao")
                    or row.get("pop")
                    or row.get("habitantes")
                    or 0
                )

                city = safe_text(city)
                population = parse_population_number(population)

                if city and population > 0:
                    populations[remove_uf_at_end(city)] = population

    except Exception as error:
        print(f"Erro ao ler cache do IBGE: {error}")

    return populations


def save_population_cache(root, populations):
    path = os.path.join(root, ARQUIVO_CACHE_IBGE)

    rows = sorted(
        populations.items(),
        key=lambda item: item[0],
    )

    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["cidade", "populacao"])

        for city, population in rows:
            writer.writerow([city, population])

    print(f"Cache de populações salvo em: {path}")


def download_ibge_populations(root):
    cached = load_population_cache(root)

    if cached:
        print(
            f"Cache de populações encontrado: {len(cached)} cidades. "
            "Nenhuma consulta à internet foi necessária."
        )
        return cached

    print("\nCache do IBGE não encontrado. Tentando consultar o SIDRA...")

    downloaded = {}

    for description, url in SIDRA_URLS:
        try:
            print(f"Consultando: {description}")

            with urllib.request.urlopen(url, timeout=60) as response:
                content = response.read().decode("utf-8")

            data = json.loads(content)

            if not isinstance(data, list) or len(data) < 2:
                continue

            for row in data[1:]:
                city_name = (
                    row.get("D1N")
                    or row.get("D1")
                    or row.get("NM_MUN")
                    or row.get("nome")
                    or ""
                )

                population_value = (
                    row.get("V")
                    or row.get("valor")
                    or row.get("POP")
                    or 0
                )

                city_name = safe_text(city_name)
                population_value = parse_population_number(population_value)

                if city_name and population_value > 0:
                    key = remove_uf_at_end(city_name)

                    if key not in downloaded:
                        downloaded[key] = population_value

            if downloaded:
                print(
                    f"Municípios recebidos nesta consulta: "
                    f"{len(downloaded)}"
                )
                break

        except Exception as error:
            print(f"Falha na consulta ao SIDRA: {error}")

    if downloaded:
        save_population_cache(root, downloaded)

    return downloaded


def build_population_map(root):
    if CONSULTAR_IBGE:
        ibge_populations = download_ibge_populations(root)
    else:
        ibge_populations = load_population_cache(root)

    manual_populations = load_manual_populations(root)

    result = dict(ibge_populations)
    result.update(manual_populations)

    print(
        f"Total de cidades disponíveis para GeoPerformance: "
        f"{len(result)}"
    )

    return result


# ==============================================================================
# LEITURA DOS ARQUIVOS
# ==============================================================================

def is_output_file(path):
    name = os.path.basename(path).lower()

    return (
        name == "dashboard_data.json"
        or re.match(r"dashboard_data_part\d+\.json$", name) is not None
        or name == ARQUIVO_POPULACOES_MANUAL.lower()
        or name == ARQUIVO_CACHE_IBGE.lower()
    )


def discover_input_files(root):
    candidates = []

    search_directories = [
        root,
        os.path.join(root, "Dados"),
    ]

    extensions = ("*.xlsx", "*.xls", "*.csv")

    for directory in search_directories:
        if not os.path.isdir(directory):
            continue

        for extension in extensions:
            candidates.extend(
                glob.glob(
                    os.path.join(directory, extension)
                )
            )

    unique = {}
    for path in candidates:
        if is_output_file(path):
            continue

        real_path = os.path.realpath(path)
        unique[real_path] = path

    files = sorted(
        unique.values(),
        key=lambda path: os.path.basename(path).lower(),
    )

    return files


def read_input_file(path):
    extension = Path(path).suffix.lower()

    if extension in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=object)

    if extension == ".csv":
        try:
            return pd.read_csv(
                path,
                dtype=object,
                sep=None,
                engine="python",
                encoding="utf-8-sig",
            )
        except Exception:
            return pd.read_csv(
                path,
                dtype=object,
                sep=";",
                encoding="latin1",
            )

    return pd.DataFrame()


def load_all_input_data(files):
    frames = []

    print(f"\nArquivos encontrados: {len(files)}")

    for path in files:
        print(f"Lendo: {path}")

        try:
            frame = read_input_file(path)

            if frame.empty:
                print("  Arquivo vazio. Ignorado.")
                continue

            frame["__arquivo_origem"] = os.path.basename(path)
            frames.append(frame)

            print(f"  Registros: {len(frame):,}")

        except Exception as error:
            print(f"  Erro ao ler arquivo: {error}")

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(
        frames,
        ignore_index=True,
        sort=False,
    )

    return combined


# ==============================================================================
# TRANSFORMAÇÃO DOS REGISTROS (VETORIZADA)
# ==============================================================================

def build_dashboard_records(df):
    if df.empty:
        return [], {}

    mapping = resolve_columns(df)
    print_column_mapping(mapping)

    required = ["emissao", "grupo", "operacao", "vlr"]

    missing = [
        field
        for field in required
        if field not in mapping
    ]

    if missing:
        raise RuntimeError(
            "Não foi possível localizar as colunas obrigatórias: "
            + ", ".join(missing)
        )

    # --------------------------------------------------------------------------
    # 1. DATA OFICIAL (coluna B)
    # --------------------------------------------------------------------------

    datas = converter_serie_emissao(df[mapping["emissao"]])

    invalidas = int(datas.isna().sum())

    if invalidas:
        print(
            f"Registros sem data válida na coluna Emissão: {invalidas:,} "
            "(serão ignorados)"
        )

    df = df.loc[datas.notna()].copy()
    datas = datas.loc[datas.notna()]

    if df.empty:
        return [], {}

    # --------------------------------------------------------------------------
    # 2. MÊS, DATA DIÁRIA E SEMANA VETORIZADOS
    # --------------------------------------------------------------------------

    dt_series = datas.dt.strftime("%Y-%m")

    # Data diária completa (YYYY-MM-DD) usada pelo calendário da tela
    # Evolução Semanal e pela comparação com os 90 dias anteriores.
    data_series = datas.dt.strftime("%Y-%m-%d")

    m_nome_series = datas.dt.month.map(MESES_PT).fillna("")

    iso = datas.dt.isocalendar()

    sem_series = (
        iso["year"].astype("string")
        + "-W"
        + iso["week"].astype("string").str.zfill(2)
    )

    # --------------------------------------------------------------------------
    # 3. CLASSIFICAÇÃO VETORIZADA
    # --------------------------------------------------------------------------

    grupo_n = normalize_series(df[mapping["grupo"]])
    operacao_n = normalize_series(df[mapping["operacao"]])

    coluna_motivo = mapping.get("motivo")

    if coluna_motivo:
        motivo_texto = df[coluna_motivo].astype("string").fillna("")
    else:
        motivo_texto = pd.Series(
            [""] * len(df),
            index=df.index,
            dtype="string",
        )

    numeros_motivo = motivo_texto.str.extract(r"^\s*(\d+)")[0]

    is_venda = grupo_n.isin({"venda", "vendas"})
    is_devolucao = grupo_n.str.contains("dev", regex=False)
    is_motivo_troca = numeros_motivo.isin(MOTIVOS_TROCA_TEXTO)

    is_troca_devolucao = is_devolucao & is_motivo_troca
    is_devolucao_efetiva = is_devolucao & ~is_motivo_troca

    is_bonificacao = operacao_n.eq(OP_BONIF_TROCA) | (
        operacao_n.str.contains("bonificacao", regex=False)
        & operacao_n.str.contains("saida", regex=False)
    )

    # --------------------------------------------------------------------------
    # 4. VALORES VETORIZADOS
    # --------------------------------------------------------------------------

    valor_abs = serie_para_numero(df[mapping["vlr"]]).abs()

    coluna_peso = mapping.get("peso")

    if coluna_peso:
        peso_abs = serie_para_numero(df[coluna_peso]).abs()
    else:
        peso_abs = pd.Series(0.0, index=df.index)

    coluna_quantidade = mapping.get("quantidade")

    if coluna_quantidade:
        qtd_abs = serie_para_numero(df[coluna_quantidade]).abs()
    else:
        print(
            "Aviso: coluna de QUANTIDADE não encontrada. "
            "O campo 'qtd' ficará zerado (Análise de Produtos)."
        )
        qtd_abs = pd.Series(0.0, index=df.index)

    v_bruto = valor_abs.where(is_venda, 0.0)
    v_dev = valor_abs.where(is_devolucao_efetiva, 0.0)
    v_liq = v_bruto - v_dev
    v_troca = valor_abs.where(is_bonificacao | is_troca_devolucao, 0.0)

    p_venda = peso_abs.where(is_venda, 0.0)
    p_dev = peso_abs.where(is_devolucao_efetiva, 0.0)
    p_liq = p_venda - p_dev

    q_venda = qtd_abs.where(is_venda, 0.0)
    q_dev = qtd_abs.where(is_devolucao_efetiva, 0.0)
    q_liq = q_venda - q_dev

    # --------------------------------------------------------------------------
    # 5. CAMPOS DE TEXTO
    # --------------------------------------------------------------------------

    def coluna_texto(nome_logico):
        coluna = mapping.get(nome_logico)

        if not coluna or coluna not in df.columns:
            return pd.Series(
                [""] * len(df),
                index=df.index,
                dtype="string",
            )

        return limpar_texto(df[coluna])

    montado = pd.DataFrame(
        {
            # Data diária (YYYY-MM-DD) - obrigatória para o calendário da
            # Evolução Semanal, clientes novos e que deixaram de comprar.
            "data": data_series,

            # Mês (YYYY-MM) usado pelas demais telas e filtros.
            "dt": dt_series,
            "m_nome": m_nome_series,
            "sem": sem_series,
            "ger": coluna_texto("gerente"),
            "sup": coluna_texto("supervisor"),
            "cid": coluna_texto("cidade"),
            "forn": coluna_texto("fornecedor"),
            "vend": coluna_texto("vendedor"),
            "sub": coluna_texto("subgrupo"),
            "cli": coluna_texto("cliente"),
            "prod": coluna_texto("produto"),
            "v_bruto": v_bruto.round(2),
            "dev": v_dev.round(2),
            "v_liq": v_liq.round(2),
            "p_liq": p_liq.round(3),
            "v_troca": v_troca.round(2),
            "qtd": q_liq.round(3),
        }
    )

    # --------------------------------------------------------------------------
    # 6. VALIDAÇÃO DAS DATAS GERADAS
    # --------------------------------------------------------------------------

    print("\nVALIDAÇÃO DAS DATAS GERADAS")
    print("-" * 80)
    print(f"Primeira data: {montado['data'].min()}")
    print(f"Última data:   {montado['data'].max()}")
    print(
        "Datas válidas: "
        f"{montado['data'].notna().sum():,} de {len(montado):,}"
    )
    print("-" * 80)

    colunas = list(montado.columns)
    listas = [montado[coluna].tolist() for coluna in colunas]

    records = [
        dict(zip(colunas, valores))
        for valores in zip(*listas)
    ]

    totals = {
        "vendas": round(float(montado["v_bruto"].sum()), 2),
        "devolucoes": round(float(montado["dev"].sum()), 2),
        "trocas": round(float(montado["v_troca"].sum()), 2),
        "liquido": round(
            float(montado["v_bruto"].sum()) - float(montado["dev"].sum()),
            2,
        ),
    }

    return records, totals


# ==============================================================================
# FILTROS E POPULAÇÕES PARA O JSON
# ==============================================================================

def unique_sorted(records, field):
    values = {
        safe_text(record.get(field))
        for record in records
        if safe_text(record.get(field))
    }

    return sorted(
        values,
        key=lambda value: normalize_text(value),
    )


def build_filter_options(records):
    months = sorted(
        {
            record["dt"]
            for record in records
            if record.get("dt")
        }
    )

    return {
        "meses": months,
        "gerentes": unique_sorted(records, "ger"),
        "supervisores": unique_sorted(records, "sup"),
        "cidades": unique_sorted(records, "cid"),
        "fornecedores": unique_sorted(records, "forn"),
        "vendedores": unique_sorted(records, "vend"),
        "subgrupos": unique_sorted(records, "sub"),
    }


def build_population_json_map(populations):
    return {
        city: int(population)
        for city, population in populations.items()
        if population and int(population) > 0
    }


# ==============================================================================
# JSON FATIADO POR TAMANHO (CADA REGISTRO É SERIALIZADO 1 VEZ)
# ==============================================================================

def compact_json_bytes(value):
    text = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )

    return text.encode("utf-8")


def split_records_by_size(records, metadata):
    overhead_primeiro = len(
        compact_json_bytes({"dados": [], **metadata})
    )
    overhead_demais = len(
        compact_json_bytes({"dados": []})
    )

    limite_primeiro = (
        MAX_JSON_BYTES - overhead_primeiro - MARGEM_SEGURANCA_BYTES
    )
    limite_demais = (
        MAX_JSON_BYTES - overhead_demais - MARGEM_SEGURANCA_BYTES
    )

    chunks = []
    atuais = []
    tamanho_atual = 0
    limite_atual = limite_primeiro

    for record in records:
        registro_bytes = compact_json_bytes(record)
        custo = len(registro_bytes) + 1

        if custo > limite_demais:
            raise RuntimeError(
                "Um único registro excede o limite de tamanho do JSON."
            )

        if atuais and tamanho_atual + custo > limite_atual:
            chunks.append(atuais)
            atuais = []
            tamanho_atual = 0
            limite_atual = limite_demais

        atuais.append(registro_bytes)
        tamanho_atual += custo

    if atuais:
        chunks.append(atuais)

    if not chunks:
        chunks = [[]]

    return chunks


def remove_old_json_parts(root):
    patterns = [
        os.path.join(root, "dashboard_data.json"),
        os.path.join(root, "dashboard_data_part*.json"),
    ]

    removed = 0

    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                removed += 1
            except OSError as error:
                print(f"Não foi possível remover {path}: {error}")

    print(f"Arquivos JSON antigos removidos: {removed}")


def write_json_chunks(root, records, filters, populations, totals):
    metadata = {
        "filtros": filters,
        "populacoes": build_population_json_map(populations),
        "totais": totals,
        "total_registros": len(records),
    }

    chunks = split_records_by_size(
        records,
        metadata,
    )

    remove_old_json_parts(root)

    generated_files = []

    for index, chunk_bytes in enumerate(chunks):
        if index == 0:
            filename = "dashboard_data.json"

            metadata_bytes = compact_json_bytes(metadata)

            payload_bytes = (
                b'{"dados":['
                + b",".join(chunk_bytes)
                + b"],"
                + metadata_bytes[1:-1]
                + b"}"
            )

        else:
            filename = f"dashboard_data_part{index + 1}.json"

            payload_bytes = (
                b'{"dados":['
                + b",".join(chunk_bytes)
                + b"]}"
            )

        output_path = os.path.join(root, filename)

        if len(payload_bytes) >= 25 * 1024 * 1024:
            raise RuntimeError(
                f"O arquivo {filename} ficou acima do limite de 25 MB."
            )

        with open(output_path, "wb") as file:
            file.write(payload_bytes)

        size_mb = len(payload_bytes) / (1024 * 1024)

        print(
            f"Gerado: {filename} | "
            f"tamanho: {size_mb:.2f} MB"
        )

        generated_files.append(output_path)

    return generated_files


# ==============================================================================
# RELATÓRIOS DE VALIDAÇÃO
# ==============================================================================

def print_field_samples(records):
    """
    Mostra amostras dos campos críticos para conferência no terminal.
    """

    print("\nAMOSTRAS DE CAMPOS (validação)")
    print("-" * 80)

    for field, label in [
        ("vend", "VENDEDOR"),
        ("sup", "SUPERVISOR"),
        ("prod", "PRODUTO"),
        ("cli", "CLIENTE"),
        ("data", "DATA DIÁRIA"),
    ]:
        values = []
        seen = set()

        for record in records:
            value = safe_text(record.get(field))

            if value and value not in seen:
                seen.add(value)
                values.append(value)

            if len(values) >= 10:
                break

        if values:
            print(f"{label}: {' | '.join(values)}")
        else:
            print(f"{label}: (vazio)")

    print("-" * 80)


def print_month_summary(records):
    month_summary = {}

    for record in records:
        month = record.get("dt")

        if not month:
            continue

        if month not in month_summary:
            month_summary[month] = {
                "registros": 0,
                "v_liq": 0.0,
                "clientes": set(),
            }

        item = month_summary[month]
        item["registros"] += 1
        item["v_liq"] += float(record.get("v_liq") or 0)

        client = record.get("cli") or ""

        if client:
            item["clientes"].add(client)

    print("\nRESUMO POR MÊS")
    print("-" * 80)

    for month in sorted(month_summary):
        item = month_summary[month]

        print(
            f"{month} | "
            f"registros: {item['registros']:,} | "
            f"clientes: {len(item['clientes']):,} | "
            f"faturamento líquido: "
            f"R$ {item['v_liq']:,.2f}"
        )

    print("-" * 80)


def print_vendor_month_validation(records):
    summary = {}

    for record in records:
        vendor = record.get("vend") or ""

        if not vendor:
            continue

        month = record.get("dt") or ""
        value = float(record.get("v_liq") or 0)

        vendor_months = summary.setdefault(vendor, {})
        vendor_months[month] = vendor_months.get(month, 0.0) + value

    print("\nVALIDAÇÃO DE VENDEDORES POR MÊS")
    print("-" * 80)

    for vendor in sorted(summary, key=normalize_text):
        months = sorted(summary[vendor].items())

        text = " | ".join(
            f"{month}: R$ {value:,.2f}"
            for month, value in months
        )

        print(f"{vendor}: {text}")

    print("-" * 80)


# ==============================================================================
# EXECUÇÃO PRINCIPAL
# ==============================================================================

def main():
    print("=" * 80)
    print("CONSOLIDAÇÃO JBS FRIOS")
    print("=" * 80)

    root = ROOT_PADRAO

    if not os.path.isdir(root):
        raise FileNotFoundError(
            f"A pasta do projeto não foi encontrada: {root}"
        )

    print(f"Pasta do projeto: {root}")
    print(f"Limite configurado por JSON: {MAX_JSON_BYTES / (1024 * 1024):.0f} MB")

    input_files = discover_input_files(root)

    if not input_files:
        raise FileNotFoundError(
            "Nenhum arquivo Excel ou CSV de origem foi encontrado."
        )

    df = load_all_input_data(input_files)

    marcar("Leitura dos arquivos concluída")

    if df.empty:
        raise RuntimeError(
            "Os arquivos foram encontrados, mas não possuem registros."
        )

    print(
        f"\nTotal de registros lidos: {len(df):,}"
    )

    records, totals = build_dashboard_records(df)

    marcar("Transformação dos registros concluída")

    if not records:
        raise RuntimeError(
            "Nenhum registro válido foi gerado. "
            "Verifique a coluna Emissão."
        )

    print(
        f"Total de registros válidos: {len(records):,}"
    )

    print("\nTOTAIS CONSOLIDADOS")
    print("-" * 80)
    print(f"Faturamento bruto:       R$ {totals['vendas']:,.2f}")
    print(f"Devoluções abatidas:     R$ {totals['devolucoes']:,.2f}")
    print(f"Faturamento líquido:     R$ {totals['liquido']:,.2f}")
    print(f"Trocas:                  R$ {totals['trocas']:,.2f}")
    print("-" * 80)

    print_field_samples(records)

    populations = build_population_map(root)
    marcar("Populações carregadas")

    filters = build_filter_options(records)
    marcar("Filtros montados")

    print_month_summary(records)
    print_vendor_month_validation(records)

    generated_files = write_json_chunks(
        root=root,
        records=records,
        filters=filters,
        populations=populations,
        totals=totals,
    )

    marcar("Arquivos JSON gravados")

    print("\nPROCESSAMENTO CONCLUÍDO")
    print("-" * 80)
    print(f"Arquivos JSON gerados: {len(generated_files)}")

    for path in generated_files:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(
            f"{os.path.basename(path)}: "
            f"{size_mb:.2f} MB"
        )

    print("-" * 80)
    print(
        f"Tempo total: {time.perf_counter() - INICIO_EXECUCAO:.1f} segundos"
    )


if __name__ == "__main__":
    main()