# 서버 API 계약 설명

## 목적

현재 React UI와 Python backend 사이의 API 계약을 Winsock TCP packet 관점으로 설명한다. 기준 코드는 `src/backend/api_contract.py`, `src/backend/pipeline_api.py`, `src/backend/pipeline_service.py`, `webapp/src/lib/api.js`다.

## 정합성 재검토 결과

실제 source와 직접 일치하는 내용:

- HTTP method와 route
- JSON request/response field
- HTTP status
- `job_status` 상태 집합
- SSE event name, event ID, heartbeat
- 질문 답변 값과 개수 제한

비교 설명만을 위한 가상 요소:

- `PacketHeader`
- `magic`
- 숫자형 `opcode`
- `protocolVersion`
- `payloadBytes`
- `requestId`
- IOCP `IoContext`

현재 Python·JavaScript source에는 위 binary header field가 존재하지 않는다. 아래 C++ 구조체는 기존 HTTP 계약을 binary protocol로 바꾼다면 어떤 위치에 대응하는지 보여주는 mental model이다. 실제 wire contract로 간주하면 안 된다.

정합성 판정:

- 실제 route 9개: 문서 반영 완료
- 선언된 run 생성 field, 질문 field, 상태값: 문서 반영 완료
- HTTP status와 SSE framing: 실제 코드 기준 반영
- 가상 C++ header와 actual HTTP wire binary compatibility: 해당 없음
- 모든 JSON payload를 고정 C++ struct로 완전 표현: 현재 불가능

마지막 항목이 불가능한 이유는 `facts`, 여러 `JsonObject`, `PipelineEventPayload.extra="allow"`, `DocumentPackageView.extra="allow"` 때문이다. 100% wire compatibility를 요구하려면 먼저 server contract를 닫힌 schema로 만들고 C++ serializer·deserializer contract test를 추가해야 한다.

## 현재 결론

핵심 Pipeline API는 prototype 기준으로 구조가 잡혀 있다.

- HTTP method와 Flask route 조합이 application opcode에 대응한다. 실제 숫자형 opcode field는 없다.
- Pydantic model이 request/response payload schema 역할을 한다.
- `job_id`가 장기 작업 식별자 역할을 한다.
- `POST /api/runs`는 작업을 등록하고 `202 Accepted`를 반환한다.
- SSE는 진행 event를 server에서 browser로 전송한다.
- 질문 답변은 별도 POST 요청으로 제출한다.
- 최종 snapshot은 GET으로 다시 읽는다.

그러나 production 계약은 아니다. 인증·인가, 전체 API version, distributed state, trace ID, OpenAPI, generated frontend type이 없다.

## 현재 Framework와 REST 정정

현재 backend는 FastAPI가 아니라 **Flask**다.

- `environments.yml` dependency: `flask`
- application composition: `src/backend/app.py`의 `Flask(__name__)`
- route 등록: `src/backend/pipeline_api.py`의 `@server.route(...)`
- 실행: `asap_app.py`의 Flask development server `app.run(..., threaded=True)`

FastAPI, Uvicorn, ASGI, LangGraph는 현재 runtime dependency나 실행 경로에 없다. Pydantic schema를 사용한다는 사실도 FastAPI 사용을 의미하지 않는다. 현재 코드는 Flask handler에서 Pydantic model을 직접 호출해 request/response를 검증한다.

HTTP는 application-layer protocol이다. REST는 별도 protocol이 아니라 HTTP method, resource URI, stateless interaction 같은 제약을 사용하는 architecture style이다. 현재 API는 `/api/runs`, `/api/runs/{job_id}`처럼 resource-oriented route를 사용하지만 SSE와 질문 replay도 포함하므로 가장 정확한 표현은 **HTTP/JSON + SSE 기반 REST-like API**다.

Flask와 FastAPI 모두 method/path를 handler에 mapping하는 routing framework를 제공할 수 있다. 그러나 framework routing은 아래 문제를 자동 해결하지 않는다.

- shared state race condition
- job 간 동시성 제한
- LLM/OCR/API rate limit
- idempotency와 중복 실행
- DB transaction과 distributed lock
- CPU-bound parallelism
- background worker의 수명과 cancellation

FastAPI로 교체해도 이 책임은 service, repository, worker, queue, semaphore, transaction 경계에 남는다.

## 실제 Schema 강제 범위

Pydantic으로 직접 검증되는 계약:

- `POST /api/runs` request와 `202` response
- 질문 답변 request
- run snapshot response
- document package 목록·상세 response
- SSE pipeline·paused·complete·not-found payload
- 알려진 Core API error response

