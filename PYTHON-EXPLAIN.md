# 폴더명 변경과 런타임 경로 의존성

## 짧은 맥락
`webapp` 폴더를 `frontend`로 바꿔도 되는지 확인할 때는 "소스가 React냐"보다 "Python/문서/ignore 규칙이 그 경로를 직접 참조하느냐"를 먼저 봐야 한다.

## 핵심 개념
폴더명 변경은 타입명 변경이 아니라 파일 시스템 주소 변경이다. 따라서 import alias와 달리, 엔트리포인트나 빌드 출력 경로를 문자열로 잡고 있으면 런타임이 바로 깨질 수 있다.

## 프로젝트 함의
이 저장소에서는 `asap_app.py`가 `ASAP_ROOT / "webapp" / "dist"`를 직접 넘긴다. 즉 현재 단일 프로세스 실행은 `webapp/dist`를 전제로 한다. 또한 `.gitignore`는 `!webapp/src/lib/` 예외를 두고 있어 폴더명을 바꾸면 새 `frontend/src/lib` 파일이 다시 무시될 수 있다.

## 최소 예시
```python
app = CreateBackendApp(
    webappDistDir=ASAP_ROOT / "webapp" / "dist",
)
```

이 경우 폴더를 `frontend`로 바꾸면 최소한 아래처럼 같이 바꿔야 한다.

```python
app = CreateBackendApp(
    webappDistDir=ASAP_ROOT / "frontend" / "dist",
)
```

## 관련 파일
- `asap_app.py`
- `src/backend/app.py`
- `.gitignore`
- `webapp/README.md`

# 질문 기반 단계 분류 상태 머신

## 짧은 맥락
Composition Lane의 값이 `unknown`이면 분류기가 무조건 임의 코드를 고르지 않고, 필요한 경우 사용자 질문을 만들어 답변 후 Classification을 다시 실행한다. 분류가 해결되면 Document 단계도 이어서 실행한다.

## 핵심 개념
현재 HS4/HS6 판정은 WPF의 상태 머신과 비슷하다.

```text
ProductUnderstanding DTO
-> 축별 canonical field binding
-> 조건별 true / false / undecided
-> confirmed / violated / needs_more_facts
-> 사용자 yes / no / unknown
-> Classification 단계 replay
```

`unknown`은 오답이 아니라 증거 부재다. 축에 따라 Composition 값이 비면 Identity Lane으로 폴백할 수 있으므로, 모든 `unknown`이 질문으로 변환되는 것은 아니다. 법적 형제 분기에서 확정 가능한 후보가 없을 때 질문이 생성된다.

활성 질문이 있는 `needs_more_facts`는 backend job의 `awaiting_input` 상태가 된다. worker thread를 잡아두지 않고 Blackboard와 API snapshot을 저장한 뒤 요청을 종료한다.

## 프로젝트 함의
- HS4와 HS6은 결정 테이블과 canonical field를 사용하는 3값 판정 대상이다.
- 질문은 안정적인 `question_key`로 후보·단계·축·필드에 귀속된다.
- 답변은 `_classification_answer_facts` overlay로 적용되며 원 ProductUnderstanding DTO를 수정하지 않는다.
- 백엔드는 `POST /api/runs/{job_id}/question-answers`로 Classification을 재실행하고, 해결되면 Document 단계까지 진행한다.
- 동일 답변 재전송은 새 answer fact를 만들지 않는다. `unknown` 질문은 활성 상태를 유지하며 이후 `yes/no`로 교체할 수 있다.
- 질문 계약 V2는 predicate `op`를 키와 answer fact에 보존하여 `not_contains`의 yes/no 극성을 올바르게 해석한다.
- CN8은 현재 `lexical_cn8_temporary`이므로 같은 질문형 확정 구조가 아직 아니다.
- 커밋 `537cc76`에는 React 질문 UI가 있었지만 현재 HEAD에서는 병합 후 연결 코드가 사라진 상태다.
- `not_contains`도 사용자에게는 긍정형 포함 질문을 제시하며, `yes`는 원 술어 위반, `no`는 충족으로 변환한다.
- 현재 UI에서 상위 Stepper 2는 `품목 분류`, 그 내부 Stepper 3은 `계층 분류`다. HS4/HS6 질문은 이 위치에 표시해야 한다.
- 목표 pause는 worker thread를 유지하는 방식이 아니라 `awaiting_input` 상태와 Blackboard를 저장하고 요청을 종료한 뒤, 답변 API로 재개하는 논리적 일시정지가 적합하다.

