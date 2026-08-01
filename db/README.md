# DB 작업 경계

`/`에는 런타임 테이블 마이그레이션과 4-table 전환에 필요한 오프라인
컴파일러만 둔다. 런타임 분류는 이 폴더의 JSON/CSV를 직접 읽지 않는다.

## 현재 유지 자산

- `migrate_runtime_assets.py`
  - pre-4-table 런타임 JSON/사전/COI/스모크 정답지를 PostgreSQL 테이블로
    한 번 옮기는 전환 스크립트.
- `run_recompile.sh`
  - 기존 branch sidecar 재컴파일 경계. 4-table 전환이 끝날 때까지 유지.
- `artifacts/`
  - 마이그레이션 입력만 임시 보존. 일반 스모크 결과와 실험 artifact는
    커밋하지 않는다.

## 런타임 자산 마이그레이션

이 프로젝트는 `.env`를 자동으로 읽지 않는다. DB 환경 변수는 실행자가 셸에
직접 주입해야 한다.

```bash
# db 연결 없이 입력 파일·행 수 검증
PYTHONPATH=src python db/migrate_runtime_assets.py --plan

# 실행자가 환경 변수를 주입한 셸에서만 실행
PYTHONPATH=src python db/migrate_runtime_assets.py --apply
PYTHONPATH=src python db/migrate_runtime_assets.py --verify
```

마이그레이션 후 런타임 권위:

- singleton JSON tables:
  `heading_axis_map`, `subheading_axis_map`, `bti_recall_index`,
  `heading_vocab`, `axis_field_binding`, `commodity_taxonomy`,
  `species_taxonomy`
- row tables:
  `std_name_dictionary`, `curated_term_bridge`, `food_type_dictionary`,
  `classification_criterion_taxonomy`, `product_input_dictionary`,
  `coi_form`, `coi_product_map`, `co_form`, `co_product_map`,
  `classification_smoke_case`

`taric_master_table`, `cn_table`, `cn_chapter_index`, BTI 원천 테이블도 런타임
DB에서 직접 조회한다.

## 스모크

분류 스모크 진입점은 루트의 `kurly_market_smoke.py` 하나다. 정답지는
`classification_smoke_case`를 조회하며 로컬 CSV fallback은 없다.

4-table 전환이 완료되면 이 디렉터리의 sidecar 컴파일러와 임시
`/artifacts`도 별도 커밋에서 제거한다.

4-table 전환용 suffix 감사, subheading 백필, condition ledger 생성기는
pre-4-table 기준선 커밋에 포함하지 않고 다음 작업 경계에서 추가한다.