부분 검증 또는 임의 JSON 계약:

- `POST /api/reconstruction-runs`
- `GET /api/admin/runs/{job_id}/blackboard`
- `GET /api/health`
- Core DTO 내부의 일부 `JsonObject` field

따라서 "모든 `/api/*`가 Pydantic contract를 가진다"는 설명은 틀리다. 정식 계약 중심은 Pipeline run, 질문, snapshot, document package, SSE다.

## Winsock과 HTTP 대응

| Winsock TCP 설계 | 현재 HTTP API |
|---|---|
| IP + port | origin, 예: `http://127.0.0.1:8060` |
| application opcode | HTTP method + path. 실제 `opcode` field 없음 |
| IOCP operation type | Browser와 Flask 아래 socket layer가 처리. API payload에 노출되지 않음 |
| magic | 없음. HTTP parser가 request start-line을 식별 |
| protocol version | HTTP version은 web stack이 처리. ASAP API 전체 version은 없음 |
| packet header | method, path, headers, status code |
| packet payload | JSON body |
| payload length | `Content-Length` 또는 transfer framing. 애플리케이션이 직접 파싱하지 않음 |
| request ID | 현재 없음. `job_id`와 다른 개념 |
| game object ID | `job_id`, `package_id`, `user_question_id` |
| ACK | `200 OK`, `202 Accepted` |
| error opcode | HTTP 4xx/5xx + `ApiErrorResponse` |
| server push packet | SSE `pipeline_event` |
| heartbeat packet | SSE `: heartbeat` comment |
| reconnect sequence | `Last-Event-ID` 또는 `start` index |

TCP는 byte stream이다. 직접 framing, packet 길이, endian, partial receive, reconnect를 처리해야 한다. HTTP는 이 framing을 web server와 browser가 처리한다. 애플리케이션은 method, path, status, JSON schema에 집중한다.

## Gunicorn gthread와 Winsock 서버 대응

현재 container는 Gunicorn의 master process, worker 1개, worker thread 4개로 Flask WSGI application을 실행한다.

Gunicorn master가 listening socket과 worker 수명을 관리하고, `gthread` worker가 connection을 accept해 thread pool에 HTTP request 처리를 배정한다. OS TCP stack이 실제 패킷 송수신을 담당하며 네 thread는 IOCP 전용 completion thread가 아니라 Flask handler까지 실행하는 request slot이다.

```text
TCP listen socket
-> Gunicorn master/worker accept
-> gthread request slot 1..4
-> Flask route
-> response
```

한 thread가 SSE나 blocking DB/HTTP I/O를 기다리면 그 slot은 점유되지만 다른 thread가 다른 요청을 처리할 수 있다. 동시에 실행 가능한 WSGI 요청은 대략 4개이며, 순수 Python CPU 작업은 GIL 때문에 네 core 병렬 실행을 보장하지 않는다. `RunRegistry`가 process-local이므로 현재 worker는 1개를 유지한다.

### Nginx와 container 경계

현재 image에는 Nginx가 없다. Flask의 `send_from_directory()`가 React `dist`와 SPA fallback을 제공하고 Gunicorn이 같은 WSGI application의 API와 정적 응답을 처리한다. Cloudflare Tunnel은 외부 ingress transport일 뿐 정적 파일 server나 reverse proxy를 대체하지 않는다.

```text
현재 시연:
cloudflared -> Gunicorn/Flask -> React dist + API + in-process pipeline

향후 운영:
cloudflared -> Nginx/frontend -> Gunicorn API -> queue -> OCR/ML worker
```

현재 방식은 같은 origin 구성과 process-local `RunRegistry`를 유지하는 면접 시연에는 적합하다. React 산출물은 약 1MB이므로 Nginx를 분리해도 대형 image 문제는 거의 줄지 않는다. 실제 분리 우선순위는 Playwright, PaddleOCR, Torch를 사용하는 worker지만, 먼저 run state, SSE event, job queue를 PostgreSQL이나 외부 queue로 옮겨야 한다. Nginx를 도입한다면 Gunicorn과 같은 container에 넣지 않고 별도 frontend/reverse-proxy container로 둔다.

### Request thread와 pipeline thread

현재에는 서로 다른 두 종류의 thread가 있다. Gunicorn의 `--threads=4`는 HTTP request 처리 slot만 4개로 제한한다. 파이프라인 수를 제한하지 않는다. `POST /api/runs`는 run을 등록한 뒤 매번 별도의 `daemon` thread를 생성하고 `202 Accepted`를 반환한다.