## 최소 예시
```json
{
  "answers": [
    {"user_question_id": "uq_001", "answer": "yes"}
  ]
}
```

## 관련 파일
- `src/bussiness_logic/classification/rules/axis_field_binding.py`
- `src/bussiness_logic/classification/rules/question_contract.py`
- `src/bussiness_logic/classification/services/staged_classification.py`
- `src/backend/pipeline_service.py`
- `webapp/src/hooks/useClassificationRun.js`

# Conda 환경과 Vite 의존성 경계

## 짧은 맥락
Python 백엔드와 React 프런트엔드는 서로 다른 패키지 관리자가 재현한다.

## 핵심 개념
`environments.yml`은 `asap_pw`의 Python 실행 환경을 선언한다. React 소스와 Vite 빌드 도구는 `webapp/package.json` 및 `package-lock.json`이 선언하며, `npm ci`가 `node_modules`를 복원하고 `npm run build`가 `dist`를 생성한다.

## 프로젝트 함의
`webapp/src/lib`는 팀이 작성한 JavaScript 소스이므로 Vite가 자동 생성하지 않는다. 반면 `webapp/node_modules`와 `webapp/dist`는 각각 설치·빌드 결과이므로 직접 복구하거나 Git에 포함하지 않는다.

## 최소 예시
```powershell
conda env create -f environments.yml
$env:PYTHONPATH = "src"
conda run -n asap_pw python -m pytest -q
cd webapp
npm ci
npm run build
```

현재 저장소는 `src` layout을 pytest 설정에 선언하지 않았으므로 `PYTHONPATH=src` 없이 루트에서 pytest를 실행하면 테스트 수집이 실패한다.

Windows에서 CMD만 `conda`를 찾고 PowerShell은 찾지 못하면 CMD에서
`conda init powershell`을 한 번 실행한 뒤 PowerShell을 다시 연다. 프로필
변경을 원하지 않으면 `cmd /c where conda`로 찾은 실행 파일을 PowerShell의
호출 연산자 `&`로 직접 실행할 수 있다.

## 관련 파일
- `environments.yml`
- `webapp/package.json`
- `webapp/package-lock.json`
- `webapp/vite.config.js`

# Interaction UI 정합성

## 짧은 맥락
HS4/HS6 분류 질문이 생성되면 run은 `awaiting_input`으로 정지하고 React Step 3에서 응답을 받는다.

## 핵심 개념
질문 대기는 worker thread를 유지하는 blocking 상태가 아니라 아래 상태 머신으로 표현해야 한다.

```text
running -> awaiting_input -> running -> completed
                         \-> awaiting_input
                         \-> failed
```

## 프로젝트 함의
- Supabase는 HS/CN branch, predicate, decision, taxonomy 같은 분류 기준 데이터 저장소다.
- 질문, 답변, run 상태는 현재 Supabase가 아니라 Blackboard JSON과 `RunRegistry`가 소유한다.
- HS4/HS6/CN8 branch와 predicate/decision 테이블은 PK/FK/UNIQUE/CHECK가 없고 대부분 nullable이다.
- taxonomy 자산 테이블은 `asset_version` PK와 NOT NULL 계약이 있다.
- 관련 `public` 테이블은 RLS가 꺼져 있다. 현재 확인한 `anon`/`authenticated` 실효 권한은 없지만 grant 변경 시 방어층이 없으므로 React가 Supabase에 직접 접근해서는 안 된다.
- 질문과 답변의 소유권은 Blackboard에 있고 Supabase 스키마는 수정하지 않았다.
- React는 Supabase에 직접 접근하지 않고 Python API만 호출한다.
- `unknown`은 미해결 상태를 유지하며 UI는 시연 범위에 맞춰 `yes/no`만 제공한다.

## 관련 파일
- `src/bussiness_logic/classification/rules/question_contract.py`
- `src/bussiness_logic/classification/services/staged_classification.py`
- `src/backend/pipeline_service.py`
- `webapp/src/hooks/useClassificationRun.js`
- `webapp/src/pages/WorkbenchPage.jsx`

# 기존 플로우와 Interaction 추가 경계

## 짧은 맥락
현재 질문 데이터는 만들어지지만 pipeline과 UI는 이를 대기 상태로 해석하지 않는다.

