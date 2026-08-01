from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_SPEC = spec_from_file_location(
    "migrate_runtime_assets",
    Path(__file__).resolve().parents[1] / "db/migrate_runtime_assets.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_BuildSmokeCaseValues = _MODULE._BuildSmokeCaseValues
_LoadFormDirectory = _MODULE._LoadFormDirectory


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, parameters: object = None) -> None:
        self.statements.append(statement)

    def executemany(self, statement: str, parameters: object) -> None:
        self.statements.append(statement)


def test_smoke_case_loader_uses_current_korean_headers() -> None:
    values = _BuildSmokeCaseValues(
        [
            {
                "품목 분류 명칭": "낙지 볶음",
                "상품 상세": "https://example.test/products/1",
                "EU HS CODE": "1605.55.00.00",
            }
        ],
        "source-hash",
    )

    assert values == [
        (
            "낙지 볶음",
            "https://example.test/products/1",
            "1605550000",
            "source-hash",
        )
    ]


def test_form_loader_truncates_fk_tables_in_one_statement(tmp_path: Path) -> None:
    cursor = _RecordingCursor()

    _LoadFormDirectory(
        cursor,
        root=tmp_path,
        formTable="coi_form",
        mapTable="coi_product_map",
        mapColumn="coi_file",
    )

    assert cursor.statements[0] == "TRUNCATE coi_product_map, coi_form"