```text
Gunicorn worker process 1개
|- gthread request slot 4개
|  |- POST /api/runs: run 등록 + pipeline thread 시작
|  |- GET /api/runs/{id}: snapshot 조회
|  `- GET /api/runs/{id}/events: SSE 대기
|- RunRegistry: process-local dict + Condition/Lock
`- pipeline daemon thread: active run마다 1개, 상한 없음
```

`RunRegistry`의 `Condition/Lock`은 한 process 안에서 run map과 event list를 동시에 수정할 때 data race를 막는다. C++로 비유하면 한 server process의 `std::unordered_map`을 `std::mutex`로 보호하는 것이다. 이 lock은 상태 영속화, process 간 공유, 작업 queue, 재시도, 취소, 동시 실행 수 제한을 제공하지 않는다.

현재 구조의 구체적인 위험은 다음과 같다.

- 동시에 생성 가능한 pipeline thread 수에 상한이 없어 CPU, RAM, browser, OCR/ML native thread, DB connection과 외부 API quota가 함께 고갈될 수 있다.
- SSE 연결 하나가 열려 있는 동안 gthread request slot 하나를 점유한다. 연결이 많으면 일반 API가 사용할 네 slot이 줄어든다.
- pipeline thread가 `daemon=True`이므로 worker crash, restart 또는 container stop 때 완료를 기다리지 않고 종료된다. memory의 run 상태와 event도 사라진다.
- `api_snapshot.json`은 결과가 만들어진 run의 local artifact이지 durable job queue나 실행 lease가 아니다. 실행 중 작업을 자동 재개한다고 볼 수 없다.
- 질문 답변 replay는 background thread가 아니라 HTTP request thread에서 동기로 실행되고 process-local `_answerLock`으로 전체 replay를 직렬화한다. 긴 replay는 request slot을 오래 점유한다.

Gunicorn worker를 2개 이상으로 늘리면 worker마다 별도 `RunRegistry`, rate-limit state와 `_answerLock`이 생성된다. `POST`가 worker A에 run을 만들고 후속 `GET` 또는 SSE가 worker B로 가면 `run_not_found`가 발생할 수 있다. 중복 실행 억제와 file lock도 process 경계를 넘지 못한다. 따라서 현재 `--workers=1`은 단순 성능 설정이 아니라 process-local 상태를 전제로 한 정확성 제약이다. Pod를 여러 개로 늘려도 같은 문제가 생긴다.

운영 구조에서는 API가 durable DB/queue에 `queued` job을 기록하고, 동시성 상한이 있는 worker가 한 job을 claim해 실행해야 한다. run, event와 질문 상태는 PostgreSQL 같은 공유 저장소에 두고 산출물은 공유 object storage에 둔다. 이때 `RunRegistry`는 권위 있는 원장이 아니라 선택적 cache/projection으로만 사용할 수 있다. OCR/LLM 실행 동안 DB transaction을 계속 열어 두어서는 안 된다.

### 수정 우선순위와 FastAPI 판단

금일 가능한 범위는 시연 안정화다. 기존 API 계약과 Flask를 유지하고, 실행마다 생성하는 무제한 thread를 표준 라이브러리의 bounded queue와 고정 worker 수로 교체한다. 무거운 pipeline 특성상 worker 1개부터 시작하고, queue가 가득 차면 새 요청을 `503`으로 거부해야 한다. queue 용량, 상태 전이, 실패 처리 test를 함께 추가하는 데 대략 4~8시간이 필요하다. 이 단계에서도 `--workers=1`과 재시작 시 실행 중 작업 손실 제약은 남는다.

운영 구조 전환에는 별도 단계가 필요하다. PostgreSQL에 run, event, answer와 job claim 상태를 저장하고, API와 pipeline worker를 별도 process/container로 실행한다. SSE는 우선 DB polling으로 구현하고 실제 병목이 확인될 때만 pub/sub을 추가한다. 질문 답변 replay도 request thread에서 실행하지 않고 resume job으로 등록한다. crash recovery, idempotency, migration과 통합 test까지 포함하면 대략 2~4 개발일 범위다. 다중 Pod가 필요하면 local artifact도 object storage로 옮겨야 한다.