## 핵심 개념
```text
현재:
HS4/HS6 unresolved -> 질문 기록 -> Classification 성공
-> Document 건너뜀 -> completed

목표:
HS4/HS6 unresolved -> 질문 계약 확정 -> awaiting_input
-> Step 2 / 내부 Step 3에서 yes/no
-> Classification replay
-> 미해결이면 다시 awaiting_input
-> 해결이면 CN8 -> Document -> completed
```

## 프로젝트 함의
- 질문 계약에는 predicate `op`와 계약 버전이 필요하다.
- 실패와 사용자 입력 대기는 서로 다른 pipeline 종료 사유여야 한다.
- 답변 제출은 active 질문에 대해 중복 적용되지 않아야 한다.
- `awaiting_input` snapshot도 재시작 후 복원되어야 한다.
- React는 기존 hook과 Stepper를 유지하고 질문 패널과 제출 함수만 추가한다.
- Interaction 상태를 위한 신규 Supabase 테이블, Supabase JS, 별도 상태 관리 라이브러리는 현재 필요하지 않다.

# 웹 UI URL 실행 경계

## 짧은 맥락
웹 UI는 모든 `http://`·`https://` URL을 입력받지만, 실제 상품 페이지 수집기는 Kurly 상품 URL만 지원한다.

## 핵심 개념
입력 검증과 수집기 지원 범위는 다르다.

```text
UI URL 형식 검사
-> POST /api/runs
-> Kurly 지원 URL이면 Playwright/OCR 수집
-> 그 외 URL이면 수집 생략
-> 준비된 facts로 분류 진행
```

## 프로젝트 함의
- 지원 경로는 `kurly.com/goods/*`, `kurlyglobal.com/products/*`, `kurlyglobal.com/en/products/*`다.
- 지원하지 않는 URL은 범용 크롤링되지 않는다.
- URL만 입력한 비지원 링크는 URL 문자열 자체가 상품명 fallback으로 분류 입력에 들어갈 수 있다.
- Kurly 수집 실패도 pipeline 예외가 아니라 warning으로 변환되므로, 이후 단계가 실행됐다는 사실이 상품 facts 수집 성공을 의미하지 않는다.
- `상품 정보만 복원`은 신규 URL 수집이 아니라 기존 캐시 artifact의 reconstruction 재실행이다.

## 관련 파일
- `webapp/src/features/classification/model/classificationInput.js`
- `webapp/src/hooks/useClassificationRun.js`
- `src/bussiness_logic/product/pipeline/kurly_product_facts.py`
- `src/bussiness_logic/classification/pipeline/raw_input.py`

# Windows dotenv와 프로세스 환경변수

## 짧은 맥락
Windows도 `.env` 같은 점으로 시작하는 파일명을 지원한다. 별도의 Windows용
확장자는 필요하지 않으며, 파일명이 아니라 애플리케이션이 그 파일을 실제로
읽는지가 중요하다.

## 핵심 개념
`ASAP_ENV_FILE`은 파일 경로를 담는 환경변수일 뿐 파일 내용을 자동으로
프로세스 환경에 등록하지 않는다. 현재 `asap_app.py`는 이 경로를 설정하지만,
DB 모듈의 `_load_project_dotenv()`는 정의만 되어 있고 호출되지 않는다.

## 프로젝트 함의
현재 DB 설정은 실행 프로세스에 이미 존재하는
`ASAP_CLASSIFICATION_DATABASE_URL`, `PG*`, `ASAP_DATABASE_URL`,
`DATABASE_URL` 순서로 결정된다. 따라서 PowerShell에서 `$env:...`를 설정한
동일 세션에서 백엔드를 실행하면 dotenv 파일명과 무관하게 연결할 수 있다.

## 최소 예시
```powershell
$env:ASAP_DATABASE_URL = "postgresql+psycopg2://..."
conda run --no-capture-output -n asap_pw python .\asap_app.py
```

CMD에서는 dotenv를 자동으로 읽지 않으므로 현재 세션에 먼저 적재한다.

```bat
for /f "usebackq eol=# tokens=1,* delims==" %A in (".env.asap_app") do @set "%A=%~B"
conda run --no-capture-output -n asap_pw python .\asap_app.py
```

## 관련 파일
- `asap_app.py`
- `src/db/db_session_manager.py`

# 웹 애니메이션과 reduced motion

