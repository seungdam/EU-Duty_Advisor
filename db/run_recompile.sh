#!/bin/zsh
# 술어 테이블 재컴파일 러너 — db/에서 컴파일 관련 실행을 일원화.
# 컴파일러 본체는 import 경로 때문에 src/agents/tools/에 남는다.
#
# 기본 = Nomenclature 트리 원천 (db/artifacts/nomenclature_tree.jsonl,
#        suffix 80/10·20 명시 계층 — 경로 추측 없음)
# 사용:
#   ./db/run_recompile.sh              # 트리 원천 + apply
#   ./db/run_recompile.sh --dry-run    # db 반영 없이 산출만
#   ./db/run_recompile.sh --legacy     # 구 방식(cn_table 경로 + enrich-h6)
set -e
cd "$(dirname "$0")/.."

echo "── ASAP_ 환경 오염 점검 ──"
# 변수 '이름만' 출력 — 값 출력 금지 (07-16 실측: .env를 셸에 export한
# 상태에서 이 점검이 API 키 값을 터미널·로그에 노출했다. tee로 로그
# 파일에도 남으므로 값은 절대 찍지 않는다.)
env | grep '^ASAP_' | cut -d= -f1 || echo "(깨끗)"

# zsh는 기본 단어분리를 안 한다 — 문자열 "--tree-source --cnen"이 통짜
# 인자 하나로 전달돼 두 플래그가 전부 무시됐다(07-16 실측: 로그에 '트리
# 원천'·'CNEN 수확' 라인 부재). 배열로 선언해야 인자별로 전달된다.
SOURCE_FLAG=(--tree-source --cnen)
[[ "$*" == *--legacy* ]] && SOURCE_FLAG=(--enrich-h6)
APPLY=(--apply)
[[ "$*" == *--dry-run* ]] && APPLY=()

LOG="DB/artifacts/recompile_$(date +%y%m%d_%H%M).log"
PYTHONPATH=src /opt/anaconda3/envs/asap/bin/python -u -m bussiness_logic.classification.offline.branch_decision_compiler $SOURCE_FLAG $APPLY 2>&1 | tee "$LOG"
echo "── 로그: $LOG ──"