FastAPI로 교체하는 것만으로 이 문제는 해결되지 않는다. `async def`는 blocking OCR, Playwright, LLM과 CPU 작업을 자동 병렬화하지 않는다. FastAPI의 in-process background task도 process가 죽으면 함께 사라지고, 여러 Uvicorn worker는 각각 별도 `RunRegistry`를 갖는다. FastAPI는 많은 SSE connection 처리, schema validation과 API 문서화에는 유리하지만 durable queue, backpressure, distributed state와 crash recovery를 대신하지 않는다. 따라서 run state와 worker 경계를 먼저 외부화하고, 이후 API adapter 교체가 실제로 필요한지 판단한다.

## IOCP Operation과 Application Opcode

두 코드는 계층이 다르다.

```cpp
enum class IoOperation : uint8_t
{
    Accept,
    Recv,
    Send,
    Disconnect,
};

struct IoContext
{
    WSAOVERLAPPED overlapped{};
    IoOperation operation{};
    std::array<char, 64 * 1024> storage{};
    WSABUF buffer{};

    IoContext()
    {
        buffer.buf = storage.data();
        buffer.len = static_cast<ULONG>(storage.size());
    }
};
```

`WSAOVERLAPPED` 자체에는 `Accept`, `Recv`, `Send` 같은 operation code field가 없다. 위 `IoOperation`은 개발자가 owning context에 추가한 값이다. IOCP에서 돌려받은 `WSAOVERLAPPED*`로 원래 context를 복원한다.

```cpp
auto* context = CONTAINING_RECORD(
    overlappedPointer,
    IoContext,
    overlapped
);
```

즉 `IoOperation`도 Winsock 표준 opcode가 아니라 custom completion context metadata다. `WSAOVERLAPPED::hEvent` 등에 operation 값을 억지로 넣기보다 별도 context field로 소유하는 편이 명확하다.

현재 프로젝트는 IOCP를 사용하지 않는다. Flask handler는 web server가 이미 파싱한 HTTP request를 받는다. Linux container에 배포하면 하위 server가 사용하는 socket multiplexer도 IOCP가 아니라 Linux runtime 구현에 따른다. IOCP 구조는 사용자의 기존 경험과 계층을 비교하기 위한 설명이다.

`IoContext::operation`은 완료 통지가 어떤 비동기 socket 작업에서 발생했는지 나타낸다.

- `Accept`: 새 socket 연결 완료
- `Recv`: 수신 buffer 채우기 완료
- `Send`: 송신 buffer 전송 완료
- `Disconnect`: 연결 종료 처리

`PacketHeader::opcode`는 수신하거나 송신하는 payload의 의미를 나타낸다.

- `CreateRunRequest`: run 생성 명령
- `CreateRunAccepted`: run 생성 ACK
- `PipelineEvent`: 진행 event
- `SubmitAnswersRequest`: 질문 답변 명령

같은 값이 아니다. 서로 대체하지도 않는다.

```text
Server가 CreateRunRequest 수신
IOCP operation = Recv
Packet opcode  = CreateRunRequest

Server가 CreateRunAccepted 송신 완료
IOCP operation = Send
Packet opcode  = CreateRunAccepted

Client가 PipelineEvent 수신
IOCP operation = Recv
Packet opcode  = PipelineEvent
```

IOCP completion 처리 순서는 개념적으로 다음과 같다.

```cpp
void OnIocpComplete(IoContext& context, DWORD transferredBytes)
{
    switch (context.operation)
    {
    case IoOperation::Recv:
        decoder.Append(context.storage.data(), transferredBytes);
        while (decoder.TryPop(packet))
            Dispatch(packet.header.opcode, packet.payload);
        PostRecv(context);
        break;

    case IoOperation::Send:
        CompleteOrContinueSend(context, transferredBytes);
        break;

    default:
        break;
    }
}
```

`operation`을 먼저 보고 I/O completion을 처리한다. 수신된 bytes에서 frame을 복원한 뒤 `opcode`를 보고 application handler를 선택한다. 현재 Flask에서는 socket·IOCP 계층을 web server가 숨기고 `method + route`를 기준으로 handler를 호출한다.

## 가상 Binary Packet Header

아래 코드는 현재 source에 없다. HTTP를 custom binary TCP protocol로 바꿀 때 사용할 수 있는 비교 모델이다.

`opcode`/`packet_type`은 TCP byte stream 위에서 application message를 구분하기 위한 설명용 field다. 현재 HTTP API에는 추가하지 않는다. 요청은 `method + path`, 응답은 `status code + response schema`, SSE message는 `event:` 이름으로 이미 구분되므로 별도 field는 중복이다.