## 짧은 맥락
React/Vite의 CSS 및 Motion 애니메이션은 Windows에서도 지원된다. 다만
브라우저는 Windows 접근성 설정의 애니메이션 감소 요청을
`prefers-reduced-motion`으로 웹 애플리케이션에 전달한다.

## 핵심 개념
CSS media query와 `motion/react`의 `useReducedMotion()`은 같은 운영체제
신호를 사용한다.

```javascript
window.matchMedia("(prefers-reduced-motion: reduce)").matches
```

## 프로젝트 함의
이 값이 `true`이면 전역 CSS가 animation과 transition을 사실상 즉시
종료하고, 소개 화면은 5.5초 자동 단계 전환도 중단한다. 값이 `false`인데
Flask 통합 실행에서만 애니메이션이 없다면 `webapp/dist`가 오래된 상태인지
확인하고 `npm run build`를 다시 실행한다.

## 관련 파일
- `webapp/src/styles/base.css`
- `webapp/src/styles/introduction.css`
- `webapp/src/components/introduction/HeroStorySequence.jsx`
- `webapp/src/pages/WorkbenchPage.jsx`

# Staged classifier 오류 래핑과 관측성

## 짧은 맥락
`staged_classifier_unavailable`은 구체적인 예외명이 아니라 staged 분류가
후보를 반환하지 못했을 때 사용하는 최종 축약 상태다.

## 핵심 개념
`ClassificationComponent`는 staged 비활성화, Product Understanding 부재,
내부 예외, 후보 0건, 유효 CN8 0건을 같은 상태로 변환한다. 내부 원문은
`self.reason()`에만 추가되지만 현재 `ComponentRun` artifact에는 저장되지
않는다.

## 프로젝트 함의
`job_edf8bdead7`은 수집, 복원, Product Understanding, HS2 routing까지
완료했으므로 입력 부재와 routing DB 부재는 배제된다. Classification은
`cn_table`, `hs4_branch_index`, `hs6_branch_index`, `cn8_branch_index`를
추가로 요구한다. 실행 프로세스가 classification DB 대신 다른 DB를
선택했는지 먼저 확인해야 하며, 연결이 맞다면 동일 Blackboard 입력으로
`StagedClassificationTool.classify()`를 직접 재생해 감춰진 예외를 확인한다.

## 관련 파일
- `src/bussiness_logic/classification/components/classification.py`
- `src/bussiness_logic/classification/services/staged_classification.py`
- `src/db/db_session_manager.py`

# 컨테이너 배포와 파일 I/O 경계

## 짧은 맥락
Docker와 Kubernetes의 로컬 파일 시스템은 Pod마다 분리되고 교체 시 사라질 수
있다. 따라서 사용자 상태와 실행 상태를 프로젝트 상대경로에 저장하면 다중
Pod, 재시작, rolling update에서 같은 job을 찾지 못한다.

## 핵심 개념
파일 사용 자체가 문제가 아니라 저장 책임이 문제다.

- 설정: 시작 시 한 번 읽는 versioned config 또는 환경변수
- 영속 상태: PostgreSQL
- 이미지·업로드·대형 산출물: S3 호환 object storage
- 계산 중 임시 파일: `tempfile` 아래에서 생성 후 삭제
- React 빌드 산출물: Nginx 또는 CDN

Kubernetes는 상태 저장소가 아니라 프로세스 배치·복구 도구다.

## 게임 서버 관점의 외부화
현재 `RunRegistry`는 dedicated server 내부의
`std::unordered_map<JobId, RunState>`와 같다. `blackboard.json`은 실행 파일
옆에 저장한 save file과 같다. 프로세스가 종료되거나 다른 replica가 요청을
받으면 같은 상태를 보장하지 못한다.

## Pod 의미
Pod는 Kubernetes가 배치·감시·교체하는 최소 실행 단위다. 보통 application
container 하나를 담지만, 같은 수명과 localhost 통신이 필요한 sidecar container를
함께 담을 수도 있다. Pod 내부 container는 하나의 Pod IP와 volume을 공유할 수
있다.

게임 서버 관점에서는 Pod가 dedicated server instance에 가깝고, container는 그
instance 안에서 실행되는 server executable에 가깝다. Node는 해당 instance를
구동하는 물리 서버 또는 VM, Deployment는 원하는 instance 개수를 유지하는
관리자, Service는 교체되는 Pod 앞의 고정 접속 주소다.

