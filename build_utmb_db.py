"""
build_utmb_db.py

Transforma la tabla plana de resultados de UTMB (CSV) en una base de datos
SQLite normalizada con 3 tablas: corredores, carreras, resultados.

Columnas esperadas en el CSV de origen:
General_Rank, time, given_name, last_name, nationality, gender,
age_category, status, race_category, race_name, distance_km,
elevation_gain, year

Uso:
    pip install pandas
    python build_utmb_db.py
"""

import sqlite3
import pandas as pd

# --- Configuración ---
CSV_PATH = r"C:\Users\d1ego\OneDrive\Escritorio\Proyectos\UTMB Proyecto\Norm\utmb_pv_24.csv"   # tu archivo plano de origen
DB_PATH = "utmb.db"         # base de datos SQLite de salida


REQUIRED_COLUMNS = {
    "general_rank", "time", "given_name", "last_name", "nationality",
    "gender", "age_category", "status", "race_category", "race_name",
    "distance_km", "elevation_gain", "year",
}


def load_and_clean(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    # Normaliza encabezados: quita espacios extremos, pasa a minúsculas y
    # sustituye espacios internos por "_", para no depender de cómo vino
    # exportado el CSV (Elevation_Gain, "elevation gain", etc.)
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise KeyError(
            f"Faltan columnas esperadas: {sorted(missing)}. "
            f"Columnas encontradas en el CSV: {df.columns.tolist()}"
        )

    text_cols = [
        "given_name", "last_name", "nationality", "gender",
        "race_name", "race_category", "status", "age_category",
    ]
    for col in text_cols:
        # fillna ANTES del cast: en pandas >= 2.x con el dtype "str" por
        # default, astype(str) ya NO convierte NaN al texto "nan" (a
        # diferencia del dtype "object" clásico) — lo deja como nulo real,
        # lo cual rompía la detección de anónimos más abajo.
        df[col] = df[col].fillna("").astype(str).str.strip()

    # Corredores anónimos (given_name/last_name vacío, ej. "Anonymous"):
    # no hay dato que permita saber si dos apariciones así son la misma
    # persona o personas distintas. Se tratan como corredores separados
    # -marcando cada uno con un last_name sintético único por fila- en
    # vez de fusionarlos bajo la misma llave natural.
    anon_mask = df["last_name"] == ""
    df.loc[anon_mask, "last_name"] = [f"__anon_{i}" for i in df.index[anon_mask]]

    return df


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TABLE IF EXISTS resultados;
        DROP TABLE IF EXISTS carreras;
        DROP TABLE IF EXISTS corredores;

        CREATE TABLE corredores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            given_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            nationality TEXT,
            gender TEXT
        );

        CREATE TABLE carreras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_name TEXT NOT NULL,
            race_category TEXT,
            distance_km REAL,
            elevation_gain REAL,
            year INTEGER NOT NULL,
            -- race_category va en la UNIQUE porque un mismo race_name
            -- (ej. "UTMB Puerto Vallarta") agrupa varias distancias en el
            -- mismo año (100M, 100K, 50K, ...): cada una es una "carrera"
            -- distinta.
            UNIQUE (race_name, race_category, year)
        );

        CREATE TABLE resultados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corredor_id INTEGER NOT NULL REFERENCES corredores(id),
            carrera_id INTEGER NOT NULL REFERENCES carreras(id),
            general_rank INTEGER,
            time TEXT,
            status TEXT,
            age_category TEXT,
            UNIQUE (corredor_id, carrera_id)
        );
    """)
    conn.commit()


def populate_corredores(
    conn: sqlite3.Connection, df: pd.DataFrame
) -> list[int]:
    """
    Inserta UN corredor por cada fila del CSV.

    Importante:
    - No se usa drop_duplicates().
    - No existe una restricción UNIQUE sobre nombre/apellido/nacionalidad.
    - Cada fila del archivo de origen representa una persona distinta,
      aunque comparta nombre con otra persona.
    - Regresamos los IDs en el mismo orden que las filas de df para que
      resultados pueda conservar la correspondencia exacta.
    """
    cur = conn.cursor()
    corredor_ids = []

    for record in df.itertuples(index=False):
        cur.execute(
            """INSERT INTO corredores
               (given_name, last_name, nationality, gender)
               VALUES (?, ?, ?, ?)""",
            (
                record.given_name,
                record.last_name,
                record.nationality,
                record.gender,
            ),
        )
        corredor_ids.append(cur.lastrowid)

    conn.commit()
    return corredor_ids


def populate_carreras(conn: sqlite3.Connection, df: pd.DataFrame) -> None:
    carreras = df[
        ["race_name", "race_category", "distance_km", "elevation_gain", "year"]
    ].drop_duplicates()
    conn.executemany(
        """INSERT OR IGNORE INTO carreras
           (race_name, race_category, distance_km, elevation_gain, year)
           VALUES (?, ?, ?, ?, ?)""",
        carreras.itertuples(index=False, name=None),
    )
    conn.commit()


def populate_resultados(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    corredor_ids: list[int],
) -> None:
    cur = conn.cursor()

    cur.execute("SELECT id, race_name, race_category, year FROM carreras")
    carrera_map = {(r[1], r[2], r[3]): r[0] for r in cur.fetchall()}

    rows = []

    for corredor_id, record in zip(corredor_ids, df.itertuples(index=False)):
        carrera_id = carrera_map[
            (record.race_name, record.race_category, record.year)
        ]

        # general_rank puede venir vacío en DNF/DSQ; pandas lo sube a float con NaN.
        rank = record.general_rank
        rank = None if pd.isna(rank) else int(rank)

        rows.append(
            (
                corredor_id,
                carrera_id,
                rank,
                record.time,
                record.status,
                record.age_category,
            )
        )

    cur.executemany(
        """INSERT INTO resultados
           (corredor_id, carrera_id, general_rank, time, status, age_category)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )

    conn.commit()


def main() -> None:
    df = load_and_clean(CSV_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)

        corredor_ids = populate_corredores(conn, df)
        populate_carreras(conn, df)
        populate_resultados(conn, df, corredor_ids)

        # Validación: debe haber exactamente un corredor y un resultado
        # por cada fila del archivo plano.
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM corredores")
        total_corredores = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM resultados")
        total_resultados = cur.fetchone()[0]

        total_origen = len(df)

        if total_corredores != total_origen:
            raise RuntimeError(
                f"Se esperaban {total_origen} corredores, "
                f"pero se insertaron {total_corredores}."
            )

        if total_resultados != total_origen:
            raise RuntimeError(
                f"Se esperaban {total_origen} resultados, "
                f"pero se insertaron {total_resultados}."
            )

        print(f"Filas del CSV:      {total_origen}")
        print(f"Corredores:         {total_corredores}")
        print(f"Resultados:         {total_resultados}")
    finally:
        conn.close()

    print(f"Base de datos creada en: {DB_PATH}")


if __name__ == "__main__":
    main()