```cpp
enum class OpCode : uint16_t
{
    CreateRunRequest       = 0x0101,
    CreateRunAccepted      = 0x0102,
    ReadRunRequest         = 0x0201,
    RunSnapshot            = 0x0202,
    PipelineEvent          = 0x0301,
    RunPaused              = 0x0302,
    RunComplete            = 0x0303,
    SubmitAnswersRequest   = 0x0401,
    SubmitAnswersResponse  = 0x0402,
    Error                  = 0xFFFF,
};

#pragma pack(push, 1)
struct PacketHeader
{
    uint32_t magic;            // frame format signature
    uint16_t protocolVersion;  // custom payload schema version
    OpCode opcode;             // application message type
    uint32_t payloadBytes;     // header 뒤 payload byte 수
    uint64_t requestId;        // request/response correlation ID
};
#pragma pack(pop)
```

실제 TCP 구현에서는 struct를 그대로 `send()`하지 않는다. padding, endian, ABI 차이를 고려해 field별 직렬화가 필요하다. 현재 HTTP/JSON에서는 web stack과 JSON parser가 이 역할을 맡는다.

### `magic`

`magic`은 frame이 예상한 protocol format인지 빠르게 확인하는 signature다. 예를 들어 wire bytes를 `41 53 41 50`, ASCII `ASAP`으로 정할 수 있다. 숫자로 표현하면 network byte order 기준 `0x41534150`이다.

PE의 magic과 기본 개념은 같다.

- `IMAGE_DOS_HEADER.e_magic = 0x5A4D`: `MZ` 식별
- `IMAGE_NT_HEADERS.Signature = 0x00004550`: `PE\0\0` 식별
- `IMAGE_OPTIONAL_HEADER.Magic = 0x10B` 또는 `0x20B`: PE32 또는 PE32+ 식별
- Network frame magic: custom packet 형식 식별

차이는 대상뿐이다. PE는 파일, 여기서는 network frame이다. `magic`은 packet 종류가 아니며 checksum, 암호화, 인증 값도 아니다. byte stream 위치가 틀렸거나 다른 protocol이 들어온 상황을 감지하는 보조값이다.

Dedicated port에서 한 protocol만 받고 length framing이 엄격하다면 magic은 필수도 아니다. 진단과 잘못된 protocol 조기 거부에는 도움되지만 무결성이나 보안을 제공하지 않는다.

현재 HTTP API에는 custom `magic`이 없다. HTTP server가 `POST /api/runs HTTP/1.1` 같은 start-line을 파싱해 protocol을 식별한다. 따라서 현재 프로젝트에 `magic`을 새로 추가할 이유도 없다.

### `protocolVersion`

Custom binary payload schema version이다. `PacketHeader` field 순서나 payload encoding이 바뀔 때 호환성을 판단한다. HTTP의 `HTTP/1.1`과도 다르고 application API version과도 별도일 수 있다.

현재 ASAP API에는 전체 application version이 없다. URL도 `/api/v1/...` 형태가 아니다. `UserQuestionView.contract_version`은 질문 계약만 versioning하며 전체 API version이 아니다.

### `opcode`

Packet payload의 application message type이다. 송수신 방향이나 IOCP completion 종류가 아니다. 현재 HTTP에서는 아래 조합이 같은 역할을 한다.

```text
POST /api/runs                              CreateRunRequest
GET  /api/runs/{job_id}                     ReadRunRequest
GET  /api/runs/{job_id}/events              SubscribeRunEvents
POST /api/runs/{job_id}/question-answers    SubmitAnswersRequest
```

숫자값 `0x0101` 등은 문서 예시일 뿐 실제 source 또는 network에 존재하지 않는다.

HTTP request는 method와 path가 handler type을 명시한다. HTTP response에는 별도 opcode가 없으며 원래 request, status code, response schema로 의미를 판단한다. SSE에서는 `event: pipeline_event`, `event: run_paused` 같은 event name이 stream 내부의 명시적 message discriminator 역할을 한다.

### `payloadBytes`

Header 다음에 읽어야 할 payload 길이다. TCP에는 packet boundary가 없으므로 decoder가 완전한 frame 도착 여부를 판정할 때 필요하다. `recv()` 한 번이 packet 하나와 일치한다는 보장은 없다.

현재 HTTP에서는 web server가 `Content-Length`, chunked transfer 등 HTTP framing을 처리한다. Flask handler는 이미 복원된 JSON body를 받는다.

### `requestId`

하나의 request와 response를 연결하는 correlation ID다. 여러 요청이 동시에 진행될 때 어느 응답이 어느 요청의 결과인지 찾는 데 사용한다.