Pod는 영구 머신이 아니다. 교체되면 이름과 IP가 바뀔 수 있으며 `emptyDir`와
container writable layer에 둔 데이터도 영속 상태로 취급할 수 없다. PVC나 외부
DB가 별도 수명을 제공한다.

외부화는 authoritative state의 소유자를 바꾸는 작업이다.

- API process: 입력 검증과 상태 조회만 담당하는 gateway
- PostgreSQL: run state, event, 질문 답변의 authoritative owner
- Worker: DB에서 job을 claim해 pipeline을 실행하는 job system
- Object storage: OCR 이미지, 업로드, 대형 산출물의 asset depot
- Container local disk: 재생성 가능한 cache와 임시 scratch만 보관

PVC에 기존 JSON 파일을 그대로 올리는 것은 영속화지만 완전한 외부화는 아니다.
파일은 살아남아도 다중 writer, locking, 상태 전이 원자성, replica 간 event 전달
문제가 남는다.

## 상태 전이 예시
`POST /api/runs`는 `queued` row를 생성한다. Worker는 짧은 transaction으로
job 하나를 claim하고 `running`으로 바꾼다. LLM/OCR 실행 중에는 DB transaction을
열어두지 않는다. 진행 event는 순번과 함께 append한다. 질문이 생기면
`awaiting_input`으로 저장한다. 답변 API는 `(run_id, question_id)` unique key로
답변을 저장하고 job을 다시 queue한다. Worker crash 시 lease가 만료된 job만
재시도한다.

핵심 상태는 typed column으로 두고, 진화가 빠른 Blackboard snapshot만 schema
version을 포함한 JSONB로 둘 수 있다. JSONB snapshot은 cache 또는 resume 입력이며
run status와 질문 답변의 authoritative schema를 대체하지 않는다.

## 프로젝트 함의
현재 `RunRegistry`는 프로세스 메모리, Blackboard와 API snapshot은 JSON 파일에
저장된다. 이 핵심 run state 구조에서는 replica를 2개 이상 둘 수 없다. `asap_app.py`와
`ocr_reconstruction_smoke.py`도 아직 이동 전 루트 config 경로를 직접 만든다.

안전한 전환 순서는 다음과 같다.

1. run, event, 질문 답변을 PostgreSQL repository로 이동한다.
2. OCR 이미지와 대형 pipeline 산출물을 object storage adapter로 이동한다.
3. web process의 daemon thread를 DB queue 기반 worker로 분리한다.
4. Vite 정적 파일은 frontend container가 제공하고 `/api`만 Python으로 전달한다.
5. 그 후 API와 worker replica를 늘린다.

## 관련 파일
- `asap_app.py`
- `ocr_reconstruction_smoke.py`
- `src/backend/app.py`
- `src/backend/pipeline_service.py`
- `src/bussiness_logic/pipeline/blackboard/store.py`

# 게임·데스크톱 개발 경험의 웹 아키텍처 전환

## 짧은 맥락
게임 엔진과 WPF 경험은 상태 머신, ownership, resource lifecycle, component
분리, 비동기 job 설계에 직접 활용된다. 웹 서비스에서는 여기에 분산 상태,
인증·인가, 무중단 배포, 관측성이 추가된다.

## 프로젝트 함의
현재 구현은 단일 process vertical slice로서는 강하다. Pipeline 단계, 질문
pause/resume, DTO projection, 회귀 테스트가 분리돼 있다. 반면 process-local
registry, daemon thread, 로컬 artifact, 인증 부재, CI/CD 부재 때문에 production
multi-replica service로는 미완성이다.

게임 관점으로는 engine/gameplay architecture는 갖췄지만 dedicated server의
authoritative persistence, matchmaking gateway, operations layer가 아직 없는
상태에 가깝다.

# HTTP API 계약과 packet_type

## 짧은 맥락
`PacketHeader::opcode` 예시는 Winsock 기반 custom binary protocol과 비교하기 위한 설명이다.

## 핵심 개념
HTTP 요청은 `method + path`, 응답은 `status code + body schema`, SSE는 `event:` 이름이 message type을 이미 표현한다.

## 프로젝트 함의
현재 ASAP HTTP API에 `opcode` 또는 `packet_type` field를 추가하지 않는다. 같은 정보를 JSON에 다시 넣으면 계약만 중복되고 불일치 가능성이 생긴다.

## 관련 파일
- `SERVER-EXPLAIN.md`
- `src/backend/app.py`

# Enterprise 제거 이후 서버 작업 순서

