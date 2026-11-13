"""Common bounded CSV framing, independent of the entities being imported."""
import csv
from io import StringIO
from django.utils.translation import gettext_lazy
from common.services.operations import json_value

MAX_DATA_ROWS = 5000  # G20


def read_source(record, required, optional):
    rows, errors = [], []
    try:
        text = bytes(record.source).decode("utf-8-sig")
        # G20: the separator is read from the header line, which never contains free text.
        delimiter = ";" if ";" in text.split("\n", 1)[0] else ","
        reader = csv.reader(StringIO(text, newline=""), delimiter=delimiter, strict=True)
        header = next(reader, [])
        if not required.issubset(header) or set(header) - (required | optional) or len(header) != len(set(header)):
            errors.append({"code": "invalid_header", "detail": gettext_lazy("En-tête CSV invalide. Utiliser le modèle fourni.")})
        else:
            while True:
                line = reader.line_num + 1
                values = next(reader, None)
                if values is None:
                    break
                if not values:
                    continue
                if len(rows) == MAX_DATA_ROWS:
                    errors.append({"code": "too_many_rows", "detail": gettext_lazy("Limiter chaque fichier à 5000 lignes de données.")})
                    break
                if len(values) != len(header):
                    rows.append({"line": line, "data": values, "errors": {"row": gettext_lazy("Nombre de colonnes incorrect.")}})
                else:
                    rows.append({"line": line, "data": dict(zip(header, values)), "errors": {}})
    except (UnicodeDecodeError, csv.Error):
        errors.append({"code": "invalid_csv", "detail": gettext_lazy("Fichier CSV UTF-8 invalide.")})
    if not rows and not errors:
        errors.append({"code": "empty_data", "detail": gettext_lazy("Ajouter au moins une ligne de données au modèle.")})
    return rows, errors


def preview_result(record, rows, candidates, file_errors):
    return json_value({"id": str(record.id), "kind": record.kind, "source_sha256": record.source_sha256,
        "applied": record.applied_operation_id is not None, "row_count": len(rows), "valid_count": len(candidates),
        "has_errors": bool(file_errors or any(row["errors"] for row in rows)), "file_errors": file_errors, "rows": rows}), candidates