현재 API에는 명시적 `request_id` 또는 `trace_id`가 없다. 다음 ID와 혼동하면 안 된다.

- `job_id`: 재연결 후에도 유지되는 Pipeline resource ID
- SSE `id`: 한 run 안의 event sequence
- `user_question_id`: 질문 entity ID
- `package_id`: document package entity ID

Custom packet `requestId`는 짧은 request/response correlation용이다. `job_id`는 장기 application state 식별자다.

HTTP mapping은 다음과 같다.

```text
POST /api/runs
```

`POST + /api/runs` 조합이 개념적으로 `OpCode::CreateRunRequest`에 대응한다. 실제 HTTP message에는 숫자형 opcode가 없다. `Content-Type: application/json`이 payload codec을 지정한다.

## 작업 생성 계약

논리적인 C++ payload는 다음과 같다.

```cpp
struct IngredientInput
{
    std::string role;        // primary | secondary
    std::string name;
    double percentage;
};

struct InputFacts
{
    std::vector<IngredientInput> ingredients;
    std::optional<std::string> intendedUse;
    std::string originCountry;   // ISO alpha-2
};

struct CreateRunRequest
{
    std::string query;
    std::string productName;
    std::string description;
    std::string url;
    std::string kurlyUrl;
    nlohmann::json facts;                 // 열린 JSON object
    std::optional<InputFacts> inputFacts;
};
```

실제 top-level JSON field:

| JSON field | 의미 |
|---|---|
| `query` | 직접 Pipeline query |
| `product_name` | 일반 UI 상품명 |
| `description` | 상품 설명 |
| `url` | 우선 사용되는 상품 URL |
| `kurly_url` | 기존 호환용 URL alias |
| `facts` | 추가 fact JSON object |
| `input_facts` | 검증되는 ingredient·용도·원산지 DTO |

`nlohmann::json`은 C++ 대응을 보여주기 위한 예시 타입이다. 현재 프로젝트가 사용하는 dependency가 아니다. 실제 Python `RunCreateRequestPayload.facts`는 임의 key를 허용하는 JSON object다. 다만 `ingredients`, `intended_use`, `origin_country`, `user_input_facts`는 server가 reserved field로 제거한 뒤 typed `input_facts`에서 다시 구성한다.

현재 JSON packet:

```http
POST /api/runs HTTP/1.1
Content-Type: application/json
```

```json
{
  "product_name": "냉동 해물파전",
  "url": "https://example.invalid/product",
  "input_facts": {
    "ingredients": [
      {
        "role": "primary",
        "name": "wheat flour",
        "percentage": 40
      }
    ],
    "intended_use": "human consumption",
    "origin_country": "KR"
  }
}
```

Backend는 다음을 검증한다.

- 알 수 없는 request field 거부
- ingredient 최대 20개
- ingredient가 비어 있지 않으면 primary ingredient 정확히 1개
- ingredient name 중복 금지
- 각 percentage는 0 초과 100 이하
- percentage 합계 100 이하
- `origin_country` ISO alpha-2 검증
- `query`, `product_name`, `description`, `url` 중 하나 필수

응답은 즉시 결과가 아니라 작업 등록 ACK다.

```cpp
struct CreateRunAccepted
{
    std::string jobId;
    std::string status;      // queued | running | ...
    bool reused;
    std::string eventsUrl;
    std::string resultUrl;
};
```

```http
HTTP/1.1 202 Accepted
```

```json
{
  "job_id": "job_1234567890",
  "status": "queued",
  "reused": false,
  "events_url": "/api/runs/job_1234567890/events",
  "result_url": "/api/runs/job_1234567890"
}
```

`202`는 명령 수신 성공이다. Pipeline 완료 성공을 의미하지 않는다. Winsock 기준 `CreateRunAccepted` ACK와 같다.

동일한 `query + facts` signature를 가진 active job이 현재 process에 있으면 새 job을 만들지 않는다. 기존 `job_id`를 반환하고 `reused=true`로 표시한다. 이 중복 억제도 `RunRegistry` memory 안에서만 동작하므로 다중 Pod 공통 idempotency는 아니다.

## Run 상태 머신

```cpp
enum class RunStatus : uint8_t
{
    Queued,
    Running,
    AwaitingInput,
    Completed,
    Failed,
};
```

현재 허용 상태도 동일한 다섯 값이다.

```text
queued
running
awaiting_input
completed
failed
```

`job_id`는 socket handle이 아니다. 연결이 끊겨도 유지돼야 하는 application entity ID다. TCP connection과 job lifetime을 결합하면 안 된다.