## 짧은 맥락
목업 UI와 API를 제거했으므로 이제 핵심 Pipeline 실행 상태를 배포 가능한 구조로 바꿔야 한다.

## 핵심 개념
컨테이너화보다 상태 소유권 정리가 먼저다. 다만 큰 리팩터링 전에 현재 통과하는 테스트를 CI로 고정한다.

## 프로젝트 함의
1. Python·Node 테스트와 Vite build를 GitHub Actions에 등록한다.
2. `run`, `event`, 질문 답변의 PostgreSQL schema를 설계하되 승인 전 DB는 수정하지 않는다.
3. `RunRegistry`와 SSE buffer를 영속 저장소로 교체한다.
4. daemon thread를 worker로 분리한다.
5. 인증·인가와 artifact storage를 정리한 뒤 Docker, 마지막에 Kubernetes를 적용한다.

## 관련 파일
- `src/backend/pipeline_service.py`
- `src/bussiness_logic/pipeline/blackboard/store.py`
- `webapp/package.json`
- `environments.yml`

# CPython GIL과 현재 서버 Thread

## 짧은 맥락
Python은 OS thread를 지원하지만 현재 CPython 3.12의 GIL은 한 process에서 순수 Python bytecode의 동시 병렬 실행을 제한한다.

## 핵심 개념
Thread는 DB·HTTP·SSE 같은 blocking I/O concurrency에는 유효하다. GIL은 application invariant를 보호하지 않으므로 공유 상태에는 별도 lock이나 transaction이 필요하다.

## 프로젝트 함의
현재 서버는 job별 daemon thread, `RunRegistry` Condition, 전역 answer/OCR lock, DB query slot을 사용한다. 단일 process 시연에는 유효하지만 lock과 registry가 process-local이므로 다중 worker나 Pod에는 확장되지 않는다.

## 관련 파일
- `SYNCH-MULTITHREAD-EXPLAINN.md`
- `src/backend/pipeline_service.py`

# 고정된 분류 정책과 환경변수 제거

## 짧은 맥락
`ASAP_STAGED_USE_LLM_SELECT=0`과 `ASAP_STAGED_VALIDATOR=1`을 배포마다 바꾸지 않으므로 runtime 설정에서 제거했다.

## 핵심 개념
HS4/HS6/CN8 단계 선택은 점수 순위 기반의 결정론적 `_select_keep()`만 사용한다. 사용되지 않던 LLM selector와 adapter 전달 경로는 삭제했다. 최종 validator는 항상 실행하지만 기존 정책대로 분류 결과를 변경하지 않고 불일치 신호만 기록한다.

## 프로젝트 함의
환경값 누락이나 오입력으로 실행 정책이 달라지지 않는다. LLM selector profile 생성도 사라져 불필요한 runtime 의존성이 줄었다.

## 관련 파일
- `src/bussiness_logic/classification/services/staged_classification.py`
- `src/bussiness_logic/classification/components/classification.py`
- `src/bussiness_logic/classification/pipeline/hs_code_classification_pipeline.py`

# React-Flask와 Kubernetes 런타임 설정

## 짧은 맥락
개발 환경에서는 Vite가 `/api`를 Flask `:8060`으로 proxy한다. 배포 빌드에서는 Flask가 `webapp/dist`와 `/api`를 같은 origin에서 제공한다.

## 핵심 개념
Kubernetes `Secret`은 backend Pod의 process environment로 주입한다. 비밀이 아닌 TOML은 `ConfigMap`으로 mount한다. `VITE_*` 값은 React build 결과에 포함되는 공개 설정이므로 API key나 DB URL을 넣으면 안 된다. GitHub Actions가 사용하는 DB writer 자격증명도 K8s Secret이 아니라 GitHub Actions Secret으로 별도 관리한다.

## 프로젝트 함의
현재 `asap_app.py`는 설정 파일 이동 후에도 루트의 `.appconfig.asap_app.toml`을 지정한다. container에서는 `ASAP_APP_CONFIG_PATH`를 존중하고 기본값을 `config/.appconfig.asap_app.toml`로 바꿔야 한다. K8s가 주입한 환경변수는 `LoadEnvironmentFile()`이 덮어쓰지 않으므로 `.env` 파일을 image나 Pod에 복사할 필요가 없다. 현재 `RunRegistry`와 background job이 process memory에 있으므로 첫 배포는 worker 1개, replica 1개가 안전하다.

