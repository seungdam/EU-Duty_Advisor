# Python 동기화와 멀티스레딩 설명

## 결론

Python은 멀티스레딩이 가능한 언어다. 현재 프로젝트의 `threading.Thread`는 실제 OS thread를 만든다. 다만 이 저장소가 사용하는 **CPython 3.12**에는 GIL(Global Interpreter Lock)이 있으므로, 한 process 안에서 여러 thread가 Python bytecode를 동시에 병렬 실행하지는 못한다.

따라서 아래 두 문장은 구분해야 한다.

- 가능: 여러 thread가 번갈아 실행되며 DB, HTTP, 파일, SSE 같은 I/O 대기를 겹친다.
- 제한: CPU만 사용하는 순수 Python 계산을 여러 thread가 여러 core에서 동시에 실행한다.

GIL은 애플리케이션 데이터의 정합성을 보장하는 lock이 아니다. `_runs`, 질문 답변, 파일 갱신 같은 공유 상태에는 여전히 `Lock`, `Condition`, queue 또는 DB transaction이 필요하다.

## Excalidraw 시각 자료

- **목표 LangGraph DAG/FSM**: [로컬 편집 파일](docs/architecture/langgraph-pipeline-fsm.excalidraw) · [Excalidraw에서 열기](https://excalidraw.com/#json=f2j1QtY4UElzA3FdtINmv,ASff_2TklP62ELQUNTQlVQ)
- **현재 Flask thread와 동기화 경계**: [로컬 편집 파일](docs/architecture/flask-thread-synchronization.excalidraw) · [Excalidraw에서 열기](https://excalidraw.com/#json=z7RxhV8G3TLxdgm5kU6Zh,V-h2le6spKM6FpmCsondfg)

첫 도식은 목표 구조다. 두 번째 도식은 현재 코드의 실제 실행 구조다. 둘을 섞어서 현재 구현이 이미 LangGraph 기반이거나 한 run 내부가 병렬화됐다고 해석하면 안 된다.

## 기준 환경

`environments.yml`은 `python=3.12`를 사용한다. 이 문서는 일반적인 CPython 3.12 build를 기준으로 한다. Python 언어 명세 자체가 GIL을 요구하는 것은 아니며, 다른 Python 구현이나 실행 방식은 다를 수 있다.

## Python bytecode란

Python bytecode는 x86/ARM assembly나 machine code가 아니다. `.py` source를 **CPython virtual machine이 이해하는 opcode**로 변환한 중간 결과다.

```text
Python source (.py)
→ parser / AST
→ code object + CPython bytecode
→ CPython evaluation loop가 opcode 해석
→ C runtime 함수 또는 CPU machine instruction 실행
```

예를 들어 프로젝트의 Python 3.12에서 다음 함수를 `dis.dis()`로 확인할 수 있다.

```python
def Add(a, b):
    return a + b
```

```text
0  RESUME        0
2  LOAD_FAST     0 (a)
4  LOAD_FAST     1 (b)
6  BINARY_OP     0 (+)
10 RETURN_VALUE
```

`LOAD_FAST`, `BINARY_OP`는 CPython VM instruction이다. CPU가 직접 실행하는 `MOV`, `ADD`, `CALL` 같은 ISA instruction이 아니다. CPython의 C로 작성된 evaluation loop가 opcode를 읽고 해당 동작을 수행한다.

Import 시 만들어질 수 있는 `__pycache__/*.pyc`는 이 code object와 bytecode를 재사용하기 위한 cache다. source parsing과 bytecode compilation을 줄이지만 다음 특성이 있다.

- CPython interpreter가 있어야 실행된다.
- Python version별 bytecode 형식 차이가 있다.
- linker가 읽는 object file이 아니다.
- symbol relocation이나 native machine code를 제공하지 않는다.
- 독립 executable 또는 shared library가 아니다.

언어별 대응은 다음과 같다.

| 언어 | 중간 결과 | 실행 방식 |
|---|---|---|
| Python/CPython | code object, bytecode, 선택적 `.pyc` cache | CPython VM이 주로 해석 |
| Java | JVM bytecode가 담긴 `.class` | JVM interpreter/JIT |
| C# | CIL과 metadata가 담긴 .NET assembly | CLR JIT/AOT |
| C/C++ | `.obj`/`.o`의 target machine code와 relocation | linker가 executable/shared library 생성 |

따라서 개념적 층은 Java bytecode와 C# CIL에 더 가깝다. 단, CPython bytecode는 Python 구현과 version에 강하게 종속된 내부 형식이며 JVM bytecode나 CIL처럼 장기 호환용 배포 계약으로 보면 안 된다.

C#의 “assembly”는 `.NET assembly`라는 packaging 단위라는 뜻이다. CPU assembly language와 같은 단어를 쓰지만 CIL, metadata, resource를 묶은 `.dll/.exe`를 의미한다.

C/C++ object file은 이미 특정 CPU/ABI용 machine code를 포함한다. Python `.pyc`보다 compilation pipeline의 훨씬 뒤 단계다. Python extension인 `.pyd`/`.so`는 native shared library이므로 `.pyc`보다 C/C++ 결과물에 가깝다.

CPython 3.12는 실행 중 opcode를 specialization할 수 있지만, 일반적인 Python 함수 전체를 C++ compiler처럼 native object code로 만드는 것은 아니다. 이 bytecode를 실행하는 한 thread가 GIL을 가져야 한다는 것이 앞서 설명한 제약이다.

## CPython GIL과 C++의 차이

“Python 언어가 CPU 병렬 처리를 금지한다”는 표현은 정확하지 않다. GIL은 Python 언어 문법이나 언어 명세의 필수 요소가 아니라 **CPython runtime 구현의 제약**이다. 현재 프로젝트가 CPython 3.12를 사용하므로 실제로 이 제약을 받는다.

C++ `std::thread`에는 GIL에 해당하는 언어·runtime 전역 lock이 없다. 두 OS thread가 실행 가능하고 CPU core가 둘 이상이면 각 thread의 native machine code를 같은 순간 실행할 수 있다.

```text
CPython 3.12 process
Core 0: Thread A가 Python bytecode 실행 [GIL 보유]
Core 1: Thread B가 Python bytecode 실행 대기 [GIL 대기]

C++ process
Core 0: std::thread A가 native code 실행
Core 1: std::thread B가 native code 실행
```

C++ standard가 특정 core 배치나 실행 속도를 보장하는 것은 아니다. 실제 병렬 실행은 CPU core 수와 OS scheduler에 달려 있다. 중요한 차이는 C++ runtime이 모든 thread를 강제로 직렬화하는 GIL을 두지 않는다는 것이다.

```cpp
#include <cstdint>
#include <thread>

std::uint64_t CpuWork(std::uint64_t begin, std::uint64_t end)
{
    std::uint64_t result = 0;
    for (std::uint64_t value = begin; value < end; ++value)
        result += value * value;
    return result;
}

int main()
{
    std::uint64_t resultA = 0;
    std::uint64_t resultB = 0;

    std::thread threadA([&] { resultA = CpuWork(0, 500'000); });
    std::thread threadB([&] { resultB = CpuWork(500'000, 1'000'000); });

    threadA.join();
    threadB.join();
    return resultA != 0 && resultB != 0 ? 0 : 1;
}
```

두 thread는 서로 다른 결과 변수에 쓰므로 shared write race가 없다. 두 thread가 같은 일반 변수에 동시에 `+=`를 수행하면 C++에서는 data race이며 undefined behavior다. 그때는 `std::mutex`, `std::atomic`, 작업 분할 또는 message passing이 필요하다.

즉 trade-off는 다음과 같다.

| CPython 3.12 | C++ |
|---|---|
| Python bytecode 실행은 GIL로 process당 한 thread | native code thread에 전역 실행 lock 없음 |
| interpreter 내부 memory 안전을 runtime이 일부 보호 | shared memory 안전을 개발자가 직접 보장 |
| I/O 대기와 GIL 해제 native code는 concurrency/parallelism 가능 | CPU 작업도 여러 core에서 직접 parallelism 가능 |
| application invariant에는 별도 lock 필요 | application invariant와 data race 모두 별도 동기화 필요 |

Python에서도 CPU 병렬 처리가 완전히 불가능한 것은 아니다.

- 여러 process는 각자 interpreter와 GIL을 가지므로 여러 core에서 실행할 수 있다.
- C/C++ extension이 GIL을 해제하면 native code가 병렬 실행될 수 있다.
- Paddle, Torch, NumPy 같은 library가 내부 native worker를 사용할 수 있다.
- CPU job을 별도 worker service로 분리할 수 있다.

따라서 정확한 결론은 “CPython 3.12의 **한 process 안에서 순수 Python CPU-bound bytecode를 여러 thread로 병렬화하기 어렵다**”이다. C++에는 같은 종류의 제약이 없다.

## CAS와 memory ordering

기억한 CAS 옵션은 보통 다음 API의 `std::memory_order`다.

```cpp
atomicValue.compare_exchange_strong(
    expected,
    desired,
    std::memory_order_acq_rel, // CAS 성공 시
    std::memory_order_acquire  // CAS 실패 시 load
);
```

CAS(Compare-And-Swap/Exchange)는 atomic 값이 `expected`와 같을 때만 `desired`로 교체한다. 비교와 교체가 하나의 indivisible read-modify-write 연산이다. 실패하면 교체하지 않고 `expected`에 실제 현재 값이 들어간다.

CAS가 보장하는 것은 **해당 atomic 변수 하나의 조건부 변경**이다. 같은 객체에 들어 있는 일반 payload 전체를 자동으로 보호하지 않는다.

### Shared memory란

같은 C++ process의 thread들은 global/static object와 heap을 공유한다. 각 thread stack은 별도지만 pointer나 reference를 전달하면 stack object도 다른 thread가 접근할 수 있다.

```cpp
struct Job
{
    Payload payload;                 // 일반 shared memory
    std::atomic<JobState> state;     // atomic synchronization variable
};
```

CPU core마다 cache와 store buffer가 있고 compiler도 명령 순서를 최적화한다. Source code에서 먼저 쓴 값이 다른 thread에 같은 순서로 보인다고 자동 가정할 수 없다. C++ memory model은 `happens-before` 관계로 어떤 write를 다른 thread가 반드시 관측하는지 정의한다.

### 주요 memory_order

| 값 | 의미 | 일반 용도 |
|---|---|---|
| `std::memory_order_relaxed` | 해당 atomic 값의 atomicity만 보장 | 통계 counter처럼 주변 data 공개가 불필요할 때 |
| `std::memory_order_release` | 이 연산 이전 write를 공개 | producer가 payload 작성 완료를 알릴 때 |
| `std::memory_order_acquire` | 대응 release 이전 write를 관측 | consumer가 공개된 payload를 읽기 시작할 때 |
| `std::memory_order_acq_rel` | acquire와 release를 모두 수행 | CAS 같은 read-modify-write 상태 전이 |
| `std::memory_order_seq_cst` | acquire/release에 단일 전역 순서까지 추가 | 가장 이해하기 쉬운 기본 atomic ordering |

`compare_exchange` 실패 경로는 값을 쓰지 않고 load만 수행한다. 따라서 failure ordering에는 `release`나 `acq_rel`을 사용할 수 없다.

### Release/Acquire publication

```cpp
#include <atomic>

struct ProductFacts
{
    int hs2 = 0;
    int candidateCount = 0;
};

ProductFacts facts;
std::atomic<bool> ready = false;

void Producer()
{
    facts.hs2 = 21;
    facts.candidateCount = 4;
    ready.store(true, std::memory_order_release);
}

void Consumer()
{
    if (ready.load(std::memory_order_acquire))
    {
        // release 이전의 facts write가 여기에서 보인다.
        Use(facts);
    }
}
```

Producer의 일반 memory write들이 release store보다 앞선다. Consumer가 그 값을 acquire load로 읽으면 release 이전 write가 Consumer의 이후 read보다 happens-before가 된다.

`ready`만 `relaxed`로 바꾸면 flag 자체는 atomic이지만 `facts` 공개 계약이 없다. “ready는 true인데 payload 관측은 보장되지 않는” 잘못된 설계가 된다.

### CAS로 job 소유권 획득

```cpp
enum class JobState
{
    Preparing,
    Queued,
    Running,
    Completed,
};

struct Job
{
    ProductFacts facts;
    std::atomic<JobState> state = JobState::Preparing;
};

void Publish(Job& job, ProductFacts facts)
{
    job.facts = facts;
    job.state.store(JobState::Queued, std::memory_order_release);
}

bool TryClaim(Job& job)
{
    auto expected = JobState::Queued;
    return job.state.compare_exchange_strong(
        expected,
        JobState::Running,
        std::memory_order_acq_rel,
        std::memory_order_acquire
    );
}
```

여러 worker가 `TryClaim()`을 호출해도 하나만 `Queued → Running` CAS에 성공한다. 성공한 worker의 acquire가 `Publish()`의 release와 연결되므로 `job.facts`를 볼 수 있다. CAS의 release 부분은 `Running` 상태를 관측하는 후속 thread에 성공 thread의 이전 write를 공개할 수 있다.

실패한 worker의 `expected`에는 현재 state가 들어간다. 실패 후 다른 payload를 읽지 않는다면 failure ordering을 `std::memory_order_relaxed`로 낮출 수도 있다. 그러나 성능 측정과 명확한 invariant 없이 ordering을 약하게 만드는 것은 권장하지 않는다.

`compare_exchange_weak()`은 값이 같아도 spurious failure가 가능하므로 보통 retry loop에서 사용한다. 한 번만 판정해야 하는 상태 전이에는 `compare_exchange_strong()`이 이해하기 쉽다.

### Mutex를 쓰면 왜 memory_order가 안 보이는가

```cpp
std::mutex mutex;
ProductFacts facts;
bool ready = false;

void Producer()
{
    std::lock_guard lock(mutex);
    facts = BuildFacts();
    ready = true;
} // unlock은 release 역할

void Consumer()
{
    std::lock_guard lock(mutex); // lock은 acquire 역할
    if (ready)
        Use(facts);
}
```

같은 mutex의 `unlock`과 이후 `lock`이 synchronization 관계를 만든다. `std::lock_guard`를 사용해 왔다면 memory ordering을 직접 작성하지 않았어도 이미 acquire/release 효과를 사용한 것이다.

현재 Python의 `with self._condition:`과 `with self._answerLock:`도 이 수준의 synchronization을 library가 제공한다. Python application code에서 C++식 `memory_order_acquire`를 직접 지정하지 않는다.

### DirectX12와의 대응

CPU `memory_order`와 DirectX12 resource barrier는 동일한 API가 아니지만 “작업 순서와 관측 가능성을 명시한다”는 관점은 비슷하다.

- CPU `release/acquire`: CPU thread 사이 shared memory publication
- `ID3D12Fence` signal/wait: CPU와 GPU 또는 queue 사이 완료 순서 동기화
- D3D12 resource barrier: GPU command와 resource state/order 제어

D3D12 barrier나 fence가 C++ host variable의 data race를 해결하지는 않는다. 반대로 `std::mutex`가 GPU resource transition을 대신하지도 않는다. 동기화 domain이 다르다.

Windows의 `InterlockedCompareExchange`, C#의 `Interlocked.CompareExchange`도 CAS 계열이다. `std::atomic::compare_exchange_*`는 이를 C++ memory model 안에서 portable하게 표현한다.

### CAS 사용 시 추가 위험

- ABA: 값이 `A → B → A`로 바뀌면 CAS는 중간 변경을 모를 수 있다.
- Busy retry: contention이 크면 CAS loop가 CPU를 계속 소비한다.
- Lifetime: lock-free pointer를 읽는 동안 다른 thread가 object를 해제할 수 있다.
- False sharing: 서로 다른 atomic도 같은 cache line에 있으면 성능이 급락할 수 있다.

따라서 lock-free container를 직접 만드는 것은 마지막 선택이다. 먼저 `std::mutex`, `std::condition_variable`, ownership 분리, queue를 사용한다. CAS는 단순 state claim이나 counter처럼 invariant가 작고 성능 근거가 있을 때 적합하다.

## 동시성과 병렬성

| 개념 | 의미 | 현재 프로젝트 예시 |
|---|---|---|
| Concurrency | 여러 작업의 진행 시간이 겹침 | Pipeline thread가 DB를 기다리는 동안 Flask가 상태 조회 처리 |
| Parallelism | 여러 작업이 같은 순간 여러 CPU core에서 실행 | Paddle/Torch 같은 native code가 내부 thread를 사용하면 가능할 수 있음 |
| Synchronization | 공유 상태의 순서와 정합성 제어 | `RunRegistry._condition`, `_answerLock` |
| Mutual exclusion | 임계구역에 한 thread만 진입 | `with self._rateLimitLock:` |

게임 서버 관점에서 concurrency는 여러 connection과 job이 동시에 살아 있는 상태다. Parallelism은 실제로 여러 worker/core가 같은 순간 계산하는 상태다. 둘은 같은 말이 아니다.

## CPython Thread가 동작하는 원리

1. `threading.Thread.start()`가 OS thread를 생성한다.
2. OS scheduler는 각 thread를 실행 가능한 CPU core에 배치한다.
3. Python bytecode를 실행하려는 thread는 먼저 GIL을 획득해야 한다.
4. 실행 중인 thread는 일정 시점, blocking I/O, `Condition.wait()` 또는 일부 native extension 호출에서 GIL을 양보한다.
5. 다른 Python thread가 GIL을 얻어 bytecode를 실행한다.

즉 OS thread는 여러 개지만, 일반 CPython bytecode 실행 권한은 process 안에서 한 번에 하나다.

```text
Thread A  [Python 실행] [DB 대기................] [Python 실행]
Thread B  [GIL 대기...] [Python 실행] [SSE 대기............]
Thread C  [GIL 대기..............] [Python 실행]
```

DB socket, HTTP 요청, 파일 대기 같은 blocking 구간에서는 해당 thread가 CPU를 쓸 수 없다. CPython과 관련 C library가 이 구간에서 GIL을 해제하면 다른 thread가 진행한다. 그래서 I/O 중심 서버에서는 GIL이 있어도 thread가 유효하다.

NumPy, Paddle, Torch 같은 C/C++ extension은 연산 중 GIL을 해제하거나 자체 native thread pool을 사용할 수 있다. 이 경우 Python 호출자는 하나여도 실제 계산은 여러 core에서 병렬 실행될 수 있다. 모든 연산이 반드시 GIL을 해제한다고 가정하면 안 되며 측정이 필요하다.

## GIL과 애플리케이션 Lock의 차이

GIL의 주 목적은 CPython interpreter 내부 상태를 보호하는 것이다. 아래 business invariant는 보호하지 않는다.

```python
if job_id not in runs:
    runs[job_id] = CreateRun()
```

두 bytecode 사이에서 thread 전환이 일어나면 두 thread 모두 “없음”을 관측할 수 있다. 현재 CPython에서 일부 `dict`/`list` 단일 연산이 GIL 아래 실행되더라도, 여러 연산으로 구성된 **check-then-act** 계약은 atomic하지 않다. 구현 세부에 의존해서도 안 된다.

C++에서는 mutex 없이 같은 `std::unordered_map`을 읽고 쓰면 data race이며 undefined behavior다. CPython의 GIL은 interpreter memory corruption 위험을 줄이지만, 중복 생성·lost update·잘못된 상태 전이 같은 논리 race는 그대로 남는다.

| Python | C++ 대응 | 용도 |
|---|---|---|
| `threading.Thread` | `std::thread` | OS thread에서 함수 실행 |
| `threading.Lock` | `std::mutex` | 비재진입 상호배제 |
| `threading.RLock` | `std::recursive_mutex` | 같은 thread의 재진입 허용 |
| `threading.Condition` | `std::condition_variable` + mutex | 상태 변화까지 sleep 후 재확인 |
| `threading.Event` | flag + condition variable | 단방향 상태 신호 |
| `queue.Queue` | mutex + condition variable 기반 bounded queue | thread-safe 작업 전달·backpressure |
| GIL | 직접 대응 없음 | CPython interpreter 실행권 |

GIL을 C++의 application-wide `std::mutex`처럼 이해하면 mental model에는 도움이 된다. 하지만 개발자가 lock/unlock 범위를 설계하지 않고 interpreter가 관리한다는 점, business state를 보호하지 않는다는 점에서 같은 물건은 아니다.

## 현재 프로젝트의 Thread 구조

시각 자료: [현재 Flask thread와 동기화 경계](docs/architecture/flask-thread-synchronization.excalidraw)

```text
CPython process
├─ Main thread
├─ Flask request thread: POST /api/runs
│  └─ daemon Pipeline thread 1개 생성 후 202 반환
├─ Flask request thread: GET /api/runs/{id}/events
│  └─ Condition에서 event 또는 heartbeat 대기
├─ Flask request thread: GET snapshot/document package
├─ Flask request thread: POST question-answers
│  └─ 현재 request thread에서 replay를 동기 실행
└─ Native library 내부 thread: Paddle/Torch/BLAS 등이 필요에 따라 생성

별도 process
└─ 선택적 MLX-VLM server
```

`asap_app.py`는 `app.run(..., threaded=True)`를 사용한다. 개발용 Flask server가 HTTP 요청마다 thread를 사용할 수 있다는 뜻이다. 이것은 production worker architecture가 아니다.

`POST /api/runs` 흐름은 다음과 같다.

1. Flask request thread가 payload를 검증한다.
2. `RunRegistry.CreateRun()`이 `queued` 상태를 등록한다.
3. `PipelineRunService.StartBackgroundRun()`이 daemon thread를 생성한다.
4. HTTP request는 `202 Accepted`를 반환한다.
5. Pipeline thread가 `running`으로 변경하고 pipeline을 실행한다.
6. progress callback이 event를 registry에 append한다.
7. SSE request thread가 통지를 받고 browser로 event를 보낸다.

## 현재 Pipeline 내부 병렬성

현재 구현에는 두 종류의 concurrency를 구분해야 한다.

- Inter-run concurrency: 서로 다른 job은 각 daemon thread에서 시간이 겹칠 수 있다.
- Intra-run parallelism: 한 job 안의 Pipeline lane을 동시에 실행하는 기능은 현재 없다.

Top-level `ExportPipelineManager.Run()`은 다음 step을 `for` loop로 하나씩 호출한다.

```text
User Input Preparation
→ Kurly Product Collection
→ HS Code Classification
→ Document Recommendation
```

HS classification 내부도 순차다.

```text
Build Raw Input
→ Evidence Intake
→ Product Understanding
→ HS2 Routing
→ Classification
```

Kurly intake도 다음 순서로 blocking 실행된다.

```text
Rendered Page 수집
→ Page parsing
→ 조건 충족 시 OCR candidate image를 for loop로 순차 처리
→ OCR text 결합
→ configured 시 LLM Input Reconstruction
```

또한 `_KURLY_OCR_RUNTIME_LOCK`이 전체 OCR pipeline을 감싸므로 서로 다른 job의 OCR도 process당 하나씩 실행된다.

Product Understanding 안의 “lane”도 현재 graph node가 아니다. 한 `Run()` method 안에서 순서대로 호출되는 함수다.

```text
COI evidence
→ configured 시 Wikipedia HTTP lookup
→ IdentityDistillerService
→ optional IdentityHintAgent LLM
→ deterministic identity transcription
→ Composition lane
→ Wikipedia material term merge
→ ProductUnderstandingPackage 저장
```

`IdentityDistillerService`는 LLM agent가 아니다. Wikipedia evidence를 regex/token 규칙으로 `DistilledIdentityFacts`로 바꾸는 결정론적 service다. 실제 LLM 호출은 이후 `_MaybeEnrichIdentityWithLlm()`이 사용하는 `IdentityHintAgent`에 있다.

따라서 현재 서버의 thread 구조는 여러 HTTP request와 여러 run의 대기 시간을 겹칠 수 있지만, 한 run 내부에서 OCR·Wikipedia·LLM·Composition을 DAG로 병렬 scheduling하지 않는다.

## LangGraph 목표와 현재 구조의 차이

시각 자료: [목표 LangGraph Lane DAG와 사용자 Interaction FSM](docs/architecture/langgraph-pipeline-fsm.excalidraw)

LangGraph는 현재 dependency가 아니며 기존 Pipeline도 LangGraph FSM이 아니다. 도입 목표는 아래처럼 **의존성이 없는 I/O lane을 병렬 실행하고 질문 상태를 interrupt/resume cycle로 표현하는 것**으로 이해해야 한다.

현재 코드 의존성을 보존하는 최소 target graph는 다음과 같다.

```text
Rendered Page + parsing
        │
        ├─ A: image download → OCR/VLM → final reconstruction → base composition
        │
        └─ B: product name → Wikipedia lookup → deterministic identity distillation
                                      │
                  A reconstruction ───┘
                                      ↓
                              IdentityHintAgent LLM
                                      │
          base composition + encyclopedia material merge
                                      │
                                      ↓
                         ProductUnderstandingPackage join
                                      ↓
                           HS2 → HS4/HS6/CN8
                                      ↓
                         needs input? ─ yes → interrupt
                                      ↑            │
                                      └─ answer ───┘
```

### LangGraph 공식 실행 계약

LangGraph의 node는 shared object를 임의 수정하는 대신 state의 부분 update를 반환한다. 같은 super-step의 독립 node는 병렬 실행될 수 있으며, 여러 node가 같은 field를 갱신하려면 reducer가 필요하다. reducer 없이 같은 field를 동시에 덮으면 `InvalidUpdateError`가 발생할 수 있다. 따라서 병렬성은 thread-safe shared JSON이 아니라 **명시적인 state field 소유권과 join 규칙**으로 설계해야 한다. 자세한 기준은 [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)와 [Graph API 사용법](https://docs.langchain.com/oss/python/langgraph/use-graph-api)에 있다.

`interrupt()`는 checkpointer에 graph state를 저장하고 실행을 멈춘다. 같은 thread ID와 `Command(resume=...)`로 재개하면 중단 지점 다음 줄로 단순 복귀하지 않고 해당 node가 처음부터 다시 실행된다. 따라서 interrupt 이전의 HTTP 호출, 파일 저장, DB 변경은 재실행돼도 안전하도록 idempotent해야 한다. 운영 환경에는 durable checkpointer가 필요하다. [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)를 따른다.

Checkpoint는 graph state의 복구 수단이지 외부 HTTP·파일·DB 부작용의 transaction이 아니다. `max_concurrency`도 graph task 수를 제한할 뿐 OCR runtime, GPU memory, 외부 API quota를 대신 보호하지 않는다. 이 자원에는 별도 semaphore, bounded worker, timeout, idempotency key, DB transaction이 필요하다.

중요한 제약이 있다.

- 현재 Input Reconstruction은 OCR 결과까지 받은 뒤 실행된다. OCR을 기다리는 동안 identity 작업을 시작하려면 먼저 DOM에서 `BasicProductSeed`를 분리해야 한다.
- Wikipedia lookup은 product name만 있으면 OCR과 겹칠 수 있다.
- 현재 IdentityHintAgent LLM은 reconstructed fact text도 입력받으므로 final reconstruction 이전에 그대로 실행하면 입력 계약이 달라진다.
- Base Composition은 reconstruction 이후 시작할 수 있고, encyclopedia material merge는 Wikipedia branch와 join한 뒤 수행할 수 있다.
- OCR/VLM은 CPU/GPU resource-bound이므로 무조건 병렬화하면 처리량이 오히려 떨어질 수 있다. bounded model worker가 필요하다.

### Blackboard 정합성

현재 `blackboard.json`은 graph shared memory로 사용할 수 없다.

`BlackboardStore.put()`과 `append()`는 매번 전체 JSON을 `load → mutate → save`한다. 두 node가 동시에 서로 다른 key를 써도 마지막 `save`가 앞선 변경을 덮는 lost update가 발생할 수 있다. `next_id()`의 in-memory counter도 concurrent allocator가 아니다.

LangGraph 기반 구조에서는 다음 계약이 필요하다.

1. Graph state를 typed field로 정의한다.
2. 각 node는 전체 state를 직접 수정하지 않고 자신의 state delta만 반환한다.
3. 병렬 node는 서로 다른 field를 소유한다.
4. 같은 field로 합류할 때는 명시적 reducer와 deterministic join을 둔다.
5. checkpoint 저장소가 state version과 resume 지점을 소유한다.
6. `blackboard.json`은 live shared state가 아니라 debug/export snapshot으로만 생성한다.

LangGraph 자체도 rate limit, GPU memory, Python GIL, DB race를 자동 해결하지 않는다. Graph는 실행 순서·분기·join·interrupt를 표현한다. 실제 자원 제어는 semaphore, bounded worker, timeout, idempotency key, DB transaction이 담당해야 한다.

## 현재 Lock별 보호 범위

| 위치 | primitive | 보호 대상 | 평가 |
|---|---|---|---|
| `backend/pipeline_service.py:82` | `Condition(Lock)` | `_runs`, status, event buffer | 짧은 process-local 임계구역으로 적절 |
| `backend/pipeline_api.py:42` | `Lock` | client별 rate-limit timestamp | check와 append를 atomic하게 묶음 |
| `backend/pipeline_service.py:495` | `Lock` | 질문 답변 반영과 Blackboard replay | 모든 run을 전역 직렬화함 |
| `product/pipeline/kurly_url_facts.py:41` | `Lock` | 재사용 Paddle OCR runtime | thread safety 우선, OCR 동시성 1 |
| `db/db_session_manager.py:146` | `Lock` | singleton engine 생성·교체 | 중복 생성/폐기 방지 |
| `db/db_session_manager.py:183` | `queue.Queue` | 동시 DB query slot | counting semaphore와 backpressure 역할 |
| `document/document_package_builder.py:34` | `Lock` | psycopg2 pool 생성·교체 | pool lifecycle 보호 |

### RunRegistry의 Condition

`Condition`은 mutex와 상태 변화 알림을 묶는다.

```python
with self._condition:
    self._runs[run_id]["status"] = "running"
    self._condition.notify_all()
```

SSE 쪽은 `wait_for(predicate, timeout=15)`를 사용한다.

```python
with self._condition:
    self._condition.wait_for(has_event_or_terminal, timeout=15)
    events = list(run["events"])
# network yield는 lock 밖에서 수행
```

`wait_for()`는 기다리는 동안 내부 lock을 해제한다. Pipeline thread는 그 사이 registry를 갱신할 수 있다. event가 생기면 `notify_all()`이 waiter를 깨운다. waiter는 lock을 다시 획득하고 predicate를 재검사한다. timeout이면 SSE heartbeat를 보낸다.

Predicate 재검사가 중요한 이유는 spurious wakeup과 여러 소비자 경쟁 때문이다. `notify`는 event payload를 저장하지 않는다. 공유 상태를 먼저 변경하고, 같은 lock의 계약 아래 통지해야 한다.

현재 구현은 event list를 lock 안에서 복사하고 실제 `yield`는 lock 밖에서 수행한다. 느린 browser 송신 중 registry 전체를 잠그지 않는다는 점은 올바르다.

### 질문 답변 Lock

`_answerLock`은 질문 JSON 갱신만 보호하지 않는다. 답변 검증, Blackboard 저장, Classification replay, Document replay, registry 갱신까지 전체를 감싼다.

장점:

- 같은 process에서 두 답변 요청이 파일과 질문 상태를 동시에 갱신하지 못한다.
- 현재 데모 규모에서 구현이 단순하다.

한계:

- 서로 다른 run의 답변도 한 번에 하나만 처리된다.
- 긴 Classification/Document 작업 중 lock을 계속 보유한다.
- 다른 process 또는 Pod의 lock과는 공유되지 않는다.
- `BlackboardStore.save()` 자체는 직접 `write_text()`하며 process 간 atomic transaction이 아니다.

따라서 현재 lock은 단일 process 데모에는 유효하지만 distributed synchronization 수단은 아니다.

### OCR Lock

Paddle OCR engine을 process 수명 동안 cache하고 `_KURLY_OCR_RUNTIME_LOCK`으로 `pipeline.Run()`을 직렬화한다. 모델의 thread safety와 GPU/메모리 충돌을 피하는 보수적 전략이다. 동시에 여러 job이 OCR 단계에 도착해도 하나만 실행되므로 처리량 상한이 명확하다.

향후 해결책은 lock 제거가 아니라 OCR 전용 worker와 bounded queue다. 모델 instance 하나를 한 worker가 소유하면 공유 mutable runtime 자체가 사라진다.

### DB 동시성

`DbSessionManager`는 SQLAlchemy engine은 공유하지만 ORM `Session`은 `OpenSession()`마다 새로 만든다. Session/connection을 thread 사이에서 공유하지 않는 올바른 방향이다.

`_querySlots`는 token queue다. token을 얻은 thread만 DB session을 열 수 있고, 제한 시간 내 token을 얻지 못하면 `TimeoutError`가 발생한다. C++의 counting semaphore와 비슷하다.

Document DB의 `ThreadedConnectionPool`은 connection checkout을 지원한다. 별도 cache dict는 전용 lock 없이 check 후 기록하므로 고정 schema에서는 같은 metadata query가 중복 실행될 수 있다. 결과가 동일하다는 전제의 benign race에 가깝지만, schema를 runtime 중 변경하는 구조에는 적합하지 않다.

## C++ std::thread로 본 동일 모델

현재 Python daemon thread 생성은 개념적으로 다음과 비슷하다.

```cpp
std::thread worker([&registry, request] {
    RunPipeline(registry, request);
});
worker.detach(); // 현재 Python은 thread handle을 저장하거나 join하지 않음
```

`daemon=True`와 `detach()`는 완전히 같지는 않다. 공통점은 호출자가 완료를 join하지 않는다는 것이다. Python daemon thread는 interpreter 종료를 막지 않으며 종료 중 갑자기 중단될 수 있다. 현재 구조에서는 shutdown, cancellation, 완료 대기를 제어할 owner가 없다.

아래 C++ 예시는 `RunRegistry`의 mutex와 condition variable 구조를 축약한 것이다.

```cpp
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

using namespace std::chrono_literals;

enum class RunStatus
{
    Queued,
    Running,
    AwaitingInput,
    Completed,
    Failed,
};

struct RunState
{
    RunStatus status = RunStatus::Queued;
    std::vector<std::string> events;
};

class RunRegistry
{
public:
    void Create(std::string runId)
    {
        {
            std::lock_guard lock(mutex_);
            runs_.try_emplace(std::move(runId));
        }
        changed_.notify_all();
    }

    void AppendEvent(const std::string& runId, std::string event)
    {
        {
            std::lock_guard lock(mutex_);
            runs_.at(runId).events.push_back(std::move(event));
        }
        changed_.notify_all();
    }

    void SetStatus(const std::string& runId, RunStatus status)
    {
        {
            std::lock_guard lock(mutex_);
            runs_.at(runId).status = status;
        }
        changed_.notify_all();
    }

    std::optional<RunState> WaitForChange(
        const std::string& runId,
        std::size_t eventIndex)
    {
        std::unique_lock lock(mutex_);
        changed_.wait_for(lock, 15s, [&] {
            const auto it = runs_.find(runId);
            return it == runs_.end()
                || it->second.events.size() > eventIndex
                || IsTerminal(it->second.status);
        });

        const auto it = runs_.find(runId);
        if (it == runs_.end())
            return std::nullopt;
        return it->second; // lock 안에서 snapshot copy
    }

private:
    static bool IsTerminal(RunStatus status)
    {
        return status == RunStatus::AwaitingInput
            || status == RunStatus::Completed
            || status == RunStatus::Failed;
    }

    std::mutex mutex_;
    std::condition_variable changed_;
    std::unordered_map<std::string, RunState> runs_;
};
```

핵심 대응은 다음과 같다.

```text
Python: with condition              C++: std::unique_lock / std::lock_guard
Python: condition.wait_for(...)     C++: condition_variable::wait_for(...)
Python: condition.notify_all()      C++: condition_variable::notify_all()
Python: list/dict snapshot copy     C++: RunState value copy
```

`std::condition_variable::wait_for()`도 대기 중 mutex를 놓고 깨어날 때 다시 잡는다. Predicate는 반드시 lock 아래 공유 상태를 읽어야 한다. C++에서는 mutex의 unlock→lock이 happens-before 관계를 형성해 이전 thread의 쓰기를 다음 thread가 관측하게 한다.

## Locking 설계 원칙

1. 먼저 보호할 invariant를 정의한다. “dict를 보호”가 아니라 “status와 event index를 같은 시점의 상태로 본다”처럼 작성한다.
2. 공유 상태 읽기와 쓰기를 같은 lock 계약 아래 둔다.
3. lock 안에서는 메모리 상태 변경과 snapshot copy만 수행한다.
4. DB, HTTP, OCR, LLM, 파일 대기, browser 송신 중에는 registry lock을 보유하지 않는다.
5. 여러 lock이 필요하면 항상 같은 획득 순서를 유지한다.
6. `Condition`은 predicate loop와 함께 사용한다.
7. thread 간 resource 전달은 공유 pointer보다 immutable DTO 또는 queue를 우선한다.
8. DB Session, cursor, file handle은 thread마다 별도 소유한다.
9. process-local lock을 DB·멀티프로세스·Pod 동기화 수단으로 착각하지 않는다.

`Lock`은 재진입 불가다. 현재 `RunRegistry`가 `_AppendEventLocked`, `_FindActiveRunBySignatureLocked`처럼 “이미 lock을 가진 호출자 전용” helper를 분리한 이유다. public method 안에서 public method를 다시 호출해 같은 lock을 획득하면 self-deadlock이 발생할 수 있다.

## 현재 전략의 객관적 평가

### 적절한 부분

- HTTP request와 장시간 Pipeline을 분리해 `POST /api/runs`가 빠르게 `202`를 반환한다.
- `RunRegistry`의 공유 dict와 event list는 하나의 `Condition` 계약으로 보호된다.
- SSE wait 중 lock을 해제하고 network yield도 lock 밖에서 수행한다.
- rate-limit의 check/update가 하나의 임계구역이다.
- DB Session을 thread마다 열고 닫는다.
- OCR runtime의 불명확한 thread safety를 lock으로 보수적으로 제한한다.

### 한계와 위험

- 요청마다 daemon job thread를 무제한 생성할 수 있다. Rate limit은 생성 빈도만 제한하며 active thread 수를 직접 제한하지 않는다.
- daemon thread handle을 저장하지 않아 join, cancellation, graceful shutdown이 없다.
- 순수 Python CPU 작업은 GIL 때문에 job thread 수만큼 빨라지지 않는다.
- Paddle/Torch/BLAS가 각 job 안에서 native thread를 추가 생성하면 CPU oversubscription이 발생할 수 있다.
- SSE 연결 하나가 Flask request thread 하나를 장시간 점유한다.
- `_answerLock`이 서로 무관한 run까지 긴 시간 직렬화한다.
- `_KURLY_OCR_RUNTIME_LOCK` 때문에 process당 OCR 동시성은 1이다.
- 모든 lock, registry, rate-limit state는 process-local이다. Gunicorn worker나 Pod가 둘이면 서로 보이지 않는다.
- `RunRegistry` snapshot은 일부만 shallow copy한다. 하위 mutable object는 caller가 변경하지 않는다는 규율에 의존한다.
- Blackboard JSON 쓰기는 DB transaction이 아니며 multi-process writer에 안전하지 않다.

현재 구조는 **저부하 단일 process 시연용으로는 합리적**이다. Production multi-worker server로 간주하면 안 된다.

## 향후 권장 전략

| 작업 종류 | 권장 실행 모델 |
|---|---|
| 짧은 request validation·projection | 동기 함수 |
| DB/HTTP 같은 blocking I/O | 제한된 thread 또는 외부 worker |
| 순수 Python CPU 계산 | process worker 또는 별도 service |
| Paddle/Torch/OCR | 모델 전용 bounded worker, 실제 처리량 측정 |
| run/event/question 상태 | PostgreSQL transaction과 constraint |
| 대형 artifact | object storage |
| 여러 Pod의 작업 전달 | durable queue/DB lease, process-local lock 금지 |

진행 순서는 다음이 안전하다.

1. 동시에 허용할 active job 수를 명시한다.
2. thread 1개/job 대신 bounded queue 또는 외부 worker가 job을 claim하게 한다.
3. `run`, `event`, 질문 답변을 PostgreSQL authoritative state로 이동한다.
4. 답변 중복은 `(run_id, question_id)` unique constraint와 상태 version으로 막는다.
5. 긴 replay는 HTTP request thread에서 분리해 다시 `202` job으로 처리한다.
6. OCR worker 수와 native library thread 수를 함께 제한한다.
7. graceful shutdown, timeout, cancellation, retry 정책을 추가한다.
8. 그 후에만 process 또는 Pod 수를 늘린다.

## 최종 요약

Python 멀티스레딩은 불가능한 것이 아니다. CPython 3.12에서는 **I/O concurrency는 가능하지만 순수 Python CPU parallelism은 GIL로 제한**된다. 현재 프로젝트는 이 특성을 이용해 Flask 요청, Pipeline job, SSE 대기를 겹친다.

GIL은 `std::mutex`를 대체하지 않는다. 현재의 `Condition`, rate-limit lock, answer lock, OCR lock, DB query queue는 각각 다른 invariant를 보호한다. 이 lock들은 한 process 안에서만 유효하다. 다중 process·Docker replica·Kubernetes Pod로 확장하려면 공유 상태와 동기화 책임을 PostgreSQL, durable queue, object storage로 옮겨야 한다.

## 관련 파일

- `environments.yml`
- `asap_app.py`
- `src/backend/pipeline_api.py`
- `src/backend/pipeline_service.py`
- `src/db/db_session_manager.py`
- `src/bussiness_logic/pipeline/blackboard/store.py`
- `src/bussiness_logic/product/pipeline/kurly_url_facts.py`
- `src/bussiness_logic/document/document_package_builder.py`