## SSE는 Server Push Packet Stream

SSE endpoint:

```text
GET /api/runs/{job_id}/events
```

Browser는 `EventSource` connection을 연다. Server는 연결 위에 text event를 순서대로 보낸다.

```text
id: 7
event: pipeline_event
data: {"stage":"Classification","status":"running","message":"..."}

```

Winsock식 논리 packet:

```cpp
struct PipelineEvent
{
    uint32_t sequence;
    std::string stage;
    std::string status;
    std::string message;
    std::optional<std::string> timestamp;
    std::optional<nlohmann::json> partialResult;
    std::optional<nlohmann::json> collectedInputSummary;
};
```

실제 `PipelineEventPayload`는 `job_id`를 공통 필수 field로 선언하지 않는다. 구독 중인 `events URL`이 job 문맥을 소유한다. 일부 extra payload에 `run_id`가 들어올 수 있지만 보장된 공통 field가 아니다. 실제 model은 추가 field도 허용하므로 위 구조체는 명시된 공통 field만 대응한다.

현재 event 종류:

- `pipeline_event`: 진행 event
- `run_paused`: 사용자 입력 대기
- `run_complete`: 성공 또는 실패 종료
- `error`: 존재하지 않는 run
- heartbeat comment: idle connection 유지

SSE는 단방향이다. Client가 같은 connection으로 답변 packet을 보낼 수 없다. 답변은 별도 HTTP POST를 사용한다. TCP full-duplex connection과 다른 지점이다.

`id`는 event list index다. Browser 재접속 시 `Last-Event-ID`를 보낼 수 있다. Server는 그 다음 event부터 다시 전송한다.

현재 terminal SSE payload의 `run_id`에는 route에서 받은 `job_id`가 들어간다. Snapshot의 `run_id`는 내부 Pipeline run ID일 수 있으므로 이름이 같아도 의미가 완전히 같다고 보장할 수 없다. Frontend는 terminal payload의 `run_id`를 사용하지 않고 closure가 가진 `jobId`로 snapshot을 다시 조회하므로 현재 UI 흐름은 유지된다. 계약 이름은 향후 `job_id`로 통일하는 편이 안전하다.

## 질문 답변 계약

```text
POST /api/runs/{job_id}/question-answers
```

```cpp
enum class Answer : uint8_t
{
    Yes,
    No,
    Unknown,
};

struct QuestionAnswer
{
    std::string userQuestionId;
    Answer answer;
};

struct SubmitAnswersRequest
{
    std::vector<QuestionAnswer> answers; // 1..8
};
```

```json
{
  "answers": [
    {
      "user_question_id": "question_abc",
      "answer": "yes"
    }
  ]
}
```

Backend는 현재 request handler 안에서 classification replay를 수행하고 snapshot을 `200 OK`로 반환한다. Replay가 길어지면 HTTP timeout 위험이 있다. Production에서는 답변 저장 후 `202 Accepted`를 반환하고 worker가 재개하는 방식이 더 일관적이다.

## Snapshot 계약

```text
GET /api/runs/{job_id}
```

응답 `RunSnapshotResponse`는 다음 영역을 포함한다.

- `job_id`, `job_status`
- request facts
- 진행 events
- input processing view
- product understanding view
- HS2 routing view
- classification candidate set
- active 또는 resolved user questions
- document package summary
- error

이 snapshot은 server authoritative state의 UI projection이다. React는 raw Blackboard를 직접 읽지 않는다. 게임 client가 server world 전체가 아니라 replication snapshot을 받는 구조와 유사하다.

현재 `run_dir`, `component_results`, 여러 `JsonObject` field도 응답에 포함될 수 있다. 내부 경로와 내부 payload가 public contract로 굳어질 위험이 있다. `run_dir`는 제거하고 artifact ID 또는 URL로 바꾸는 편이 안전하다.

## Error Packet

핵심 Pipeline API의 error payload:

```cpp
struct ErrorPayload
{
    std::string errorCode;
    std::string message;
    std::optional<std::string> field;
    std::optional<std::string> hint;
    std::optional<std::string> jobId;
    std::optional<uint32_t> retryAfterSeconds;
};
```

```json
{
  "error": "invalid_run_create_payload",
  "message": "Input should be ...",
  "field": "input_facts.origin_country",
  "hint": "Check the structured input field and submit it again."
}
```

주요 HTTP status:

- `200`: 조회·답변 처리 성공
- `202`: 비동기 작업 등록 성공
- `400`: payload 또는 의미 검증 실패
- `404`: 일반 조회에서 run, question, package 없음
- `409`: 질문 replay 충돌 또는 실패
- `429`: 실행 생성 rate limit

SSE에서 존재하지 않는 run은 HTTP `404`가 아니다. HTTP stream은 이미 `200`으로 시작하고 내부에 `event: error`와 `run_not_found` payload를 보낸 뒤 종료한다.

React `readJson()`은 현재 structured error를 JavaScript `Error.message` 한 줄로 축약한다. `error`, `field`, `hint`, `retry_after_seconds`를 UI까지 보존하지 못한다.

## 현재 Endpoint

| Method | Path | 역할 |
|---|---|---|
| GET | `/api/health` | process 생존 확인 |
| POST | `/api/runs` | Pipeline job 등록 |
| GET | `/api/runs/{job_id}` | 최신 snapshot 조회 |
| GET | `/api/runs/{job_id}/events` | SSE 진행 event |
| POST | `/api/runs/{job_id}/question-answers` | 질문 답변과 replay |
| GET | `/api/runs/{job_id}/document-packages` | 서류 package 목록 |
| GET | `/api/runs/{job_id}/document-packages/{package_id}` | 서류 package 상세 |
| POST | `/api/reconstruction-runs` | 캐시 기반 reconstruction 재실행 |
| GET | `/api/admin/runs/{job_id}/blackboard` | 내부 Blackboard 일부 조회 |

## 현재 계약의 강점

- Core run·document package route는 대부분 resource 중심이다.
- 비동기 job 생성에 `202`를 사용한다.
- Pipeline run·질문·snapshot·document package의 핵심 request와 response는 Pydantic으로 검증한다.
- Run 생성과 질문 답변의 알 수 없는 top-level request field를 거부한다.
- `job_status` 상태 집합이 명시돼 있다.
- 질문 ID와 답변 값이 명시돼 있다.
- SSE event name과 terminal event가 구분돼 있다.
- frontend가 stale request와 중복 EventSource를 방어한다.
- Python question replay와 React projection 회귀 테스트가 있다.

## 현재 계약의 약점

- 인증·인가 없음. Admin route도 공개 상태다.
- `/api/v1` 같은 전체 contract version 없음.
- OpenAPI 또는 공유 IDL 없음.
- JavaScript는 TypeScript가 아니므로 compile-time DTO 검증 없음.
- `JsonObject` field가 많아 일부 payload schema가 느슨하다.
- Run 생성의 `facts` field는 열린 JSON bag이다.
- `PipelineEventPayload`는 알 수 없는 추가 field를 허용한다.
- `DocumentPackageView`도 추가 field를 허용하고 `UserQuestionView`는 알 수 없는 field를 무시한다.
- Terminal SSE의 `run_id`가 실제로 route `job_id`를 담아 내부 `run_id`와 의미가 충돌한다.
- `run_dir` 같은 server 내부 경로가 frontend DTO에 노출된다.
- 명시적 request ID, trace ID, idempotency key 없음.
- Rate limit이 process memory 기반이라 다중 Pod에서 공유되지 않는다.
- `RunRegistry`와 SSE event buffer가 process-local이다.
- 질문 replay와 reconstruction이 동기 HTTP 작업이다.
- 일부 unhandled exception은 공통 `ApiErrorResponse`가 아닌 Flask `500` response가 될 수 있다.

## 권장 개선 순서

1. 인증·인가 적용 전 외부 공개를 막는다.
2. `run_dir`와 raw internal payload를 public snapshot에서 제거한다.
3. structured error object를 React까지 보존한다.
4. request ID와 trace ID를 error·log·response header에 추가한다.
5. run, event, answer를 PostgreSQL에 저장한다.
6. 답변 replay를 `202` 기반 worker job으로 변경한다.
7. Pydantic schema에서 OpenAPI 또는 JSON Schema를 생성한다.
8. frontend contract type 또는 runtime validator를 생성한다.

Framework 변경이 먼저는 아니다. Flask를 유지하면서 contract와 state ownership부터 정리할 수 있다.

## 관련 파일

- `src/backend/api_contract.py`
- `src/backend/pipeline_api.py`
- `src/backend/pipeline_service.py`
- `src/backend/pipeline_projection.py`
- `src/backend/app.py`
- `webapp/src/lib/api.js`
- `webapp/src/hooks/useClassificationRun.js`
- `tests/test_classification_question_contract.py`
- `tests/test_question_answer_replay.py`
- `webapp/src/hooks/useClassificationRun.test.js`