## 관련 파일
- `asap_app.py`
- `src/bussiness_logic/app_config.py`
- `src/backend/app.py`
- `src/backend/pipeline_api.py`
- `webapp/src/lib/api.js`
- `webapp/vite.config.js`

# Flask 현재 구조와 LangGraph 목표

## 짧은 맥락
현재 backend는 FastAPI가 아니라 Flask이며, LangGraph도 설치되거나 실행되지 않는다.

## 핵심 개념
HTTP framework는 route mapping과 request 처리를 제공할 뿐 race condition, rate limit, worker 수명, CPU/GPU 병렬성을 자동 해결하지 않는다. REST는 protocol이 아니라 HTTP resource 설계 style이다.

## 프로젝트 함의
현재 Pipeline은 job 간 thread concurrency만 있고 한 job 내부 step과 OCR image, Product Understanding lane은 순차 실행된다. LangGraph 도입 시 node는 `blackboard.json` 전체를 동시에 수정하지 않고 typed state delta를 반환해야 한다. 질문 흐름은 `classify → interrupt → answer → classify` cycle로 표현할 수 있다.

## 관련 파일
- `SERVER-EXPLAIN.md`
- `SYNCH-MULTITHREAD-EXPLAINN.md`
- `src/bussiness_logic/pipeline/pipeline_manager.py`
- `src/bussiness_logic/product/components/product_understanding.py`
- `src/bussiness_logic/pipeline/blackboard/store.py`
- `src/backend/pipeline_api.py`
- `src/db/db_session_manager.py`

# Python bytecode와 native object file

## 짧은 맥락
Python bytecode는 source를 compile한 결과지만 CPU assembly나 C/C++ object file은 아니다.

## 핵심 개념
`LOAD_FAST`, `BINARY_OP` 같은 CPython VM instruction을 interpreter가 해석한다. `.pyc`는 code object cache이며 Java `.class`나 C# CIL과 유사한 층이다. `.obj/.o`는 이미 target CPU machine code와 relocation 정보를 가진 linker 입력이다.

## 최소 예시
```python
import dis

def Add(a, b):
    return a + b

dis.dis(Add)
```

## 프로젝트 함의
CPython 3.12 thread가 이 bytecode를 실행하려면 GIL이 필요하다. Native extension의 machine code는 GIL을 해제하고 별도 CPU core를 사용할 수 있다.

## 관련 파일
- `SYNCH-MULTITHREAD-EXPLAINN.md`
- `environments.yml`

# CPython GIL과 C++ std::thread

## 짧은 맥락
GIL은 Python 언어 자체의 규칙이 아니라 현재 프로젝트가 사용하는 CPython 3.12 runtime의 제약이다.

## 핵심 개념
C++ `std::thread`에는 interpreter-wide lock이 없다. 여러 core에서 native code를 병렬 실행할 수 있지만 shared memory의 data race, lock, atomic, memory ordering 책임은 개발자가 직접 진다.

## 프로젝트 함의
순수 Python CPU-bound 단계는 thread 수를 늘려도 선형 가속되지 않는다. DB·HTTP·SSE 같은 I/O에는 thread가 유효하며, CPU 작업은 process worker 또는 GIL을 해제하는 native library를 사용해야 한다.

## 관련 파일
- `SYNCH-MULTITHREAD-EXPLAINN.md`
- `environments.yml`
- `src/backend/pipeline_service.py`

# CAS와 C++ memory_order

## 짧은 맥락
`compare_exchange_*()`에서 본 옵션은 CAS 성공·실패 시 주변 shared memory의 관측 순서를 정의하는 `std::memory_order`다.

## 핵심 개념
CAS는 atomic 변수 하나의 비교·교체만 보장한다. 일반 payload 공개에는 producer의 `release`와 consumer의 `acquire`가 연결돼 happens-before를 만들어야 한다. Mutex의 unlock/lock은 이 효과를 이미 제공한다.

## 프로젝트 함의
Python `Lock`과 `Condition`을 사용하는 현재 코드에는 explicit memory order가 필요 없다. 향후 C++ worker를 작성해도 lock-free 구조보다 mutex와 queue를 우선하고, CAS는 job의 `Queued → Running` 단일 소유권 획득처럼 작은 invariant에만 사용한다.

## 관련 파일
- `SYNCH-MULTITHREAD-EXPLAINN.md`
- `src/backend/pipeline_service.py`
